"""Table naming rules.

These come from the old generator and are invariants, not preferences: the
server, the staging table and the destination must agree or a transfer fails.
Every function here is idempotent, so applying a rule to an already-correct
name is a no-op rather than a double prefix.
"""

from __future__ import annotations

import re

GLOBAL_TEMP_PREFIX = "##JVM_"
LOCAL_STAGING_PREFIX = "#Local_"
DEFAULT_SCHEMA = "dbo"

# A bare identifier, optionally bracketed: PatientDim, [PatientDim], ##JVM_X
_IDENTIFIER = r"(?:\[[^\]]+\]|[A-Za-z_#@][\w@$#]*)"
# A possibly-qualified reference, matched whole so `dbo.PatientDim` is never
# mistaken for the bare identifier `dbo` and qualified a second time.
_REFERENCE = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
_FROM_OR_JOIN = re.compile(
    rf"(?P<lead>\b(?:FROM|JOIN)\s+)(?P<table>{_REFERENCE})",
    re.IGNORECASE,
)
_LEADING_TABLE = re.compile(rf"^(?P<ws>\s*)(?P<table>{_REFERENCE})")


class NamingError(ValueError):
    """Raised when a name cannot be derived."""


def _require(dest_table: str | None) -> str:
    if dest_table is None or not str(dest_table).strip():
        raise NamingError("dest_table is required to derive a table name.")
    return str(dest_table).strip()


def base_name(dest_table: str | None) -> str:
    """Strip any temp marker and generator prefix down to the bare name."""
    name = _require(dest_table).lstrip("#")
    for prefix in ("JVM_", "Local_"):
        if name.upper().startswith(prefix.upper()):
            name = name[len(prefix):]
            break
    return name


def global_temp(dest_table: str | None) -> str:
    """Cosmos session-scoped output: PKTable -> ##JVM_PKTable.

    A dest_table that already starts with JVM_ is not prefixed twice, which is
    the `##JVM_JVM_Foo` bug the old generator guarded against.
    """
    return GLOBAL_TEMP_PREFIX + base_name(dest_table)


def local_staging(dest_table: str | None) -> str:
    """Projects session-scoped staging: PKTable -> #Local_PKTable."""
    return LOCAL_STAGING_PREFIX + base_name(dest_table)


def destination(project_db: str | None, dest_table: str | None) -> str:
    """Durable Projects table, always fully qualified.

    Generated SQL gets run from tools with ambiguous database context, so the
    destination never relies on USE.
    """
    if not project_db or not str(project_db).strip():
        raise NamingError("project_db is required to qualify a destination table.")
    return f"{str(project_db).strip()}.{DEFAULT_SCHEMA}.{base_name(dest_table)}"


def is_temp_table(token: str) -> bool:
    return token.lstrip("[").startswith("#")


def is_schema_qualified(token: str) -> bool:
    """True when a reference already names a schema.

    Dots inside brackets do not count, so `[My.Table]` is still unqualified.
    """
    return "." in re.sub(r"\[[^\]]*\]", "", token)


def qualify(token: str, database: str | None = None) -> str:
    """Add the schema, and optionally the database, to a bare table reference.

    A database is supplied when a cohort reads somewhere other than the
    connected one -- a SneakPeek cohort under `cosmos_db: Dual`, where a
    two-part name would silently resolve against COSMOS instead.

    Left alone: temp tables, which live in tempdb and must stay unqualified,
    and anything already carrying a schema, which is what prevents `dbo.dbo.`.
    """
    if is_temp_table(token) or is_schema_qualified(token):
        return token
    if database:
        return f"{database}.{DEFAULT_SCHEMA}.{token}"
    return f"{DEFAULT_SCHEMA}.{token}"


def qualify_table_ref(ref: str, database: str | None = None) -> str:
    """Qualify the leading table of a `from` entry: `PatientDim AS p`."""
    match = _LEADING_TABLE.match(ref)
    if not match:
        return ref
    table = match.group("table")
    return ref[: match.start("table")] + qualify(table, database) + ref[match.end("table"):]


def qualify_join_clause(clause: str, database: str | None = None) -> str:
    """Qualify every table named after FROM or JOIN in a clause."""
    return _FROM_OR_JOIN.sub(
        lambda m: m.group("lead") + qualify(m.group("table"), database), clause
    )
