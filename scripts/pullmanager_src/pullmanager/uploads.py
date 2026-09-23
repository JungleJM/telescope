"""Upload cohorts: getting local data up into a Cosmos global temp.

There is no linked server from Cosmos back to Projects, so everything here
travels through the client and lands via parameter binding.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .db import DatabaseError, bulk_insert, execute_script
from .naming import global_temp
from .normalize import normalize_bool

# Room above the widest value seen, so a later file with slightly longer
# values does not immediately fail.
LENGTH_HEADROOM = 50
MIN_COLUMN_WIDTH = 50
MAX_COLUMN_WIDTH = 4000

_LEADING_DIGIT = re.compile(r"^\d")
_UNSAFE = re.compile(r"[^\w]+")


class UploadError(ValueError):
    """Raised when an upload cohort cannot be materialized."""


@dataclass
class UploadPlan:
    name: str
    dest_table: str
    global_temp: str
    columns: list[str]
    rows: list[tuple]
    widths: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def safe_identifier(header: str, position: int) -> str:
    """Turn a CSV header into something SQL can name."""
    cleaned = _UNSAFE.sub("_", str(header or "").strip()).strip("_")
    if not cleaned:
        cleaned = f"Column{position + 1}"
    if _LEADING_DIGIT.match(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned


def read_csv(path: Path) -> tuple[list[str], list[tuple]]:
    """Read a CSV, BOM-safe, with headers normalized to SQL identifiers."""
    if not path.is_file():
        raise UploadError(f"Upload file not found: {path}")
    # utf-8-sig strips a byte order mark, which otherwise becomes part of the
    # first column name and silently breaks every reference to it.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise UploadError(f"Upload file is empty: {path}") from None
        columns = [safe_identifier(name, i) for i, name in enumerate(header)]
        if not columns:
            raise UploadError(f"Upload file has no header columns: {path}")
        seen: dict[str, int] = {}
        for index, name in enumerate(columns):
            if name in seen:
                seen[name] += 1
                columns[index] = f"{name}_{seen[name]}"
            else:
                seen[name] = 0
        width = len(columns)
        rows: list[tuple] = []
        for record in reader:
            if not any(str(cell).strip() for cell in record):
                continue
            padded = list(record[:width]) + [None] * max(0, width - len(record))
            rows.append(tuple(cell if str(cell) != "" else None for cell in padded))
    return columns, rows


def measure_widths(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, int]:
    """Size each column from the data actually present.

    This works for an upload because the whole file is in hand before the table
    is created. It does not work for a batched pull, where the table exists
    before any batch runs.
    """
    widths = {name: MIN_COLUMN_WIDTH for name in columns}
    for row in rows:
        for name, value in zip(columns, row):
            if value is None:
                continue
            widths[name] = max(widths[name], len(str(value)) + LENGTH_HEADROOM)
    return {name: min(width, MAX_COLUMN_WIDTH) for name, width in widths.items()}


def resolve_path(file_loc: str, root: Path) -> Path:
    candidate = Path(file_loc)
    return candidate if candidate.is_absolute() else (root / candidate)


def plan_csv_upload(cohort: dict[str, Any], root: Path) -> UploadPlan:
    dest = str(cohort.get("dest_table") or cohort.get("name") or "")
    if not dest:
        raise UploadError("Upload cohort has neither dest_table nor name.")
    file_loc = cohort.get("file_loc")
    if not file_loc:
        raise UploadError(f"Upload cohort {dest!r} is file_type csv but has no file_loc.")
    path = resolve_path(str(file_loc), root)
    columns, rows = read_csv(path)
    plan = UploadPlan(
        name=str(cohort.get("name") or dest),
        dest_table=dest,
        global_temp=global_temp(dest),
        columns=columns,
        rows=rows,
        widths=measure_widths(columns, rows),
    )
    if not rows:
        plan.notes.append(f"{path.name} has a header but no data rows; uploading an empty table.")
    return plan


def render_create(plan: UploadPlan) -> str:
    body = ",\n".join(
        f"    [{name}] NVARCHAR({plan.widths.get(name, MIN_COLUMN_WIDTH)}) NULL"
        for name in plan.columns
    )
    return (
        f"DROP TABLE IF EXISTS {plan.global_temp};\n\n"
        f"CREATE TABLE {plan.global_temp}\n(\n{body}\n);"
    )


def materialize(connection: Any, plan: UploadPlan, *, chunk_size: int) -> int:
    """Create the temp table and bind the rows into it."""
    execute_script(connection, render_create(plan), label=f"upload {plan.dest_table}")
    if not plan.rows:
        return 0
    try:
        return bulk_insert(
            connection,
            plan.global_temp,
            plan.columns,
            plan.rows,
            chunk_size=chunk_size,
        )
    except DatabaseError as exc:
        raise UploadError(f"Upload of {plan.dest_table!r} failed: {exc}") from exc


def plan_dbtable_upload(
    projects_connection: Any, cohort: dict[str, Any], project_db: str
) -> UploadPlan:
    """Read an existing Projects table and carry it up through the client."""
    dest = str(cohort.get("dest_table") or cohort.get("name") or "")
    source = str(cohort.get("source_table") or dest)
    cursor = projects_connection.cursor()
    cursor.execute(f"SELECT * FROM {project_db}.dbo.{source};")
    columns = [column[0] for column in cursor.description or []]
    rows = [tuple(row) for row in cursor.fetchall()]
    return UploadPlan(
        name=str(cohort.get("name") or dest),
        dest_table=dest,
        global_temp=global_temp(dest),
        columns=columns,
        rows=rows,
        widths=measure_widths(columns, rows),
    )


def enabled_uploads(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        upload for upload in doc.get("upload_cohorts") or []
        if isinstance(upload, dict)
        and normalize_bool(upload.get("push_this_cycle"), default=True)
    ]


def upload_kind(cohort: dict[str, Any]) -> str:
    kind = str(cohort.get("file_type") or "").strip().lower()
    if kind == "parquet":
        raise UploadError(
            f"Upload cohort {cohort.get('name')!r} is file_type parquet, which Cosmos "
            "cannot read. Convert it to CSV, or load it into Projects and use dbtable."
        )
    if kind not in ("csv", "dbtable"):
        raise UploadError(
            f"Upload cohort {cohort.get('name')!r} has unsupported file_type "
            f"{cohort.get('file_type')!r}. Expected csv or dbtable."
        )
    return kind
