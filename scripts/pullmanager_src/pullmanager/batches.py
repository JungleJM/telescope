"""Turning a logical batch into the rows it selects.

Batch membership is decided against the durable Projects copy of the PK table,
never against Cosmos. That copy does not change under a refresh, so a batch
means the same rows on a resume as it did on the night it first ran -- and a
fresh run and a resume take the same path, so recovery is exercised nightly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .naming import destination
from .sql import hash_order


class BatchError(ValueError):
    """Raised when a batch cannot be turned into a selection."""


@dataclass
class BatchSelection:
    """A parameterized SELECT over the local PK table."""

    sql: str
    params: list[Any] = field(default_factory=list)
    description: str = ""


def dimension_predicate(dimension: dict[str, Any]) -> tuple[str, list[Any]]:
    column = dimension.get("column")
    if not column:
        raise BatchError(f"Batch dimension {dimension.get('name')!r} names no column.")
    if dimension.get("is_other"):
        excludes = dimension.get("excludes") or []
        if not excludes:
            raise BatchError(
                f"Batch dimension {dimension.get('name')!r} is the catch-all but lists "
                "nothing to exclude, so it would select every row."
            )
        placeholders = ", ".join("?" for _ in excludes)
        # NULL is not 'not in' anything in SQL, so include it explicitly or the
        # catch-all silently drops rows with no value.
        return f"([{column}] NOT IN ({placeholders}) OR [{column}] IS NULL)", list(excludes)
    if "value" not in dimension:
        raise BatchError(f"Batch dimension {dimension.get('name')!r} has no value.")
    return f"[{column}] = ?", [dimension["value"]]


def chunk_clause(batch: dict[str, Any], key_columns: list[str]) -> tuple[str, str]:
    """OFFSET/FETCH for a row chunk, plus the ordering that makes it stable.

    A chunk is only reproducible if the ordering is a total order, which is why
    the PK's uniqueness is verified before any batch runs.
    """
    runtime = batch.get("runtime") or []
    chunks = [d for d in runtime if str(d.get("kind", "")).lower() == "row_chunk"]
    if not chunks:
        return "", ""
    if len(chunks) > 1:
        raise BatchError("More than one row_chunk dimension in a single batch.")
    unresolved = [
        d for d in runtime
        if str(d.get("kind", "")).lower() == "column_values"
    ]
    if unresolved:
        names = ", ".join(str(d.get("name")) for d in unresolved)
        raise BatchError(
            f"Batch dimension(s) {names} use `values: all`, which has to be resolved "
            "against real data before the batch set is known. Not yet supported; "
            "list the values explicitly in the template."
        )
    if not key_columns:
        raise BatchError("Row chunking needs the PK key columns to order by.")
    order = ", ".join(f"[{c}]" for c in key_columns)
    size = chunks[0].get("rows_per_batch")
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise BatchError(f"row_chunk has a non-numeric rows_per_batch: {size!r}") from None
    if size <= 0:
        raise BatchError(f"row_chunk rows_per_batch must be positive, got {size}.")
    return order, str(size)


def select_batch_rows(
    project_db: str,
    pk_table: str,
    batch: dict[str, Any] | None,
    key_columns: list[str],
    *,
    chunk_index: int = 0,
) -> BatchSelection:
    """Every PK row belonging to one batch.

    Whole rows, not just keys: batching selects on PK attributes such as Sex
    and StateOrProvinceAbbreviation, and cohort joins may use them too.
    """
    table = destination(project_db, pk_table)
    if not batch:
        return BatchSelection(sql=f"SELECT * FROM {table};", description="whole PK table")

    predicates: list[str] = []
    params: list[Any] = []
    described: list[str] = []
    for dimension in batch.get("dimensions") or []:
        clause, values = dimension_predicate(dimension)
        predicates.append(clause)
        params.extend(values)
        described.append(
            f"{dimension.get('column')}="
            + ("other" if dimension.get("is_other") else str(dimension.get("value")))
        )

    sql = f"SELECT * FROM {table}"
    if predicates:
        sql += "\nWHERE " + "\n  AND ".join(predicates)

    order, size = chunk_clause(batch, key_columns)
    if order:
        offset = chunk_index * int(size)
        sql += f"\nORDER BY {order}\nOFFSET {offset} ROWS FETCH NEXT {size} ROWS ONLY"
        described.append(f"rows {offset}-{offset + int(size)}")

    return BatchSelection(
        sql=sql + ";",
        params=params,
        description=", ".join(described) or "whole PK table",
    )


def batch_where(batch: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """` WHERE ...` selecting a batch's rows (empty for no batch), with its params."""
    predicates: list[str] = []
    params: list[Any] = []
    for dimension in (batch or {}).get("dimensions") or []:
        clause, values = dimension_predicate(dimension)
        predicates.append(clause)
        params.extend(values)
    return (" WHERE " + " AND ".join(predicates) if predicates else ""), params


def count_batch_rows(project_db: str, pk_table: str, batch: dict[str, Any] | None) -> BatchSelection:
    """How many PK rows a batch's predicate matches, before chunking."""
    where, params = batch_where(batch)
    return BatchSelection(
        sql=f"SELECT COUNT_BIG(1) FROM {destination(project_db, pk_table)}{where};", params=params
    )


def sample_down(
    project_db: str,
    pk_table: str,
    batch: dict[str, Any] | None,
    key_columns: list[str],
    keep: int,
) -> BatchSelection:
    """Delete a batch's PK rows beyond the first `keep` in hash order (D59).

    The rows kept are a pseudo-random sample, the same on every run (D60).
    """
    if not key_columns:
        raise BatchError("Sampling needs the PK's key columns to order by.")
    where, params = batch_where(batch)
    order = hash_order([f"[{c}]" for c in key_columns])
    return BatchSelection(
        sql=(
            "WITH [_ranked] AS (\n"
            f"    SELECT ROW_NUMBER() OVER (ORDER BY {order}) AS [_sample_rn]\n"
            f"    FROM {destination(project_db, pk_table)}{where}\n"
            ")\n"
            "DELETE FROM [_ranked] WHERE [_sample_rn] > ?;"
        ),
        params=[*params, int(keep)],
        description=f"keep {int(keep)}",
    )
