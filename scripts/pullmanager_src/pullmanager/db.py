"""Database adapter.

pyodbc is imported lazily so the rest of the package -- planning, rendering,
the dry run -- works on a machine without a driver.

The behaviours here were harvested from the old generator rather than invented;
see "Connection And Execution Facts" in pullmanager_contracts.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
# Both hosts are DNS aliases, not machine names. The real Cosmos instance is
# discovered per connection with @@SERVERNAME, because it changes every time.
DEFAULT_COSMOS_SERVER = "COSMOS"
DEFAULT_PROJECTS_SERVER = "PROJECTS"
DEFAULT_UPLOAD_CHUNK = 20_000

# `GO` is a client batch separator, not T-SQL. The driver rejects it.
BATCH_SEPARATOR = "GO"


class DatabaseError(RuntimeError):
    """A SQL failure, carrying the server messages that explain it."""

    def __init__(self, message: str, server_messages: Sequence[str] = ()):
        super().__init__(message)
        self.server_messages = list(server_messages)

    def __str__(self) -> str:
        base = super().__str__()
        if not self.server_messages:
            return base
        detail = "\n".join(f"  [SQL MESSAGE] {m}" for m in self.server_messages)
        return f"{base}\n{detail}"


ENV_FILENAME = ".env"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Deliberately small: the bundle stays stdlib-only."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def find_env_file(explicit: str | Path | None = None, *extra: Path) -> Path | None:
    """Locate a .env: an explicit path, then the usual places."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise DatabaseError(f"No .env file at {path}")
        return path
    candidates = [
        Path.cwd() / ENV_FILENAME,
        Path(__file__).resolve().parent.parent / ENV_FILENAME,
        *[Path(p) / ENV_FILENAME for p in extra],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Read a .env into the process environment.

    A real environment variable wins over the file unless `override`, which is
    what lets a one-off run be redirected without editing the file.
    """
    found = find_env_file(path)
    if found is None:
        return {}
    values = parse_env_file(found.read_text(encoding="utf-8"))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


@dataclass
class Settings:
    """Connection settings. Windows auth, so never credentials."""

    cosmos_server: str = DEFAULT_COSMOS_SERVER
    cosmos_database: str = "COSMOS"
    projects_server: str = DEFAULT_PROJECTS_SERVER
    projects_database: str = ""
    driver: str = DEFAULT_DRIVER
    login_timeout: int = 10
    query_timeout: int = 0
    upload_chunk: int = DEFAULT_UPLOAD_CHUNK

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def get(name: str, fallback: str) -> str:
            return str(source.get(f"PULLMANAGER_{name}", fallback) or fallback)

        def get_int(name: str, fallback: int) -> int:
            try:
                return int(source.get(f"PULLMANAGER_{name}", fallback))
            except (TypeError, ValueError):
                return fallback

        return cls(
            cosmos_server=get("COSMOS_SERVER", DEFAULT_COSMOS_SERVER),
            cosmos_database=get("COSMOS_DATABASE", "COSMOS"),
            projects_server=get("PROJECTS_SERVER", DEFAULT_PROJECTS_SERVER),
            projects_database=str(source.get("PULLMANAGER_PROJECTS_DATABASE", "") or ""),
            driver=get("ODBC_DRIVER", DEFAULT_DRIVER),
            login_timeout=get_int("LOGIN_TIMEOUT", 10),
            query_timeout=get_int("QUERY_TIMEOUT", 0),
            upload_chunk=get_int("UPLOAD_CHUNK", DEFAULT_UPLOAD_CHUNK),
        )

    def connection_string(self, server: str, database: str) -> str:
        if not server:
            raise DatabaseError("No server configured. Set it in .env.")
        if not database:
            raise DatabaseError("No database configured. Set it in .env.")
        return (
            f"Driver={{{self.driver}}};"
            f"Server=tcp:{server};"
            f"Database={database};"
            "Trusted_Connection=yes;"
        )

    def cosmos_connection_string(self, database: str | None = None) -> str:
        return self.connection_string(self.cosmos_server, database or self.cosmos_database)

    def projects_connection_string(self, database: str | None = None) -> str:
        return self.connection_string(
            self.projects_server, database or self.projects_database
        )


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[tuple]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class ExecutionResult:
    result_sets: list[ResultSet] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def rows_of(self, *columns: str) -> list[dict[str, Any]]:
        """Result sets carrying a declared telemetry shape."""
        wanted = set(columns)
        out: list[dict[str, Any]] = []
        for result in self.result_sets:
            if wanted.issubset(set(result.columns)):
                out.extend(result.as_dicts())
        return out


def split_batches(script: str) -> list[str]:
    """Split on lines consisting solely of GO, which the driver cannot execute."""
    batches: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        if line.strip().upper() == BATCH_SEPARATOR:
            if current:
                batches.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        batches.append("\n".join(current))
    return [b for b in batches if b.strip()]


def collect_messages(cursor: Any) -> list[str]:
    """Server PRINT output and nested engine errors.

    This is what surfaces the inner error of a failed OPENQUERY; without it the
    caller sees only a generic outer failure.
    """
    try:
        raw = list(cursor.messages or [])
    except Exception:
        return []
    return [" | ".join(str(part) for part in message) for message in raw]


def drain(cursor: Any) -> list[ResultSet]:
    """Consume every result set a batch produced.

    A generated script interleaves DDL, inserts and telemetry SELECTs, so a
    batch yields a mixture of row-producing and silent statements.
    """
    results: list[ResultSet] = []
    while True:
        if cursor.description is not None:
            columns = [column[0] for column in cursor.description]
            try:
                rows = list(cursor.fetchall())
            except Exception:
                rows = []
            results.append(ResultSet(columns=columns, rows=rows))
        else:
            try:
                cursor.fetchall()
            except Exception:
                pass  # statement produced no rows
        try:
            if not cursor.nextset():
                break
        except Exception:
            break
    return results


def execute_script(connection: Any, script: str, *, label: str = "script") -> ExecutionResult:
    """Run every batch of a script, failing fast with server messages."""
    outcome = ExecutionResult()
    batches = split_batches(script)
    cursor = connection.cursor()
    for index, batch in enumerate(batches, start=1):
        try:
            cursor.execute(batch)
            outcome.messages.extend(collect_messages(cursor))
            outcome.result_sets.extend(drain(cursor))
        except Exception as exc:
            messages = collect_messages(cursor)
            raise DatabaseError(
                f"{label}: batch {index}/{len(batches)} failed: {exc}", messages
            ) from exc
    return outcome


def capture_server_name(connection: Any) -> str:
    """The Cosmos instance name, which changes on every connection."""
    cursor = connection.cursor()
    cursor.execute("SELECT @@SERVERNAME;")
    row = cursor.fetchone()
    if not row or not row[0]:
        raise DatabaseError(
            "Could not determine the Cosmos instance via @@SERVERNAME. Local SQL "
            "cannot build OPENQUERY without it."
        )
    return str(row[0])


def chunked(rows: Iterable[Sequence[Any]], size: int) -> Iterator[list[Sequence[Any]]]:
    batch: list[Sequence[Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def bulk_insert(
    connection: Any,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    chunk_size: int = DEFAULT_UPLOAD_CHUNK,
) -> int:
    """Insert rows with parameter arrays rather than a literal VALUES list.

    A table value constructor is capped at 1000 rows; parameter arrays are not,
    because the statement stays single-row and only its bindings repeat. Values
    are bound rather than interpolated, so quotes and NULLs need no escaping.

    Chunked because fast_executemany allocates buffers from declared column
    width times batch size, so memory grows with both.
    """
    if not columns:
        raise DatabaseError(f"Cannot insert into {table}: no columns given.")
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(f"[{c}]" for c in columns)
    statement = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"

    cursor = connection.cursor()
    try:
        cursor.fast_executemany = True
    except Exception:
        pass  # older drivers fall back to per-row inserts

    inserted = 0
    for batch in chunked(rows, max(1, chunk_size)):
        try:
            cursor.executemany(statement, batch)
        except Exception as exc:
            raise DatabaseError(
                f"Bulk insert into {table} failed after {inserted} row(s): {exc}",
                collect_messages(cursor),
            ) from exc
        inserted += len(batch)
    return inserted


def connect(connection_string: str, *, login_timeout: int = 10, query_timeout: int = 0) -> Any:
    """Open a connection. pyodbc is imported here so the package loads without it."""
    try:
        import pyodbc
    except ImportError as exc:
        raise DatabaseError(
            "pyodbc is not installed. It is needed only to execute; planning, "
            "rendering and --dry-run work without it."
        ) from exc

    connection = pyodbc.connect(connection_string, timeout=login_timeout)
    if query_timeout:
        connection.timeout = query_timeout
    return connection
