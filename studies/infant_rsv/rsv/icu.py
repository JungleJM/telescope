"""`python rsv icu`: run sql/check_icu.sql against Cosmos and write its results, every row, to a page.

The month is settings `verify`'s, and the specialties tested as ICU are
`icu_specialties`, so the page tests what the analysis uses. It only reads.
"""

from __future__ import annotations

import re
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import verify
from .config import PACKAGE_DIR, RsvError, Settings
from .page import Page, fit

SCRIPT = PACKAGE_DIR / "sql" / "check_icu.sql"
TITLE = re.compile(r"/\*\s*(\d+)\.\s*(.*?)\s*\*/", re.DOTALL)


def script_for(settings: Settings, path: Path = SCRIPT) -> str:
    """The script with the month and the ICU specialties set from the settings."""
    text = path.read_text(encoding="utf-8")
    params = verify.window(settings)
    meds_to = verify.key_of(verify.day_of(params["to"]) + timedelta(days=90))
    for name, value in (("from", params["from"]), ("to", params["to"]), ("meds_to", meds_to)):
        text, n = re.subn(rf"(DECLARE @{name} BIGINT = )\d+;", rf"\g<1>{value};", text)
        if n != 1:
            raise RsvError(f"{path} has no `DECLARE @{name} BIGINT = ...;` line to set.")
    specialties = [str(s).replace("'", "''") for s in settings["icu_specialties"]]
    if not specialties:
        raise RsvError("settings.yaml `icu_specialties` is empty: nothing to test as ICU.")
    values = ",\n    ".join(f"(N'{s}')" for s in specialties)
    text, n = re.subn(r"(INSERT INTO @icu \(Specialty\) VALUES)[^;]*;", rf"\g<1>\n    {values};", text)
    if n != 1:
        raise RsvError(f"{path} has no `INSERT INTO @icu (Specialty) VALUES ...;` to set.")
    return text


def titles(script: str) -> list[str]:
    return [" ".join(m.group(2).split()) for m in TITLE.finditer(script)]


def run(connection: Any, script: str) -> list[tuple[list[str], list[tuple]]]:
    """Every result set the script returns, as (column names, rows)."""
    cursor = connection.cursor()
    cursor.execute(script)
    results = []
    while True:
        if cursor.description:
            results.append(([d[0] or "" for d in cursor.description], [tuple(r) for r in cursor.fetchall()]))
        if not cursor.nextset():
            break
    return results


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return verify.fmt(value)
    if isinstance(value, float):
        return f"{value:,.1f}"
    try:                                   # Decimal: a median or a rate, never a count
        return f"{float(value):,.1f}"
    except (TypeError, ValueError):
        return str(value)


def icu_page(settings: Settings, results: list[tuple[list[str], list[tuple]]], names: list[str]) -> Page:
    params = verify.window(settings)
    page = Page(settings, "ICU check", "icu", section="infant admissions",
                section_filter=f"admitted {params['from']}-{params['to']}")
    for line in textwrap.wrap("ICU = " + "; ".join(str(s) for s in settings["icu_specialties"]), page.width):
        page.line(line)
    if len(results) != len(names):
        page.line(fit(f"{len(results)} results for {len(names)} questions", page.width))
    for number, (columns, rows) in enumerate(results, 1):
        title = names[number - 1] if number <= len(names) else ""
        page.line()
        for text in textwrap.wrap(f"{number}. {title}", page.width, subsequent_indent="   "):
            page.line(text)
        if not rows:
            page.line("   (no rows)")
            continue
        if len(columns) == 2:                    # a name and its count: wrapped lines, every one
            lines = textwrap.wrap("; ".join(f"{cell(r[0]) or '(blank)'} {cell(r[1])}" for r in rows), page.width - 3)
        elif len(rows) == 1:                     # one row of totals: name=value
            lines = textwrap.wrap("; ".join(f"{c}={cell(v)}" for c, v in zip(columns, rows[0])), page.width - 3)
        elif len({r[0] for r in rows}) < len(rows):   # rows sharing their first value: one entry per value
            lines = textwrap.wrap(" | ".join(columns), page.width - 3)
            groups: dict[Any, list[str]] = {}
            for r in rows:
                groups.setdefault(r[0], []).append(" ".join(cell(c) for c in r[1:]))
            for first, rest in groups.items():
                lines += textwrap.wrap(f"- {cell(first)}: " + "; ".join(rest), page.width - 3,
                                       subsequent_indent="  ")
        else:
            lines = textwrap.wrap(" | ".join(columns), page.width - 3)
            for row in rows:
                lines += textwrap.wrap("- " + " | ".join(cell(c) for c in row), page.width - 3,
                                       subsequent_indent="  ")
        for text in lines:
            page.line("   " + text)
    return page


def check_icu(settings: Settings, connection: Any | None = None) -> Path:
    verify.MIN_CELL = int(settings.get("min_cell", 0) or 0)
    script = script_for(settings)
    connection = connection or verify.connect_cosmos(settings.root)
    print("Running sql/check_icu.sql against Cosmos (it only reads) ...", flush=True)
    results = run(connection, script)
    return icu_page(settings, results, titles(script)).write("icu-check")
