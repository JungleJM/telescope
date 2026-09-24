"""Projects-side SQL: destination tables and the transfer from Cosmos.

Write mode is decided: the destination is dropped and created once per session
in the setup phase, and every run appends. The old generator dropped inside
each transfer block, which with batching leaves only the last batch.

A session that resumes (D52) keeps its destinations: setup then creates only
the tables that are missing. Every row a run lands carries its batch label in
`_batch`, so a run first deletes its own label's rows, which makes a retry
after a partial failure land each batch exactly once, and counts only its own
rows when checking the transfer.
"""

from __future__ import annotations

from typing import Any

from .naming import destination, global_temp, local_staging
from .normalize import normalize_bool
from .sql import (
    SqlBlock,
    column_list,
    column_names,
    ddl_body,
    quote_literal,
    quote_name,
)

BATCH_COLUMN = "_batch"
BATCH_COLUMN_TYPE = "NVARCHAR(200) NOT NULL"
UNBATCHED_LABEL = "all"

# Types whose stored length is worth measuring so templates can be tuned.
_MEASURABLE = ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR")


class LocalRenderError(ValueError):
    """Raised when local SQL cannot be rendered."""


def _columns(cohort: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]


def _remote_query(dest: str, columns: list[dict[str, Any]]) -> str:
    """The inner query sent to the linked server, quoted for embedding."""
    cols = ", ".join(quote_name(n) for n in column_names(columns))
    inner = f"SELECT {cols} FROM {global_temp(dest)}"
    return inner.replace("'", "''")


def batch_label(doc: dict[str, Any]) -> str | None:
    """The `_batch` value a run document's rows carry; None outside a run."""
    context = doc.get("pull_context") or {}
    if context.get("phase") != "run":
        return None
    return str((context.get("batch") or {}).get("name") or UNBATCHED_LABEL)


def render_table_shell(
    cohort: dict[str, Any],
    project_db: str,
    *,
    keep: bool = False,
    batch_column: bool = False,
) -> str:
    """Create one destination table. Runs once per session.

    Dropped first on a fresh session. Kept on a resume, since it holds the rows
    of the batches that already finished, and then only created if missing.
    """
    dest = cohort.get("dest_table")
    if not dest:
        raise LocalRenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
    columns = _columns(cohort)
    if not columns:
        raise LocalRenderError(f"Cohort {dest!r} declares no columns.")
    table = destination(project_db, dest)
    body = ddl_body(columns)
    if batch_column:
        body += f",\n    {quote_name(BATCH_COLUMN)} {BATCH_COLUMN_TYPE}"
    create = f"CREATE TABLE {table}\n(\n{body}\n);"
    if keep:
        return (
            f"-- session table shell for {dest}, kept: finished batches are in it\n"
            f"IF OBJECT_ID(N{quote_literal(table)}, N'U') IS NULL\n{create}"
        )
    return (
        f"-- session table shell for {dest}\n"
        f"DROP TABLE IF EXISTS {table};\n\n"
        f"{create}"
    )


def render_delete_batch(cohort: dict[str, Any], project_db: str, label: str) -> str:
    """Remove a run's rows before it lands them, so a retry cannot double them."""
    table = destination(project_db, str(cohort["dest_table"]))
    return (
        f"-- clear {label} from {table} before it is pulled\n"
        f"DELETE FROM {table} WHERE {quote_name(BATCH_COLUMN)} = {quote_literal(label)};"
    )


def render_transfer(
    cohort: dict[str, Any], project_db: str, linked_server: str, label: str | None = None
) -> str:
    """Pull one cohort from its global temp into the destination table.

    The slow OPENQUERY lands in a staging table outside the transaction, so no
    lock is held while data crosses the linked server. Only the final insert is
    transactional, which is what makes a retry after a partial failure safe.
    """
    if not linked_server:
        raise LocalRenderError(
            "No linked server supplied. The Cosmos instance name changes on every "
            "connection and must come from the current session."
        )
    dest = str(cohort["dest_table"])
    columns = _columns(cohort)
    table = destination(project_db, dest)
    staging = local_staging(dest)
    cols = column_list(columns)
    insert_cols, select_cols = cols, cols
    if label is not None:
        insert_cols = f"{cols}, {quote_name(BATCH_COLUMN)}"
        select_cols = f"{cols}, {quote_literal(label)}"

    return (
        f"-- transfer {global_temp(dest)} -> {table}\n"
        f"DROP TABLE IF EXISTS {staging};\n\n"
        f"SELECT {cols}\n"
        f"INTO {staging}\n"
        f"FROM OPENQUERY(\n"
        f"    [{linked_server}],\n"
        f"    '{_remote_query(dest, columns)}'\n"
        f");\n\n"
        f"BEGIN TRANSACTION;\n"
        f"INSERT INTO {table} ({insert_cols})\n"
        f"SELECT {select_cols} FROM {staging};\n"
        f"COMMIT TRANSACTION;"
    )


def render_row_counts(
    cohort: dict[str, Any], project_db: str, linked_server: str, label: str | None = None
) -> str:
    """Both sides of the transfer, so a mismatch is visible.

    In a run, the Projects side counts only this batch's rows: the destination
    also holds every earlier batch, which the Cosmos temp does not.
    """
    dest = str(cohort["dest_table"])
    table = destination(project_db, dest)
    where = (
        f"\nWHERE {quote_name(BATCH_COLUMN)} = {quote_literal(label)}" if label is not None else ""
    )
    remote = f"SELECT 1 AS dummy FROM {global_temp(dest)}".replace("'", "''")
    return (
        "SELECT\n"
        f"    {quote_literal(dest)} AS [DestTable],\n"
        "    'cosmos' AS [Side],\n"
        "    COUNT_BIG(1) AS [RowCount]\n"
        f"FROM OPENQUERY([{linked_server}], '{remote}');\n\n"
        "SELECT\n"
        f"    {quote_literal(dest)} AS [DestTable],\n"
        "    'projects' AS [Side],\n"
        "    COUNT_BIG(1) AS [RowCount]\n"
        f"FROM {table}{where};"
    )


def render_length_probe(cohort: dict[str, Any]) -> str | None:
    """Measure the widest value actually stored in each string column.

    Reported rather than applied: the destination table is created in setup,
    before any data exists, and sizing it from one batch would truncate a later
    batch carrying a longer value.
    """
    dest = str(cohort["dest_table"])
    staging = local_staging(dest)
    measurable = [
        c for c in _columns(cohort)
        if str(c.get("type", "")).split("(")[0].strip().upper() in _MEASURABLE
    ]
    if not measurable:
        return None
    selects = [
        "SELECT\n"
        f"    {quote_literal(dest)} AS [DestTable],\n"
        f"    {quote_literal(str(c['name']))} AS [Column],\n"
        f"    {quote_literal(str(c.get('type')))} AS [DeclaredType],\n"
        f"    MAX(LEN({quote_name(str(c['name']))})) AS [MaxLength]\n"
        f"FROM {staging}"
        for c in measurable
    ]
    return "\n UNION ALL\n".join(selects) + ";"


def render_setup(
    doc: dict[str, Any],
    cohorts: list[dict[str, Any]],
    block_prefix: str,
    *,
    keep: bool = False,
    batched: frozenset[str] | set[str] = frozenset(),
) -> list[SqlBlock]:
    """One shell block per destination table for the whole session.

    `batched` names the destinations runs fill, which get the `_batch` column;
    the PK's own destination is not one, since batches are selected from it.
    """
    project_db = doc.get("project_db")
    if not project_db:
        raise LocalRenderError("Phase document has no `project_db`.")
    blocks = []
    for cohort in cohorts:
        if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
            continue
        blocks.append(
            SqlBlock(
                block_id=f"{block_prefix}/shell/{cohort['dest_table']}",
                side="local",
                sql=render_table_shell(
                    cohort,
                    str(project_db),
                    keep=keep,
                    batch_column=str(cohort["dest_table"]) in batched,
                ),
                dest_table=str(cohort["dest_table"]),
                meta={"destination": destination(str(project_db), cohort["dest_table"])},
            )
        )
    return blocks


def render_phase(doc: dict[str, Any], block_prefix: str, linked_server: str) -> list[SqlBlock]:
    """Transfer, counts and measurement for every cohort in a phase."""
    project_db = doc.get("project_db")
    if not project_db:
        raise LocalRenderError("Phase document has no `project_db`.")
    label = batch_label(doc)
    blocks: list[SqlBlock] = []
    for cohort in doc.get("cohorts") or []:
        if not isinstance(cohort, dict) or not cohort.get("dest_table"):
            continue
        if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
            continue
        dest = str(cohort["dest_table"])
        if label is not None:
            # Its own block: a chunked run clears once, then lands every chunk.
            blocks.append(
                SqlBlock(
                    block_id=f"{block_prefix}/{dest}/clear",
                    side="local",
                    sql=render_delete_batch(cohort, str(project_db), label),
                    dest_table=dest,
                    meta={"clears": label, "destination": destination(str(project_db), dest)},
                )
            )
        parts = [
            render_transfer(cohort, str(project_db), linked_server, label),
            render_row_counts(cohort, str(project_db), linked_server, label),
        ]
        probe = render_length_probe(cohort)
        if probe:
            parts.append(probe)
        blocks.append(
            SqlBlock(
                block_id=f"{block_prefix}/{dest}",
                side="local",
                sql="\n\n".join(parts),
                dest_table=dest,
                meta={
                    "destination": destination(str(project_db), dest),
                    "staging": local_staging(dest),
                    "global_temp": global_temp(dest),
                    "linked_server": linked_server,
                },
            )
        )
    return blocks
