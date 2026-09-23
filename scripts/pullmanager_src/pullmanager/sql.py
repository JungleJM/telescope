"""Shared SQL construction helpers.

The delicate part is the WHERE builder. Authors write predicates as a list of
lines, and a line may continue a parenthesised boolean group started by the
previous one, so `AND` cannot simply be inserted between entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .naming import qualify_join_clause, qualify_table_ref
from .normalize import normalize_bool

# A line that already begins with one of these continues the previous
# predicate, so prefixing `AND` would produce invalid SQL.
CONTINUATION_PREFIXES = ("AND", "OR", ")", "--")


@dataclass
class SqlBlock:
    """One executable unit, addressed by manifest id rather than by text."""

    block_id: str
    side: str  # "server" or "local"
    sql: str
    dest_table: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"<SqlBlock {self.side} {self.block_id}>"


def quote_literal(value: Any) -> str:
    """Render a Python value as a T-SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def quote_name(name: str) -> str:
    return f"[{name}]"


def column_names(columns: list[dict[str, Any]]) -> list[str]:
    return [str(c["name"]) for c in columns if isinstance(c, dict) and c.get("name")]


def column_list(columns: list[dict[str, Any]], indent: str = "") -> str:
    return ", ".join(quote_name(n) for n in column_names(columns))


def is_nullable(column: dict[str, Any]) -> bool:
    return normalize_bool(column.get("nullable"), default=True)


def ddl_body(columns: list[dict[str, Any]]) -> str:
    """The column definitions inside a CREATE TABLE."""
    lines = []
    for column in columns:
        if not isinstance(column, dict) or not column.get("name"):
            continue
        null = "NULL" if is_nullable(column) else "NOT NULL"
        lines.append(f"    {quote_name(str(column['name']))} {column.get('type', 'VARCHAR(900)')} {null}")
    return ",\n".join(lines)


def from_entries(filter_block: dict[str, Any]) -> list[str]:
    """`from` may be a string or a list; the old renderer tolerated both."""
    value = (filter_block or {}).get("from")
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def join_entries(filter_block: dict[str, Any]) -> list[str]:
    value = (filter_block or {}).get("join")
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def where_entries(filter_block: dict[str, Any]) -> list[str]:
    value = (filter_block or {}).get("where")
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if str(item).strip()]


def non_null_predicates(columns: list[dict[str, Any]]) -> list[str]:
    """`IS NOT NULL` for every column the cohort declares as non-nullable.

    Without these a NOT NULL destination column rejects the insert partway
    through, after the expensive part of the pull has already run.
    """
    predicates = []
    for column in columns:
        if not isinstance(column, dict) or is_nullable(column):
            continue
        source = str(column.get("source") or "").strip()
        if source:
            predicates.append(f"{source} IS NOT NULL")
    return predicates


def render_where(predicates: list[str], indent: str = "    ") -> str:
    """Join predicates with AND, leaving continuation lines alone.

    A grouped code list authored as separate list entries must survive intact:

        ( dt.Value LIKE 'K50.%'      ->  AND ( dt.Value LIKE 'K50.%'
          OR dt.Value = 'K50'        ->      OR dt.Value = 'K50'
        )                            ->      )
    """
    rendered: list[str] = []
    first = True
    for raw in predicates:
        for line in str(raw).splitlines() or [""]:
            stripped = line.strip()
            if not stripped:
                continue
            continues = stripped.upper().startswith(CONTINUATION_PREFIXES)
            if first and not continues:
                rendered.append(f"{indent}{stripped}")
                first = False
            elif continues:
                rendered.append(f"{indent}{stripped}")
            else:
                rendered.append(f"{indent}AND {stripped}")
                first = False
    return "\n".join(rendered)


def render_source_clause(filter_block: dict[str, Any], indent: str = "") -> str:
    """FROM and JOIN lines, schema-qualified consistently."""
    lines: list[str] = []
    froms = from_entries(filter_block)
    if froms:
        lines.append(f"{indent}FROM {qualify_table_ref(froms[0]).strip()}")
        for extra in froms[1:]:
            lines.append(f"{indent}    , {qualify_table_ref(extra).strip()}")
    for join in join_entries(filter_block):
        lines.append(f"{indent}{qualify_join_clause(join.strip())}")
    return "\n".join(lines)
