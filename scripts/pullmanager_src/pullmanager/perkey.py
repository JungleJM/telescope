"""Rows per join key: how many rows each key brought, per landed table (D157).

Celiac's other diagnoses, 124 per patient on average, looked like failed
deduplication until the spread was measured (median 93, 90th percentile 269,
maximum 1,160). A table deduplicated to one row per key shows 1, 1, 1.
"""

from __future__ import annotations

import re
from typing import Any

from .naming import destination
from .sql import quote_name

_EQUALITY = re.compile(r"(\w+)\.\[?(\w+)\]?\s*=\s*(\w+)\.\[?(\w+)\]?")
_JOINED_ALIAS = re.compile(r"\bJOIN\s+\S+\s+(?:AS\s+)?(\w+)\s+ON\b", re.I)


def join_column(cohort: dict[str, Any]) -> str | None:
    """The landed column whose source is this table's side of the first
    equality in the cohort's first JOIN; None when there is none."""
    joins = (cohort.get("filter") or {}).get("join") or []
    if isinstance(joins, str):
        joins = [joins]
    if not joins:
        return None
    first = str(joins[0])
    equality = _EQUALITY.search(first.split(" ON ", 1)[-1] if " ON " in first.upper() else first)
    if not equality:
        return None
    joined = _JOINED_ALIAS.search(first)
    joined_alias = joined.group(1).lower() if joined else None
    sides = [(equality.group(1), equality.group(2)), (equality.group(3), equality.group(4))]
    # This table's side first: the one not naming the table being joined.
    sides.sort(key=lambda side: side[0].lower() == joined_alias)
    sources = {str(c.get("source") or "").replace("[", "").replace("]", "").strip().lower(): str(c["name"])
               for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")}
    for alias, column in sides:
        name = sources.get(f"{alias}.{column}".lower())
        if name:
            return name
    return None


def measure_sql(project_db: str, dest: str, keys: list[str], label: str | None) -> tuple[str, list[Any]]:
    """One row: keys, median, 90th percentile and maximum rows per key."""
    grouped = ", ".join(quote_name(k) for k in keys)
    where, params = ("", []) if label is None else (" WHERE [_batch] = ?", [label])
    sql = (
        "SELECT TOP 1 COUNT_BIG(1) OVER () AS [Keys],\n"
        "    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY [n]) OVER () AS [Median],\n"
        "    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY [n]) OVER () AS [P90],\n"
        "    MAX([n]) OVER () AS [Max]\n"
        f"FROM (SELECT {grouped}, COUNT_BIG(1) AS [n] FROM {destination(project_db, dest)}{where}"
        f" GROUP BY {grouped}) AS [per_key];"
    )
    return sql, params


def stats(key: str, row: Any) -> dict[str, Any]:
    """What the manifest keeps; no row means nothing landed."""
    if not row:
        return {"key": key, "keys": 0}
    return {"key": key, "keys": int(row[0]), "median": number(row[1]),
            "p90": number(row[2]), "max": int(row[3])}


def number(value: Any) -> int | float:
    value = round(float(value), 1)
    return int(value) if value == int(value) else value


def shown(value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"
