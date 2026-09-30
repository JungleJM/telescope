"""Before a session pulls, the columns its cohorts read, asked of Cosmos (D156).

A column the dictionary listed and Cosmos lacked failed a run after an
eleven-minute query (`ReadyToDispenseDateKey`, 30 September 2026). Here each
plain `alias.Column` source is matched to the Cosmos table its alias names,
and each table's columns are read once, so a missing one fails `setup` in a
second, before anything is built.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .server_sql import cohort_database

_NAME = r"\[?[A-Za-z_][\w$#]*\]?"
_TABLE = rf"{_NAME}(?:\.{_NAME}){{0,2}}"
_LEADING = re.compile(rf"^\s*(?P<table>{_TABLE})(?:\s+(?:AS\s+)?(?P<alias>\w+))?\s*$", re.I)
_JOINED = re.compile(rf"\bJOIN\s+(?P<table>{_TABLE})\s+(?:AS\s+)?(?P<alias>\w+)\b", re.I)
_SOURCE = re.compile(r"^\s*(?P<alias>\w+)\.\[?(?P<column>\w+)\]?\s*$")


@dataclass
class Missing:
    cohort: str
    table: str
    column: str
    near: list[str] = field(default_factory=list)

    def line(self) -> str:
        close = f" (near: {', '.join(self.near)})" if self.near else ""
        return f"{self.cohort} reads {self.table}.{self.column}{close}"


def bare(name: str) -> str:
    return name.strip("[]")


def cosmos_tables(cohort: dict[str, Any]) -> dict[str, tuple[str | None, str]]:
    """alias -> (database, table) for each Cosmos table the cohort's `filter`
    names in `from` and `join`. Temps (`##prefix_X`) are ours, not Cosmos's."""
    spec = cohort.get("filter") or {}

    def listed(key: str) -> list[str]:
        value = spec.get(key) or []
        return [str(v) for v in ([value] if isinstance(value, str) else value)]

    found: dict[str, tuple[str | None, str]] = {}
    default_db = cohort_database(cohort, {})
    matches = [_LEADING.match(entry) for entry in listed("from")]
    matches += [m for entry in listed("join") for m in _JOINED.finditer(entry)]
    for match in matches:
        if not match or "#" in match.group("table") or "{{" in match.group("table"):
            continue
        parts = [bare(p) for p in match.group("table").split(".")]
        table = parts[-1]
        database = parts[0] if len(parts) == 3 else default_db
        alias = match.group("alias") or table
        found[alias.lower()] = (database, table)
    return found


def columns_read(cohorts: list[dict[str, Any]]) -> dict[tuple[str | None, str], list[tuple[str, str]]]:
    """(database, table) -> [(cohort, column)] for every plain source column."""
    wanted: dict[tuple[str | None, str], list[tuple[str, str]]] = {}
    for cohort in cohorts:
        tables = cosmos_tables(cohort)
        name = str(cohort.get("name") or cohort.get("dest_table"))
        for column in cohort.get("columns") or []:
            if not isinstance(column, dict):
                continue
            match = _SOURCE.match(str(column.get("source") or ""))
            if match and match.group("alias").lower() in tables:
                wanted.setdefault(tables[match.group("alias").lower()], []).append(
                    (name, match.group("column")))
    return wanted


def columns_sql(database: str | None) -> tuple[str, str]:
    """The query for a table's columns, and how to name the table in it."""
    if database and re.fullmatch(r"\w+", database):
        return f"SELECT c.name FROM {database}.sys.columns AS c WHERE c.object_id = OBJECT_ID(?);", f"{database}.dbo."
    return "SELECT c.name FROM sys.columns AS c WHERE c.object_id = OBJECT_ID(?);", "dbo."


def check(connection: Any, cohorts: list[dict[str, Any]]) -> tuple[list[Missing], list[str]]:
    """The source columns Cosmos lacks, and the tables whose columns could not be read."""
    missing: list[Missing] = []
    unread: list[str] = []
    cursor = connection.cursor()
    for (database, table), uses in columns_read(cohorts).items():
        sql, qualifier = columns_sql(database)
        cursor.execute(sql, [f"{qualifier}{table}"])
        have = [str(row[0]) for row in cursor.fetchall()]
        if not have:
            unread.append(f"{database + '.' if database else ''}{table}")
            continue
        lower = {c.lower() for c in have}
        for cohort, column in dict.fromkeys(uses):
            if column.lower() not in lower:
                near = [c for c in have if c.lower() == f"{column.lower()}_x"]
                near += difflib.get_close_matches(column, have, n=2, cutoff=0.8)
                missing.append(Missing(cohort, table, column, list(dict.fromkeys(near))))
    return missing, unread
