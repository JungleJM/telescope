"""The dictionary audit: what the dictionary lists and Cosmos lacks (D155),
and each column's type and nullability where they differ from Cosmos's (D161).

`MedicationDispenseFact.ReadyToDispenseDateKey` was in the dictionary and not
in Cosmos, and a pull failed on it after an eleven-minute query. This asks
Cosmos for each dictionary table's columns and writes
`runs/dictionary_audit.yaml` with only what did not check out, short enough
to be copied off the VM by screenshot and read back by `scope.py
dictionary-fix` on the Mac.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AUDIT_FILENAME = "dictionary_audit.yaml"
COLUMNS_SQL = (
    "SELECT c.name, TYPE_NAME(c.user_type_id), c.max_length, c.precision, c.scale, "
    "c.is_nullable FROM sys.columns AS c WHERE c.object_id = OBJECT_ID(?);"
)
# A type as a dictionary page writes it: `nvarchar(300)`, `numeric(19,4)`,
# `tinyint (flag)`; what follows the size is an annotation.
_TYPE = re.compile(r"^\s*(?P<base>[A-Za-z][\w/]*)\s*(?:\(\s*(?P<size>\d+\s*(?:,\s*\d+)?|max)\s*\))?",
                   re.IGNORECASE)
_DEFAULT_SCALE = {"datetime2": "7", "time": "7", "datetimeoffset": "7"}


@dataclass
class Column:
    name: str
    type: str
    nullable: bool


@dataclass
class Audit:
    database: str
    checked: int = 0
    tables_not_found: list[str] = field(default_factory=list)
    # table -> [(column, its near matches in Cosmos)]
    missing: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)
    # table -> {column: Cosmos's type}, and {column: Cosmos's nullability}
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    nullable: dict[str, dict[str, bool]] = field(default_factory=dict)

    @property
    def wrong(self) -> int:
        return (len(self.tables_not_found) + sum(len(v) for v in self.missing.values())
                + sum(len(v) for v in self.types.values())
                + sum(len(v) for v in self.nullable.values()))


def sql_type(name: Any, max_length: Any, precision: Any, scale: Any) -> str:
    """Cosmos's type as the page writes it: `nvarchar(300)`, `numeric(19,4)`, `bigint`."""
    base = str(name or "").lower()
    if base in ("nvarchar", "nchar"):
        return f"{base}({'max' if int(max_length) == -1 else int(max_length) // 2})"
    if base in ("varchar", "char", "varbinary", "binary"):
        return f"{base}({'max' if int(max_length) == -1 else int(max_length)})"
    if base in ("decimal", "numeric"):
        return f"{base}({int(precision)},{int(scale)})"
    if base in _DEFAULT_SCALE and str(int(scale)) != _DEFAULT_SCALE[base]:
        return f"{base}({int(scale)})"
    return base


def same_type(dictionary_type: Any, cosmos: str) -> bool:
    """Whether the dictionary's type is Cosmos's, its annotation aside."""
    match = _TYPE.match(str(dictionary_type or ""))
    if not match:
        return False
    base, size = match.group("base").lower(), match.group("size")
    size = re.sub(r"\s+", "", size).lower() if size else ""
    if size and size == _DEFAULT_SCALE.get(base):
        size = ""
    return (f"{base}({size})" if size else base) == cosmos


def cosmos_columns(connection: Any, table: str) -> list[Column]:
    """The columns Cosmos has for a table; none when it has no such table, or
    this login cannot see it."""
    cursor = connection.cursor()
    cursor.execute(COLUMNS_SQL, [f"dbo.{table}"])
    return [Column(str(row[0]), sql_type(*row[1:5]), bool(row[5])) for row in cursor.fetchall()]


def near(column: str, columns: list[str]) -> list[str]:
    """Close spellings, `_X` first: Epic's suffix is the usual difference."""
    suffixed = [c for c in columns if c.lower() in (f"{column.lower()}_x", column.lower().removesuffix("_x"))]
    close = difflib.get_close_matches(column, columns, n=2, cutoff=0.8)
    return list(dict.fromkeys(suffixed + close))


def audit_dictionary(connection: Any, dictionary: dict[str, Any], database: str) -> Audit:
    result = Audit(database)
    for table, entry in dictionary.items():
        if not isinstance(entry, dict):
            continue
        listed = entry.get("columns") or {}
        result.checked += 1
        found = cosmos_columns(connection, str(table))
        if not found:
            result.tables_not_found.append(str(table))
            continue
        have = {c.name.lower(): c for c in found}
        names = [c.name for c in found]
        gone = [(str(c), near(str(c), names)) for c in listed if str(c).lower() not in have]
        if gone:
            result.missing[str(table)] = gone
        for name, meta in listed.items():
            cosmos = have.get(str(name).lower())
            if cosmos is None:
                continue
            meta = meta if isinstance(meta, dict) else {}
            if not same_type(meta.get("type"), cosmos.type):
                result.types.setdefault(str(table), {})[str(name)] = cosmos.type
            # No `nullable` is nullable, as a pull reads it.
            if meta.get("nullable", True) is not False and not cosmos.nullable:
                result.nullable.setdefault(str(table), {})[str(name)] = False
            elif meta.get("nullable", True) is False and cosmos.nullable:
                result.nullable.setdefault(str(table), {})[str(name)] = True
    return result


def _flow(values: dict[str, Any]) -> str:
    """`{A: nvarchar(50), B: "numeric(19,4)"}`: a comma inside a value is quoted."""
    def text(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return f'"{value}"' if "," in str(value) else str(value)
    return "{" + ", ".join(f"{k}: {text(v)}" for k, v in values.items()) + "}"


def report(result: Audit, bundle: str = "") -> str:
    header = (f"# dictionary audit, {time.strftime('%Y-%m-%d %H:%M')}"
              f"{f', bundle {bundle}' if bundle else ''}, database {result.database}: "
              f"{result.checked} tables, {result.wrong} wrong")
    lines = [header]
    if not result.wrong:
        lines.append("the dictionary matches Cosmos")
        return "\n".join(lines) + "\n"
    if result.tables_not_found:
        lines.append("tables_not_found:  # the dictionary names them; Cosmos has no such "
                     "table, or this login cannot see it")
        lines.extend(f"  - {table}" for table in result.tables_not_found)
    if result.missing:
        lines.append("columns_not_in_cosmos:  # remove these from the dictionary")
        for table, gone in result.missing.items():
            nears = "; ".join(f"{c} -> {', '.join(n)}" for c, n in gone if n)
            line = f"  {table}: [{', '.join(c for c, _ in gone)}]"
            lines.append(line + (f"  # near: {nears}" if nears else ""))
    if result.types:
        lines.append("types_wrong:  # Cosmos's type; dictionary-fix writes it in")
        lines.extend(f"  {table}: {_flow(columns)}" for table, columns in result.types.items())
    if result.nullable:
        lines.append("nullable_wrong:  # Cosmos's nullability; dictionary-fix writes it in")
        lines.extend(f"  {table}: {_flow(columns)}" for table, columns in result.nullable.items())
    return "\n".join(lines) + "\n"


def write_report(result: Audit, runs: Path, bundle: str = "") -> tuple[Path, str]:
    text = report(result, bundle)
    path = Path(runs) / AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text
