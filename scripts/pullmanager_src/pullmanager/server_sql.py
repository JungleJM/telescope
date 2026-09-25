"""Cosmos-side SQL.

Renders one block per cohort, addressed by manifest id. Nothing downstream
searches SQL text to decide what to run.
"""

from __future__ import annotations

import re

from typing import Any

from .naming import global_temp, temp_prefix
from .normalize import (
    cosmos_database,
    normalize_bool,
    normalize_dedup_keys,
    root_pk_cohorts,
    validate_dedup_columns,
)
from .sql import (
    SqlBlock,
    column_list,
    column_names,
    column_sources,
    ddl_body,
    hash_order,
    non_null_predicates,
    quote_literal,
    quote_name,
    render_source_clause,
    render_where,
    where_entries,
)

SERVER_NAME_QUERY = "SELECT @@SERVERNAME AS CosmosServerName;"

_PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class RenderError(ValueError):
    """Raised when a cohort cannot be rendered."""


def top_clause(
    cohort: dict[str, Any], doc: dict[str, Any], roots: list[dict[str, Any]]
) -> str:
    """`TOP (n)`, applied to root PK cohorts only.

    Limiting a downstream PK as well compounds the restriction: 500 patients
    and then 500 of their events is not 500 patients' worth of events.

    Plural because `cosmos_db: Dual` renders each cohort twice, once per
    database. Those are parallel chains, not competing ones, so each has its
    own root and each is limited.
    """
    if not normalize_bool(test_option(doc, "smallset") or test_option(doc, "smallest")):
        return ""
    if not any(cohort is root for root in roots):
        return ""
    limit = test_option(doc, "stop_at_for_pk_table")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return ""
    return f"TOP ({limit}) " if limit > 0 else ""


def sample_keys(cohort: dict[str, Any], key_sets: list[list[str]]) -> list[str]:
    """The PK's key: its first dedup key set, else its `key_column(s)` (D60)."""
    if key_sets:
        return list(key_sets[0])
    key = cohort.get("key_column") or cohort.get("key_columns")
    if isinstance(key, str):
        return [key]
    return [str(k) for k in key or []]


def sample_order(cohort: dict[str, Any], doc: dict[str, Any], key_sets: list[list[str]]) -> str:
    """The ORDER BY that makes a limited PK a reproducible random sample (D60).

    Without it `TOP (n)` keeps whichever rows the server reaches first, often
    clustered by site or period. Deduplicated, the key is read from the
    `[_deduped]` rows by name; otherwise through its source, since an alias
    cannot be used inside an expression in the same SELECT.
    """
    if not normalize_bool(test_option(doc, "random_pk_sample")):
        return ""
    keys = sample_keys(cohort, key_sets)
    if not keys:
        raise RenderError(
            f"Cohort {cohort.get('dest_table')!r}: random_pk_sample needs the PK's key, from "
            "its dedup_keys or key_column, and it has neither."
        )
    if key_sets:
        return hash_order([f"[_deduped].{quote_name(k)}" for k in keys])
    sources = column_sources(cohort.get("columns") or [])
    missing = [k for k in keys if k not in sources]
    if missing:
        raise RenderError(
            f"Cohort {cohort.get('dest_table')!r}: key column(s) {', '.join(missing)} are "
            "not among its columns."
        )
    return hash_order([sources[k] for k in keys])


def test_option(doc: dict[str, Any], key: str) -> Any:
    """A test option: at the split document's top level, where YAML Manager
    writes it, else in a `test_options` group (splits made before that).

    Reading only the group meant a template that wrote `smallset` at the top
    level silently lost its row limit.
    """
    if key in doc:
        return doc[key]
    return (doc.get("test_options") or {}).get(key)


def cohort_predicates(cohort: dict[str, Any]) -> list[str]:
    columns = cohort.get("columns") or []
    return where_entries(cohort.get("filter") or {}) + non_null_predicates(columns)


def cohort_database(cohort: dict[str, Any], doc: dict[str, Any]) -> str | None:
    """The database this cohort reads, when it differs from the connection.

    Under `cosmos_db: Dual` the SneakPeek variants carry their own `cosmos_db`,
    and a two-part name would resolve against the connected COSMOS instead.
    """
    declared = cohort.get("cosmos_db")
    if not declared:
        return None
    return cosmos_database(declared)


def render_select(
    cohort: dict[str, Any], top: str, inner_indent: str = "    ", database: str | None = None
) -> str:
    columns = cohort.get("columns") or []
    projections = [
        f"{inner_indent}{column['source']} AS {quote_name(str(column['name']))}"
        for column in columns
        if isinstance(column, dict) and column.get("name") and column.get("source")
    ]
    parts = [f"SELECT {top}".rstrip(), ",\n".join(projections)]
    source = render_source_clause(cohort.get("filter") or {}, database=database)
    if source:
        parts.append(source)
    predicates = cohort_predicates(cohort)
    if predicates:
        parts.append("WHERE")
        parts.append(render_where(predicates, inner_indent))
    return "\n".join(parts)


def render_dedup_select(
    cohort: dict[str, Any],
    key_sets: list[list[str]],
    top: str,
    database: str | None = None,
) -> tuple[str, list[str]]:
    """Wrap the projection in ROW_NUMBER and keep one row per key set.

    Returns the SQL and any notes. Deduplication is always visible in the
    output: the old generator could silently emit none at all.

    Keys are the cohort's column names, but ROW_NUMBER sits in the SELECT that
    defines those names, where only source columns are visible, so each is
    written as its source (D58). `[BillingCodeValue]` there was an invalid
    column; `[PatientDurableKey]` worked only because `dxf` has one.
    """
    notes: list[str] = []
    sources = column_sources(cohort.get("columns") or [])
    unsourced = [k for k in key_sets[0] if k not in sources]
    if unsourced:
        raise RenderError(
            f"Cohort {cohort.get('dest_table')!r}: dedup key(s) {', '.join(unsourced)} "
            "have no `source` to deduplicate on."
        )
    keys = [sources[k] for k in key_sets[0]]
    if len(key_sets) > 1:
        notes.append(
            f"Only the first dedup key set {key_sets[0]} is applied; "
            f"{len(key_sets) - 1} further set(s) were declared."
        )
    order_by = dedup_order_by(cohort, sources)
    if order_by:
        order_sql = ", ".join(order_by)
    else:
        order_sql = ", ".join(keys)
        notes.append(
            f"No dedup_order_by for {cohort.get('dest_table')!r}, so which duplicate "
            "survives is arbitrary and may differ between runs."
        )
    inner = render_select(cohort, top="", inner_indent="        ", database=database)
    inner = inner.replace(
        "SELECT\n",
        "SELECT\n"
        f"        ROW_NUMBER() OVER (PARTITION BY {', '.join(keys)} ORDER BY {order_sql}) AS [_dedup_rn],\n",
        1,
    )
    cols = column_list(cohort.get("columns") or [])
    sql = (
        f"SELECT {top}{cols}\n"
        f"FROM (\n"
        f"{_indent(inner, '    ')}\n"
        f") AS [_deduped]\n"
        f"WHERE [_deduped].[_dedup_rn] = 1"
    )
    return sql, notes


ORDER_DIRECTIONS = ("ASC", "DESC")


def dedup_order_by(cohort: dict[str, Any], sources: dict[str, str]) -> list[str]:
    """`dedup_order_by`, each column name written as its source (D58).

    `[IndexDate, EncounterKey DESC]` becomes `dxf.StartDateKey, dxf.EncounterKey
    DESC`. The spellings read before, `dedup_order` and `order_by`, were written
    by nothing while every recipe wrote this one, so "the first diagnosis" was
    any diagnosis; they are refused rather than guessed at.
    """
    for old in ("dedup_order", "order_by"):
        if cohort.get(old):
            raise RenderError(
                f"Cohort {cohort.get('dest_table')!r}: `{old}` is not read. Write "
                "`dedup_order_by: [<column>, ...]`."
            )
    raw = cohort.get("dedup_order_by")
    if not raw:
        return []
    entries = [raw] if isinstance(raw, str) else list(raw)
    rendered = []
    for entry in entries:
        name, direction = split_order_entry(entry)
        if name not in sources:
            raise RenderError(
                f"Cohort {cohort.get('dest_table')!r}: dedup_order_by names `{name}`, "
                f"which is not one of its columns ({', '.join(sources) or 'none'})."
            )
        rendered.append(f"{sources[name]} {direction}" if direction else sources[name])
    return rendered


def split_order_entry(entry: Any) -> tuple[str, str]:
    """`IndexDate DESC` -> (`IndexDate`, `DESC`); `IndexDate` -> (`IndexDate`, ``)."""
    words = str(entry).split()
    if len(words) == 2 and words[1].upper() in ORDER_DIRECTIONS:
        return words[0], words[1].upper()
    return str(entry).strip(), ""


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


def render_cohort(
    cohort: dict[str, Any],
    doc: dict[str, Any],
    roots: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    """DDL plus population for one cohort's global temp table."""
    dest = cohort.get("dest_table")
    if not dest:
        raise RenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
    columns = [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    if not columns:
        raise RenderError(f"Cohort {dest!r} declares no columns.")
    names = column_names(columns)
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise RenderError(f"Cohort {dest!r} declares duplicate column(s): {', '.join(duplicates)}")

    notes: list[str] = []
    temp = global_temp(dest, temp_prefix(doc))
    top = top_clause(cohort, doc, roots or [])
    database = cohort_database(cohort, doc)

    key_sets, dedup_notes = normalize_dedup_keys(cohort)
    notes.extend(dedup_notes)
    if key_sets:
        problems = validate_dedup_columns(key_sets, cohort)
        if problems:
            raise RenderError("; ".join(problems))
        body, more = render_dedup_select(cohort, key_sets, top, database)
        notes.extend(more)
    else:
        body = render_select(cohort, top, database=database)
    sample = sample_order(cohort, doc, key_sets) if top else ""
    if sample:
        body += f"\nORDER BY {sample}"

    sql = (
        f"-- cohort {cohort.get('name')!r} -> {temp}\n"
        f"DROP TABLE IF EXISTS {temp};\n\n"
        f"CREATE TABLE {temp}\n(\n{ddl_body(columns)}\n);\n\n"
        f"INSERT INTO {temp} ({column_list(columns)})\n"
        f"{body};\n\n"
        f"{render_cohort_telemetry(cohort, temp)}"
    )
    # Variable substitution is YAML Manager's job and has already happened by
    # the time a split YAML reaches us. A placeholder surviving to here would
    # render as invalid T-SQL, so fail with the name rather than emit it.
    leftover = sorted({m.group(1) for m in _PLACEHOLDER.finditer(sql)})
    if leftover:
        raise RenderError(
            f"Cohort {dest!r} still contains unsubstituted placeholder(s): "
            f"{', '.join(leftover)}. Render from split YAML, not a raw template."
        )
    return sql, notes


def render_cohort_telemetry(cohort: dict[str, Any], temp: str) -> str:
    """A declared telemetry shape, not column names to be scraped."""
    return (
        "SELECT\n"
        f"    {quote_literal(cohort.get('name'))} AS [CohortName],\n"
        f"    {quote_literal(cohort.get('dest_table'))} AS [DestTable],\n"
        f"    COUNT_BIG(1) AS [RowCount]\n"
        f"FROM {temp};"
    )


def render_phase(doc: dict[str, Any], block_prefix: str) -> tuple[list[SqlBlock], list[str]]:
    """Render every cohort in one phase document."""
    cohorts = [c for c in doc.get("cohorts") or [] if isinstance(c, dict)]
    roots = root_pk_cohorts(cohorts, temp_prefix(doc))
    blocks: list[SqlBlock] = []
    notes: list[str] = []
    for cohort in cohorts:
        if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
            notes.append(f"Skipping {cohort.get('dest_table')!r}: pull_this_cycle is false.")
            continue
        sql, cohort_notes = render_cohort(cohort, doc, roots)
        notes.extend(cohort_notes)
        blocks.append(
            SqlBlock(
                block_id=f"{block_prefix}/{cohort['dest_table']}",
                side="server",
                sql=sql,
                dest_table=str(cohort["dest_table"]),
                meta={"global_temp": global_temp(cohort["dest_table"], temp_prefix(doc))},
            )
        )
    return blocks, notes


def render_setup(doc: dict[str, Any], block_prefix: str) -> list[SqlBlock]:
    """Capture the runtime instance name; it changes on every connection."""
    return [
        SqlBlock(
            block_id=f"{block_prefix}/server-identity",
            side="server",
            sql=SERVER_NAME_QUERY,
            meta={"captures": "linked_server"},
        )
    ]
