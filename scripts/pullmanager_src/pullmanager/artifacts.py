"""`--artifacts`: a pull's finished tables, as parquets in its run folder (D72).

    runs/<project>/parquets/SneakPeek/   tables pulled from COSMOS_SneakPeek (`_sp`)
    runs/<project>/parquets/Cosmos/      tables pulled from COSMOS
    runs/<project>/parquets/uploads/     the uploads, copied from the split

Only finished tables are packaged, and the manifest decides which those are,
never what happens to exist in Projects: a PK table once its PK phase is done,
a run's table once every run that fills it is. The rest are listed with why.
Each table is read from Projects in chunks and written with pyarrow, typed
from its own columns; `_batch` is internal and dropped. A batching dimension
marked `separate_parquets` gives one file per value. Each packaging replaces
the last.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import uploads
from .batches import dimension_predicate
from .manifest import Manifest, Session
from .models import DONE, SKIPPED
from .naming import destination
from .normalize import NormalizationError, cosmos_database
from .pulls import run_folder
from .yaml_io import load_yaml

PARQUETS_DIR = "parquets"
SNEAKPEEK_DIR = "SneakPeek"
COSMOS_DIR = "Cosmos"
UPLOADS_DIR = "uploads"
BATCH_COLUMN = "_batch"
SP_SUFFIX = "_sp"
FETCH_ROWS = 50_000


class ArtifactError(RuntimeError):
    """Raised when a pull cannot be packaged at all."""


@dataclass
class Part:
    """One parquet file of a table: the whole table, or one separated value."""

    label: str  # "" for the whole table, else its values joined: "LA", "LA_Female"
    where: str = ""  # a WHERE clause selecting it, with its params
    params: list[Any] = field(default_factory=list)
    path: Path | None = None
    rows: int = 0
    seconds: float = 0.0


@dataclass
class TableSpec:
    """A destination table the pull made, and what it takes to package it."""

    dest: str
    kind: str  # "pk", "run" or "upload"
    session: str
    folder: str  # SneakPeek, Cosmos or uploads
    cohort: dict[str, Any] = field(default_factory=dict)
    doc: dict[str, Any] = field(default_factory=dict)  # the phase document it came from
    parts: list[Part] = field(default_factory=list)
    columns: list[tuple[str, str]] = field(default_factory=list)  # name, SQL type, as held
    source_file: Path | None = None  # an upload's parquet in the split

    @property
    def rows(self) -> int:
        return sum(part.rows for part in self.parts)


@dataclass
class Plan:
    tables: list[TableSpec] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)  # table, why
    failed: list[tuple[str, str]] = field(default_factory=list)  # table, the error (D88)


def is_settled(node: Any) -> bool:
    return node.status in (DONE, SKIPPED)


def database_folder(cohort: dict[str, Any], doc: dict[str, Any]) -> str:
    try:
        database = cosmos_database(cohort.get("cosmos_db") or doc.get("cosmos_db"))
    except NormalizationError:
        database = "COSMOS"
    return SNEAKPEEK_DIR if database.lower() == "cosmos_sneakpeek" else COSMOS_DIR


def safe_part(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_") or "blank"


def file_name(dest: str, label: str) -> str:
    """`OtherDiagnoses_LA.parquet`; a SneakPeek table keeps `_sp` last."""
    if not label:
        return f"{dest}.parquet"
    if dest.endswith(SP_SUFFIX):
        return f"{dest[: -len(SP_SUFFIX)]}_{label}{SP_SUFFIX}.parquet"
    return f"{dest}_{label}.parquet"


def separate_parts(session: Session, kind: str, runs: list[Any] | None = None) -> list[Part]:
    """One part per combination of the separated dimensions' values (D72).

    A run's table is chosen by its `_batch` labels; a PK table, which has no
    `_batch`, by the dimensions' own predicates on its columns. `runs` are
    the runs that fill the table: its table group's (D134).
    """
    groups: dict[str, list[Any]] = {}
    group_dims: dict[str, list[dict[str, Any]]] = {}
    for run in session.runs if runs is None else runs:
        dims = [d for d in (run.batch or {}).get("dimensions") or [] if d.get("separate")]
        if not dims:
            continue
        label = "_".join(
            "other" if d.get("is_other") else safe_part(d.get("value")) for d in dims
        )
        groups.setdefault(label, []).append((run.batch or {}).get("name"))
        group_dims[label] = dims
    parts: list[Part] = []
    for label, batch_names in groups.items():
        if kind == "run":
            marks = ", ".join("?" for _ in batch_names)
            parts.append(Part(label, f" WHERE [{BATCH_COLUMN}] IN ({marks})", list(batch_names)))
        else:
            clauses, params = [], []
            for dim in group_dims[label]:
                clause, values = dimension_predicate(dim)
                clauses.append(clause)
                params.extend(values)
            parts.append(Part(label, " WHERE " + " AND ".join(clauses), params))
    return parts or [Part("")]


def plan(manifest: Manifest) -> Plan:
    """Which tables to package, from the manifest and the split's documents."""
    result = Plan()
    seen: set[str] = set()
    uploads_seen: set[str] = set()
    for session in manifest.sessions:
        phases = {phase.name: phase for phase in session.phases}
        pk_phase = phases.get("pk")
        if pk_phase is not None and pk_phase.yaml:
            doc = load_yaml(manifest.resolve(pk_phase)) or {}
            source = pk_phase.pk_source or {}
            for cohort in doc.get("cohorts") or []:
                if not isinstance(cohort, dict) or cohort.get("dest_table") != session.pk_table:
                    continue
                dest = str(cohort["dest_table"])
                if source.get("kind") == "uploaded_cohort":
                    continue  # packaged as an upload
                if dest in seen:
                    continue
                seen.add(dest)
                if not is_settled(pk_phase):
                    result.left_out.append((dest, f"its PK phase is {pk_phase.status}"))
                    continue
                result.tables.append(TableSpec(
                    dest, "pk", session.session_id, database_folder(cohort, doc), cohort, doc,
                    separate_parts(session, "pk"),
                ))
        # Each table group's runs hold its own tables (D134), so each group's
        # first run names them, and a finished group is packaged even while
        # another is not.
        by_group: dict[Any, list[Any]] = {}
        for run in session.runs:
            by_group.setdefault(run.group, []).append(run)
        for runs in by_group.values():
            doc = load_yaml(manifest.resolve(runs[0])) or {}
            unsettled = [run for run in runs if not is_settled(run)]
            for cohort in doc.get("cohorts") or []:
                if not isinstance(cohort, dict) or not cohort.get("dest_table"):
                    continue
                dest = str(cohort["dest_table"])
                if dest in seen or dest == session.pk_table:
                    continue
                seen.add(dest)
                if unsettled:
                    states = ", ".join(sorted({run.status for run in unsettled}))
                    result.left_out.append(
                        (dest, f"{len(unsettled)} of its {len(runs)} run(s) are not done ({states})")
                    )
                    continue
                result.tables.append(TableSpec(
                    dest, "run", session.session_id, database_folder(cohort, doc), cohort, doc,
                    separate_parts(session, "run", runs),
                ))
        upload_phase = phases.get("upload_cohorts")
        if upload_phase is not None and upload_phase.yaml:
            doc = load_yaml(manifest.resolve(upload_phase)) or {}
            for item in doc.get("upload_cohorts") or []:
                if not isinstance(item, dict):
                    continue
                dest = str(item.get("dest_table") or item.get("name") or "")
                if not dest or dest in uploads_seen:
                    continue
                uploads_seen.add(dest)
                if str(item.get("file_type", "")).lower() != "parquet" or not item.get("file_loc"):
                    result.left_out.append(
                        (dest, "an upload with no parquet in the split; it is in Projects as "
                               f"{uploads.copy_table(dest)}")
                    )
                    continue
                spec = TableSpec(dest, "upload", session.session_id, UPLOADS_DIR, item, doc, [Part("")])
                spec.source_file = manifest.root / str(item["file_loc"])
                result.tables.append(spec)
    return result


# --------------------------------------------------------------- writing

def arrow_type(pa: Any, sql_type: str) -> Any:
    """The Arrow type for a SQL Server column type, as INFORMATION_SCHEMA gives it."""
    base = sql_type.split("(")[0].upper()
    if base == "BIGINT":
        return pa.int64()
    if base == "INT":
        return pa.int32()
    if base in ("SMALLINT", "TINYINT"):
        return pa.int16()
    if base == "BIT":
        return pa.bool_()
    if base == "FLOAT":
        return pa.float64()
    if base == "REAL":
        return pa.float32()
    if base in ("DECIMAL", "NUMERIC"):
        inside = sql_type[sql_type.index("(") + 1: sql_type.index(")")] if "(" in sql_type else "18,0"
        precision, _, scale = inside.partition(",")
        return pa.decimal128(int(precision), int(scale or 0))
    if base in ("MONEY", "SMALLMONEY"):
        return pa.decimal128(19, 4)
    if base == "DATE":
        return pa.date32()
    if base in ("DATETIME", "DATETIME2", "SMALLDATETIME"):
        return pa.timestamp("us")
    if base == "TIME":
        return pa.time64("us")
    if base in ("BINARY", "VARBINARY", "IMAGE"):
        return pa.binary()
    return pa.string()


def select_expression(name: str, sql_type: str) -> str:
    """Types the driver cannot hand back as Python values are read as text."""
    base = sql_type.split("(")[0].upper()
    if base in ("DATETIMEOFFSET", "UNIQUEIDENTIFIER", "XML", "SQL_VARIANT", "HIERARCHYID",
                "GEOGRAPHY", "GEOMETRY"):
        return f"CONVERT(NVARCHAR(MAX), [{name}]) AS [{name}]"
    return f"[{name}]"


def describe(cursor: Any, project_db: str, table: str) -> list[tuple[str, str]]:
    sql, params = uploads.describe_sql(project_db, table)
    cursor.execute(sql, params)
    return [(str(row[0]), uploads.type_from_info(*row[1:6])) for row in cursor.fetchall()]


def batches_of(cursor: Any, size: int = FETCH_ROWS) -> Iterator[list[tuple]]:
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield [tuple(row) for row in rows]


def write_part(cursor: Any, pa: Any, pq: Any, sql: str, params: list[Any],
               columns: list[tuple[str, str]], path: Path) -> int:
    """Stream one SELECT into one parquet file; returns its row count."""
    schema = pa.schema([(name, arrow_type(pa, sql_type)) for name, sql_type in columns])
    tmp = path.with_name(path.name + ".tmp")
    rows = 0
    try:
        cursor.execute(sql, params)
        with pq.ParquetWriter(tmp, schema) as writer:
            for chunk in batches_of(cursor):
                arrays = [
                    pa.array([row[i] for row in chunk], type=schema.field(i).type)
                    for i in range(len(columns))
                ]
                writer.write_batch(pa.record_batch(arrays, schema=schema))
                rows += len(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)  # no half-written file is left to be mistaken for one
        raise
    tmp.replace(path)
    return rows


def package(manifest: Manifest, connection: Any, out_dir: Path,
            log: Callable[[str], None] = print) -> Plan:
    """Write every finished table's parquet(s) under `out_dir`, replacing the last."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ArtifactError(f"--artifacts needs pyarrow, which this Python lacks ({exc}).") from exc
    project_db = str(manifest.project.get("project_db") or "")
    if not project_db:
        raise ArtifactError("The manifest names no project_db, so there is nothing to read from.")
    result = plan(manifest)
    if out_dir.exists():
        shutil.rmtree(out_dir)  # each packaging replaces the last (D72)
    cursor = connection.cursor()
    for spec in result.tables:
        folder = out_dir / spec.folder
        folder.mkdir(parents=True, exist_ok=True)
        try:
            package_table(spec, cursor, pa, pq, project_db, folder, out_dir, result, log)
        except Exception as exc:  # noqa: BLE001 - one table's failure spares the rest (D88)
            # A driver error names the table in its SQL; say which table anyway.
            result.failed.append((spec.dest, f"{type(exc).__name__}: {exc}"))
            log(f"  FAILED   {spec.dest}: {type(exc).__name__}: {exc}")
            for part in spec.parts:
                if part.path is not None and part.path.exists():
                    part.path.unlink()  # a table is packaged whole or not at all
            spec.columns = []
            try:  # a failed statement can leave the cursor unusable
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
            cursor = connection.cursor()
    result.tables = [spec for spec in result.tables if spec.columns]
    return result


def pk_parquet_path(manifest_path: Path, cohort: dict[str, Any], doc: dict[str, Any], dest: str) -> Path:
    """Where Artifacts puts a PK's parquet, so the one written early is replaced."""
    return parquets_folder(manifest_path) / database_folder(cohort, doc) / file_name(dest, "")


def write_whole_table(connection: Any, project_db: str, table: str, path: Path) -> int:
    """One Projects table into one parquet, typed from its own columns (D87)."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ArtifactError(f"this Python lacks pyarrow ({exc})") from exc
    cursor = connection.cursor()
    described = describe(cursor, project_db, table)
    if not described:
        raise ArtifactError(f"{table} is not in {project_db}")
    columns = [(name, sql_type) for name, sql_type in described if name != BATCH_COLUMN]
    select = ", ".join(select_expression(name, sql_type) for name, sql_type in columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_part(cursor, pa, pq, f"SELECT {select} FROM {destination(project_db, table)};",
                      [], columns, path)


def package_table(spec: TableSpec, cursor: Any, pa: Any, pq: Any, project_db: str, folder: Path,
                  out_dir: Path, result: Plan, log: Callable[[str], None]) -> None:
    """Write one table's parquet(s), saying when each starts and how it went."""
    if spec.kind == "upload":
        part = spec.parts[0]
        part.path = folder / file_name(spec.dest, "")
        started = time.monotonic()
        shutil.copyfile(spec.source_file, part.path)
        part.rows = pq.read_metadata(part.path).num_rows
        part.seconds = time.monotonic() - started
        schema = pq.read_schema(part.path)
        spec.columns = [(name, str(schema.field(name).type)) for name in schema.names]
        log(f"  copied   {shown(part.path, out_dir)}  ({part.rows:,} rows, "
            f"{size_text(part.path)}, {seconds_text(part.seconds)})")
        return
    described = describe(cursor, project_db, spec.dest)
    if not described:
        result.left_out.append((spec.dest, f"it is not in {project_db}"))
        return
    spec.columns = [(name, sql_type) for name, sql_type in described if name != BATCH_COLUMN]
    select = ", ".join(select_expression(name, sql_type) for name, sql_type in spec.columns)
    for part in spec.parts:
        part.path = folder / file_name(spec.dest, part.label)
        log(f"  writing  {shown(part.path, out_dir)} ...")
        started = time.monotonic()
        sql = f"SELECT {select} FROM {destination(project_db, spec.dest)}{part.where};"
        part.rows = write_part(cursor, pa, pq, sql, part.params, spec.columns, part.path)
        part.seconds = time.monotonic() - started
        log(f"  wrote    {shown(part.path, out_dir)}  ({part.rows:,} rows, "
            f"{size_text(part.path)}, {seconds_text(part.seconds)})")


def size_text(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return ""


def seconds_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m {rest:02d}s" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def shown(path: Path, out_dir: Path) -> str:
    try:
        return str(path.relative_to(out_dir.parent))
    except ValueError:
        return str(path)


def parquets_folder(manifest_path: Path) -> Path:
    return run_folder(manifest_path) / PARQUETS_DIR
