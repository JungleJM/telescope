"""Noticing that Cosmos was refreshed (D51).

Cosmos and Cosmos_SneakPeek are rebuilt about monthly, and a rebuild recreates
the database, so `sys.databases.create_date` changes. Work pulled before a
refresh cannot be mixed with work pulled after it, so a manifest records the
value for each database it reads, and a different value later means every
session starts over.
"""

from __future__ import annotations

from typing import Any

from .executor import iter_units
from .manifest import Manifest
from .normalize import cosmos_database
from .yaml_io import load_yaml

REFRESH_SQL = "SELECT name, create_date FROM sys.databases WHERE name LIKE 'Cosmos%';"


def read_stamps(connection: Any) -> dict[str, str]:
    """Database name to its `create_date`, to the millisecond, as text."""
    cursor = connection.cursor()
    cursor.execute(REFRESH_SQL)
    stamps: dict[str, str] = {}
    for name, created in cursor.fetchall():
        if hasattr(created, "isoformat"):
            stamps[str(name)] = created.isoformat(timespec="milliseconds")
        else:
            stamps[str(name)] = str(created)
    return stamps


def databases_used(manifest: Manifest) -> set[str]:
    """The Cosmos databases the manifest's cohorts read, lower-cased.

    A cohort tagged for SneakPeek reads Cosmos_SneakPeek whichever database
    the connection opened; an untagged one reads the phase's `cosmos_db`.
    """
    used: set[str] = set()
    for session in manifest.sessions:
        for _, _, path in iter_units(manifest, session):
            if not path.is_file():
                continue
            doc = load_yaml(path) or {}
            used.add(cosmos_database(doc.get("cosmos_db")).lower())
            for cohort in doc.get("cohorts") or []:
                if isinstance(cohort, dict) and cohort.get("cosmos_db"):
                    used.add(cosmos_database(cohort["cosmos_db"]).lower())
    return used


def changes(
    manifest: Manifest, stamps: dict[str, str], used: set[str]
) -> list[tuple[str, str, str]]:
    """(database, recorded, now) for every database used whose value moved."""
    now = {name.lower(): (name, value) for name, value in stamps.items()}
    moved = []
    for recorded_name, recorded in manifest.cosmos_refresh.items():
        key = recorded_name.lower()
        if key in used and key in now and now[key][1] != recorded:
            moved.append((now[key][0], recorded, now[key][1]))
    return moved


def unseen(stamps: dict[str, str], used: set[str]) -> list[str]:
    """Databases used whose `create_date` this login could not read."""
    seen = {name.lower() for name in stamps}
    return sorted(db for db in used if db not in seen)


def reconcile(manifest: Manifest, stamps: dict[str, str], used: set[str]) -> list[str]:
    """Compare, reset everything if Cosmos moved, record, and say what happened."""
    lines: list[str] = []
    moved = changes(manifest, stamps, used)
    if moved:
        for name, before, after in moved:
            lines.append(f"{name} was refreshed: created {before} when last run, {after} now.")
        lines.append(
            "Work pulled before a refresh cannot be mixed with work pulled after it, "
            "so every session starts over."
        )
        manifest.reset_all(f"re-pulled: Cosmos refreshed ({', '.join(m[0] for m in moved)})")
    for db in unseen(stamps, used):
        lines.append(
            f"Could not read create_date for {db}, so a refresh of it cannot be detected."
        )
    for name, value in stamps.items():
        if name.lower() in used:
            manifest.cosmos_refresh[name] = value
    return lines
