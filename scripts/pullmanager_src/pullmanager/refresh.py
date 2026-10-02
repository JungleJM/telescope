"""Noticing that Cosmos was refreshed (D51, D183).

Cosmos and Cosmos_SneakPeek are rebuilt about monthly, and a rebuild recreates
the database, so `sys.databases.create_date` changes. Work pulled before a
refresh cannot be mixed with work pulled after it, so a manifest records the
value for each database it reads, and a value more than a day away later means
every session starts over. Cosmos is served by several instances, each with its
own copy restored minutes apart, so a smaller difference is another instance of
the same refresh (D183); each instance's value is recorded too, by its server
name (`SERVERPROPERTY('ServerName')`, the same name `@@SERVERNAME` gives).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .executor import iter_units
from .manifest import Manifest
from .normalize import cosmos_database
from .yaml_io import load_yaml

REFRESH_SQL = (
    "SELECT name, create_date, CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS server "
    "FROM sys.databases "
    "WHERE name LIKE 'Cosmos%';"
)

# Copies of one refresh on different instances differ by minutes; refreshes
# come about monthly (D183).
SAME_REFRESH = timedelta(days=1)


def read(connection: Any) -> tuple[dict[str, str], str]:
    """Database name to its `create_date`, to the millisecond, as text, and the
    instance that answered (its server name, '' if not given)."""
    cursor = connection.cursor()
    cursor.execute(REFRESH_SQL)
    stamps: dict[str, str] = {}
    server = ""
    for row in cursor.fetchall():
        name, created = row[0], row[1]
        if len(row) > 2 and row[2]:
            server = str(row[2])
        if hasattr(created, "isoformat"):
            stamps[str(name)] = created.isoformat(timespec="milliseconds")
        else:
            stamps[str(name)] = str(created)
    return stamps, server


def read_stamps(connection: Any) -> dict[str, str]:
    """Database name to its `create_date`, to the millisecond, as text."""
    return read(connection)[0]


def _when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        return None


def is_refresh(recorded: str, now: str) -> bool:
    """True when `now` is another refresh than `recorded`: more than a day
    apart (D183). Values that cannot be read as times count if they differ."""
    before, after = _when(recorded), _when(now)
    if before is None or after is None:
        return recorded != now
    return abs(after - before) > SAME_REFRESH


def record_instance(manifest: Manifest, server: str, stamps: dict[str, str], used: set[str]) -> None:
    """Keep each instance's own values, under `cosmos_refresh_instances` (D183)."""
    if not server:
        return
    mine = manifest.cosmos_refresh_instances.setdefault(server, {})
    for name, value in stamps.items():
        if name.lower() in used:
            mine[name] = value


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
    """(database, recorded, now) for every database used that was refreshed."""
    return [m for m in _differences(manifest, stamps, used) if is_refresh(m[1], m[2])]


def same_refresh(
    manifest: Manifest, stamps: dict[str, str], used: set[str]
) -> list[tuple[str, str, str]]:
    """(database, recorded, now) where the value differs but by a day or less:
    another instance's copy of the same refresh (D183)."""
    return [m for m in _differences(manifest, stamps, used) if not is_refresh(m[1], m[2])]


def _differences(
    manifest: Manifest, stamps: dict[str, str], used: set[str]
) -> list[tuple[str, str, str]]:
    now = {name.lower(): (name, value) for name, value in stamps.items()}
    differ = []
    for recorded_name, recorded in manifest.cosmos_refresh.items():
        key = recorded_name.lower()
        if key in used and key in now and now[key][1] != recorded:
            differ.append((now[key][0], recorded, now[key][1]))
    return differ


def unseen(stamps: dict[str, str], used: set[str]) -> list[str]:
    """Databases used whose `create_date` this login could not read."""
    seen = {name.lower() for name in stamps}
    return sorted(db for db in used if db not in seen)


def reconcile(
    manifest: Manifest, stamps: dict[str, str], used: set[str], server: str = ""
) -> list[str]:
    """Compare, reset everything if Cosmos was refreshed, record, and say what happened."""
    lines: list[str] = []
    moved = changes(manifest, stamps, used)
    for name, before, after in same_refresh(manifest, stamps, used):
        lines.append(
            f"{name}: created {after} on {server or 'this instance'}, {before} recorded: "
            "another instance, same refresh (D183)."
        )
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
    refreshed = {m[0].lower() for m in moved}
    recorded = {name.lower() for name in manifest.cosmos_refresh}
    for name, value in stamps.items():
        key = name.lower()
        # The value first recorded stands for its refresh; another instance's
        # copy of it does not replace it.
        if key in used and (key in refreshed or key not in recorded):
            manifest.cosmos_refresh[name] = value
    if moved:
        manifest.cosmos_refresh_instances.clear()
    record_instance(manifest, server, stamps, used)
    return lines
