"""Projects-side SQL: destination tables and the transfer from Cosmos.

Write mode is decided: the destination is dropped and created once per session
in the setup phase, and every run appends. The old generator dropped inside
each transfer block, which with batching leaves only the last batch.
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


def render_table_shell(cohort: dict[str, Any], project_db: str) -> str:
    """Drop and create one destination table. Runs once per session."""
    dest = cohort.get("dest_table")
    if not dest:
        raise LocalRenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
    columns = _columns(cohort)
    if not columns:
        raise LocalRenderError(f"Cohort {dest!r} declares no columns.")
    table = destination(project_db, dest)
    return (
        f"-- session table shell for {dest}\n"
        f"DROP TABLE IF EXISTS {table};\n\n"
        f"CREATE TABLE {table}\n(\n{ddl_body(columns)}\n);"
    )


def render_transfer(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
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
        f"INSERT INTO {table} ({cols})\n"
        f"SELECT {cols} FROM {staging};\n"
        f"COMMIT TRANSACTION;"
    )


def render_row_counts(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
    """Both sides of the transfer, so a mismatch is visible."""
    dest = str(cohort["dest_table"])
    table = destination(project_db, dest)
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
        f"FROM {table};"
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


def render_setup(doc: dict[str, Any], cohorts: list[dict[str, Any]], block_prefix: str) -> list[SqlBlock]:
    """One shell block per destination table for the whole session."""
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
                sql=render_table_shell(cohort, str(project_db)),
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
    blocks: list[SqlBlock] = []
    for cohort in doc.get("cohorts") or []:
        if not isinstance(cohort, dict) or not cohort.get("dest_table"):
            continue
        if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
            continue
        dest = str(cohort["dest_table"])
        parts = [
            render_transfer(cohort, str(project_db), linked_server),
            render_row_counts(cohort, str(project_db), linked_server),
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
