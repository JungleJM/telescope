"""Upload cohorts: files into Projects, then up into a Cosmos global temp (D54).

Every upload lands in Projects first, as a typed table named `upload_<dest>`:
a parquet file read with pyarrow, or a `dbtable` copied server-side. That copy
is the source from then on. The Cosmos temp is loaded from it, an uploaded
PK's batches are drawn from it, and a resume or retry never re-reads the file,
so a file edited between runs cannot mix populations.

There is no linked server from Cosmos back to Projects, so the Cosmos side
travels through the client and lands via parameter binding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .naming import base_name
from .normalize import normalize_bool
from .sql import quote_name

UPLOAD_COPY_PREFIX = "upload_"

# Room above the widest value seen, so a later file with slightly longer
# values does not immediately fail.
LENGTH_HEADROOM = 50
MIN_COLUMN_WIDTH = 50
MAX_COLUMN_WIDTH = 4000

PYARROW_HINT = (
    "Reading parquet needs pyarrow, which the VM has (22.0.0). Elsewhere: "
    "`python -m pip install pyarrow==22.0.0`."
)

# Declared types an upload column can take, and the Arrow type each converts
# to. YAML Manager converts a CSV with the same table (D54).
DECLARED_TYPES = {
    "BIGINT": "int64", "INT": "int32", "INTEGER": "int32", "SMALLINT": "int16",
    "TINYINT": "uint8", "BIT": "bool", "FLOAT": "float64", "REAL": "float32",
    "DECIMAL": "decimal", "NUMERIC": "decimal", "DATE": "date32",
    "DATETIME": "timestamp", "DATETIME2": "timestamp", "SMALLDATETIME": "timestamp",
    "VARCHAR": "string", "NVARCHAR": "string", "CHAR": "string", "NCHAR": "string",
}
_SQL_TYPE = re.compile(r"^\s*([A-Za-z0-9]+)\s*(?:\(\s*(MAX|\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*$", re.I)


class UploadError(ValueError):
    """Raised when an upload cohort cannot be materialized."""


@dataclass
class UploadTable:
    """A file's rows, typed, ready to land in Projects."""

    name: str
    dest_table: str
    columns: list[tuple[str, str]]  # (name, SQL type)
    rows: list[tuple]
    notes: list[str] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [name for name, _ in self.columns]


def copy_table(dest_table: str | None) -> str:
    """The Projects copy of an upload: `upload_HospitalICDCodes`."""
    return UPLOAD_COPY_PREFIX + base_name(dest_table)


def upload_dest(cohort: dict[str, Any]) -> str:
    dest = str(cohort.get("dest_table") or cohort.get("name") or "")
    if not dest:
        raise UploadError("Upload cohort has neither dest_table nor name.")
    return dest


def enabled_uploads(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        upload for upload in doc.get("upload_cohorts") or []
        if isinstance(upload, dict)
        and normalize_bool(upload.get("push_this_cycle"), default=True)
    ]


def upload_kind(cohort: dict[str, Any]) -> str:
    kind = str(cohort.get("file_type") or "").strip().lower()
    if kind == "csv":
        raise UploadError(
            f"Upload cohort {cohort.get('name')!r} is still a CSV. Splits convert CSVs to "
            "parquet with their declared column types (D54); this one was made before "
            "that. Export the split again."
        )
    if kind not in ("parquet", "dbtable"):
        raise UploadError(
            f"Upload cohort {cohort.get('name')!r} has unsupported file_type "
            f"{cohort.get('file_type')!r}. Expected parquet or dbtable."
        )
    return kind


def _pyarrow():
    try:
        import pyarrow
        import pyarrow.compute
        import pyarrow.parquet
    except ImportError as exc:
        raise UploadError(PYARROW_HINT) from exc
    return pyarrow, pyarrow.compute, pyarrow.parquet


def arrow_type_for(sql_type: str):
    """The Arrow type a declared SQL type converts a column to."""
    pa = _pyarrow()[0]
    match = _SQL_TYPE.match(str(sql_type or ""))
    if not match or match.group(1).upper() not in DECLARED_TYPES:
        raise UploadError(
            f"`{sql_type}` is not a type an upload column can take. Use one of: "
            + ", ".join(sorted(DECLARED_TYPES))
        )
    kind = DECLARED_TYPES[match.group(1).upper()]
    if kind == "decimal":
        return pa.decimal128(int(match.group(2) or 18), int(match.group(3) or 0))
    if kind == "timestamp":
        return pa.timestamp("us")
    if kind == "bool":
        return pa.bool_()
    return getattr(pa, kind)()


def _string_width(column) -> int:
    pa, pc, _ = _pyarrow()
    longest = pc.max(pc.utf8_length(pc.cast(column, pa.string()))).as_py() or 0
    return max(MIN_COLUMN_WIDTH, longest + LENGTH_HEADROOM)


def sql_type_for(name: str, column) -> str:
    """The SQL type a parquet column lands as, from its Arrow type."""
    pa = _pyarrow()[0]
    t = column.type
    types = pa.types
    if types.is_dictionary(t):
        t = t.value_type
    if types.is_int8(t) or types.is_int16(t):
        return "SMALLINT"
    if types.is_uint8(t):
        return "TINYINT"
    if types.is_int32(t) or types.is_uint16(t):
        return "INT"
    if types.is_int64(t) or types.is_uint32(t):
        return "BIGINT"
    if types.is_uint64(t):
        return "DECIMAL(20,0)"
    if types.is_boolean(t):
        return "BIT"
    if types.is_float16(t) or types.is_float32(t):
        return "REAL"
    if types.is_float64(t):
        return "FLOAT"
    if types.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    if types.is_date(t):
        return "DATE"
    if types.is_timestamp(t):
        return "DATETIME2(7)"
    if types.is_time(t):
        return "TIME(7)"
    if types.is_null(t):
        return f"NVARCHAR({MIN_COLUMN_WIDTH})"
    if types.is_string(t) or types.is_large_string(t):
        width = _string_width(column)
        return f"NVARCHAR({width})" if width <= MAX_COLUMN_WIDTH else "NVARCHAR(MAX)"
    raise UploadError(
        f"Column `{name}` is {t}, which cannot be uploaded. Convert it to text, a "
        "number or a date in the file."
    )


def read_parquet(cohort: dict[str, Any], root: Path) -> UploadTable:
    """Read a parquet upload, applying what `columns:` declares: types, renames
    and dropped columns (D54, D98)."""
    pa, pc, pq = _pyarrow()
    dest = upload_dest(cohort)
    file_loc = cohort.get("file_loc")
    if not file_loc:
        raise UploadError(f"Upload cohort {dest!r} is file_type parquet but has no file_loc.")
    path = Path(str(file_loc))
    path = path if path.is_absolute() else root / path
    if not path.is_file():
        raise UploadError(f"Upload file not found: {path}")
    table = pq.read_table(str(path))
    entries = [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    # An entry names the file's column by `from:`, else by `name` (D98).
    declared = {
        str(c.get("from") or c["name"]): str(c["type"])
        for c in entries
        if c.get("type") and c.get("drop") is not True
    }
    renames = {str(c["from"]): str(c["name"]) for c in entries if c.get("from") and c.get("drop") is not True}
    dropped = {str(c.get("from") or c["name"]) for c in entries if c.get("drop") is True}
    missing = [name for name in [*declared, *renames, *sorted(dropped)] if name not in table.column_names]
    if missing:
        raise UploadError(
            f"Upload {dest!r}: declared column(s) {', '.join(dict.fromkeys(missing))} are not in "
            f"{path.name} ({', '.join(table.column_names)})."
        )
    landed = [renames.get(name, name) for name in table.column_names if name not in dropped]
    repeated = sorted({name for name in landed if landed.count(name) > 1})
    if repeated:
        raise UploadError(
            f"Upload {dest!r}: more than one column would be called {', '.join(repeated)}. "
            "Rename or drop one under `columns:`."
        )
    notes: list[str] = []
    columns: list[tuple[str, str]] = []
    kept: list[Any] = []
    for index, name in enumerate(table.column_names):
        if name in dropped:
            continue
        column = table.column(index)
        target = renames.get(name, name)
        if name in declared:
            try:
                column = pc.cast(column, arrow_type_for(declared[name]), safe=True)
            except pa.ArrowInvalid as exc:
                raise UploadError(
                    f"Upload {dest!r}, column `{name}`: {str(exc).splitlines()[0]}. Fix the "
                    f"value in {path.name}, or declare a type that fits."
                ) from exc
            columns.append((target, declared[name].upper()))
            kept.append(column)
            continue
        if pa.types.is_timestamp(column.type) and column.type.tz is not None:
            # SQL Server's DATETIME2 has no zone; land UTC and say so.
            zone = column.type
            column = pc.cast(column, pa.timestamp(column.type.unit))
            notes.append(f"{dest}.{target} had time zone {zone}; landed as UTC.")
        columns.append((target, sql_type_for(target, column)))
        kept.append(column)
    values = [column.to_pylist() for column in kept]
    rows = list(zip(*values)) if values else []
    if not rows:
        notes.append(f"{path.name} has no rows; landing an empty table.")
    return UploadTable(name=str(cohort.get("name") or dest), dest_table=dest,
                       columns=columns, rows=rows, notes=notes)


def column_ddl(columns: list[tuple[str, str]]) -> str:
    return ",\n".join(f"    {quote_name(name)} {sql_type} NULL" for name, sql_type in columns)


def render_create(table: str, columns: list[tuple[str, str]]) -> str:
    """Drop and create a table, a Projects copy or a Cosmos temp, with these columns."""
    return f"DROP TABLE IF EXISTS {table};\n\nCREATE TABLE {table}\n(\n{column_ddl(columns)}\n);"


def render_copy_dbtable(copy_fq: str, source_fq: str) -> str:
    """A dbtable upload's copy, made server-side, types and all."""
    return f"DROP TABLE IF EXISTS {copy_fq};\n\nSELECT * INTO {copy_fq} FROM {source_fq};"


def describe_sql(project_db: str, table: str) -> tuple[str, list[Any]]:
    """The columns of a Projects table, in order, with their types."""
    return (
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
        "NUMERIC_SCALE, DATETIME_PRECISION "
        f"FROM {project_db}.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION;",
        [base_name(table)],
    )


def type_from_info(data_type: Any, length: Any, precision: Any, scale: Any, dt_precision: Any) -> str:
    """Rebuild a column's SQL type from INFORMATION_SCHEMA.COLUMNS."""
    base = str(data_type).upper()
    if base in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "VARBINARY", "BINARY"):
        return f"{base}({'MAX' if length in (-1, None) else int(length)})"
    if base in ("DECIMAL", "NUMERIC"):
        return f"DECIMAL({int(precision)},{int(scale or 0)})"
    if base in ("DATETIME2", "TIME", "DATETIMEOFFSET") and dt_precision is not None:
        return f"{base}({int(dt_precision)})"
    return base
