"""The dictionary audit: what the dictionary lists and Cosmos lacks (D155).

`MedicationDispenseFact.ReadyToDispenseDateKey` was in the dictionary and not
in Cosmos, and a pull failed on it after an eleven-minute query. This asks
Cosmos for each dictionary table's columns and writes
`runs/dictionary_audit.yaml` with only what did not check out, short enough
to be copied off the VM by screenshot and read back by `scope.py
dictionary-fix` on the Mac.
"""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AUDIT_FILENAME = "dictionary_audit.yaml"
COLUMNS_SQL = "SELECT c.name FROM sys.columns AS c WHERE c.object_id = OBJECT_ID(?);"


@dataclass
class Audit:
    database: str
    checked: int = 0
    tables_not_found: list[str] = field(default_factory=list)
    # table -> [(column, its near matches in Cosmos)]
    missing: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)

    @property
    def wrong(self) -> int:
        return len(self.tables_not_found) + sum(len(v) for v in self.missing.values())


def cosmos_columns(connection: Any, table: str) -> list[str]:
    """The columns Cosmos has for a table; none when it has no such table, or
    this login cannot see it."""
    cursor = connection.cursor()
    cursor.execute(COLUMNS_SQL, [f"dbo.{table}"])
    return [str(row[0]) for row in cursor.fetchall()]


def near(column: str, columns: list[str]) -> list[str]:
    """Close spellings, `_X` first: Epic's suffix is the usual difference."""
    suffixed = [c for c in columns if c.lower() in (f"{column.lower()}_x", column.lower().removesuffix("_x"))]
    close = difflib.get_close_matches(column, columns, n=2, cutoff=0.8)
    return list(dict.fromkeys(suffixed + close))


def audit_dictionary(connection: Any, dictionary: dict[str, Any], database: str) -> Audit:
    result = Audit(database)
    for table, entry in dictionary.items():
        if not isinstance(entry, dict):
            continue
        listed = [str(c) for c in (entry.get("columns") or {})]
        result.checked += 1
        found = cosmos_columns(connection, str(table))
        if not found:
            result.tables_not_found.append(str(table))
            continue
        have = {c.lower() for c in found}
        gone = [(c, near(c, found)) for c in listed if c.lower() not in have]
        if gone:
            result.missing[str(table)] = gone
    return result


def report(result: Audit, bundle: str = "") -> str:
    header = (f"# dictionary audit, {time.strftime('%Y-%m-%d %H:%M')}"
              f"{f', bundle {bundle}' if bundle else ''}, database {result.database}: "
              f"{result.checked} tables, {result.wrong} wrong")
    lines = [header]
    if not result.wrong:
        lines.append("the dictionary matches Cosmos")
        return "\n".join(lines) + "\n"
    if result.tables_not_found:
        lines.append("tables_not_found:  # the dictionary names them; Cosmos has no such "
                     "table, or this login cannot see it")
        lines.extend(f"  - {table}" for table in result.tables_not_found)
    if result.missing:
        lines.append("columns_not_in_cosmos:  # remove these from the dictionary")
        for table, gone in result.missing.items():
            nears = "; ".join(f"{c} -> {', '.join(n)}" for c, n in gone if n)
            line = f"  {table}: [{', '.join(c for c, _ in gone)}]"
            lines.append(line + (f"  # near: {nears}" if nears else ""))
    return "\n".join(lines) + "\n"


def write_report(result: Audit, runs: Path, bundle: str = "") -> tuple[Path, str]:
    text = report(result, bundle)
    path = Path(runs) / AUDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text
