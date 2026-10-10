"""Choosing a pull's Projects database before it first runs (D164, D218).

Two of three pulls stopped when PROJECTD93A5E7 filled while the user's other
project databases stood empty. So a pull executing for the first time is
given a database of its own where it can be: the blueprint's own if no other
unfinished pull uses it, else the emptiest no other unfinished pull uses,
else (stacking) the emptiest. Only a database with more than 6 GB free is
chosen; with none, nothing is built. The choice goes into the manifest and
every phase document and never changes (D52): a resume stays where its
finished batches are.

A blueprint whose `project_db` is `auto` (or empty) leaves the choice to this.
One that names a database has chosen it (D218): it is used even where another
unfinished pull is, and with 6 GB or less free, or no way in, nothing is built.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config
from .executor import iter_units
from .manifest import FINISHED, Manifest
from .models import PENDING
from .yaml_io import dump_yaml, load_yaml

MIN_FREE_MB = 6 * 1024
AUTO = "auto"   # project_db that leaves the choice to the first Execute (D218)


def is_auto(project_db: Any) -> bool:
    return str(project_db or "").strip().lower() in ("", AUTO)
# Each data file's room to its cap, as clear_projects_db reports it (MB).
FILES_SQL = """
SELECT type_desc, FILEPROPERTY(name, 'SpaceUsed') / 128,
       CASE WHEN max_size = -1 THEN NULL ELSE max_size / 128 END
FROM sys.database_files
"""


class DatabaseChoiceError(RuntimeError):
    """No listed database can take the pull; the message lists each one."""


@dataclass
class Room:
    database: str
    free_mb: float | None = None  # None: a data file has no cap
    problem: str = ""  # why it could not be measured

    @property
    def usable(self) -> bool:
        return not self.problem and (self.free_mb is None or self.free_mb > MIN_FREE_MB)

    @property
    def size(self) -> float:
        return float("inf") if self.free_mb is None else self.free_mb

    def text(self) -> str:
        if self.problem:
            return f"could not be opened ({self.problem})"
        if self.free_mb is None:
            return "no cap on its data files"
        return f"{self.free_mb / 1024:,.1f} GB free"


def needs_choice(manifest: Manifest) -> bool:
    """A pull given no database yet, with nothing run: its first Execute."""
    if manifest.data.get("database_choice"):
        return False
    return all(node.status == PENDING for session in manifest.sessions for node in session.children)


LOG_WAIT_SQL = "SELECT log_reuse_wait_desc FROM sys.databases WHERE name = DB_NAME()"


def room_and_log(connection: Any) -> tuple[float | None, float | None, str]:
    """The data files' and the log's room to their caps (MB; None, no cap),
    and what, if anything, keeps the log from reusing its space."""
    cursor = connection.cursor()
    cursor.execute(FILES_SQL)
    room: dict[str, float | None] = {"ROWS": 0.0, "LOG": 0.0}
    for kind, used, cap in cursor.fetchall():
        kind = str(kind).upper()
        if kind not in room or room[kind] is None:
            continue
        room[kind] = None if cap is None else room[kind] + float(cap) - float(used or 0)
    cursor.execute(LOG_WAIT_SQL)
    row = cursor.fetchone()
    return room["ROWS"], room["LOG"], str(row[0]) if row and row[0] else ""


def free_space(connection: Any) -> float | None:
    """The data files' room to their caps, in MB; None if one has no cap."""
    cursor = connection.cursor()
    cursor.execute(FILES_SQL)
    free = 0.0
    for kind, used, cap in cursor.fetchall():
        if str(kind).upper() != "ROWS":
            continue
        if cap is None:
            return None
        free += float(cap) - float(used or 0)
    return free


def measure(names: list[str], settings: Any, connect_fn: Callable[..., Any]) -> list[Room]:
    rooms = []
    for name in names:
        try:
            connection = connect_fn(settings.projects_connection_string(name),
                                    login_timeout=settings.login_timeout,
                                    query_timeout=settings.query_timeout)
        except Exception as exc:  # noqa: BLE001 - skipped, and said
            rooms.append(Room(name, problem=str(exc).splitlines()[0] if str(exc) else type(exc).__name__))
            continue
        try:
            rooms.append(Room(name, free_space(connection)))
        except Exception as exc:  # noqa: BLE001
            rooms.append(Room(name, problem=str(exc).splitlines()[0] if str(exc) else type(exc).__name__))
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
    return rooms


def unfinished_pulls(manifest: Manifest) -> dict[str, list[str]]:
    """Each database other unfinished pulls beside this one use, lower-cased,
    with their names. Unfinished: not finished and packaged. A pull split and
    never run has claimed no database yet."""
    from .pulls import manifest_state

    used: dict[str, list[str]] = {}
    if manifest.path is None:
        return used
    here = manifest.path.parent.resolve()
    for other in sorted(here.parent.glob("*/pullmanifest.yaml")):
        if other.parent.resolve() == here:
            continue
        _, _, how = manifest_state(other)
        if how == FINISHED and (other.parent / "contents.md").is_file():
            continue
        try:
            data = load_yaml(other) or {}
        except Exception:  # noqa: BLE001 - an unreadable pull claims nothing
            continue
        if not how and not data.get("database_choice"):
            continue
        database = str((data.get("project") or {}).get("project_db") or "").strip()
        if database:
            used.setdefault(database.lower(), []).append(other.parent.name)
    return used


def choose(rooms: list[Room], own: str, used: dict[str, list[str]]) -> tuple[Room, str]:
    """The database, and why, among those with more than 6 GB free."""
    usable = [room for room in rooms if room.usable]
    if not usable:
        lines = "\n".join(f"  {room.database}: {room.text()}" for room in rooms)
        raise DatabaseChoiceError(
            f"No Projects database has more than {MIN_FREE_MB // 1024} GB free, so nothing was "
            f"built:\n{lines}\nFree space in one (drop a finished pull's tables in "
            "clear_projects_db), or add a database to DEFAULT_PROJECTS_DATABASES in "
            "pullmanager/config.py and bundle again, "
            "then Execute again.")
    free = [room for room in usable if room.database.lower() not in used]
    mine = next((room for room in free if room.database.lower() == own.lower()), None)
    if mine is not None:
        return mine, "the blueprint's own; no other unfinished pull uses it"
    if free:
        best = max(free, key=lambda room: room.size)
        return best, "the most free space of those no other unfinished pull uses"
    best = max(usable, key=lambda room: room.size)
    sharing = ", ".join(used.get(best.database.lower(), []))
    return best, f"every database has an unfinished pull, so it shares, with {sharing}"


def record(manifest: Manifest, room: Room, why: str, rooms: list[Room]) -> None:
    """The choice, into the manifest and every phase document, for good."""
    manifest.data.setdefault("project", {})["project_db"] = room.database
    manifest.data["database_choice"] = {
        "database": room.database,
        "why": why,
        "chosen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "free": {r.database: r.text() for r in rooms},
    }
    for session in manifest.sessions:
        for _, _, path in iter_units(manifest, session):
            if not Path(path).is_file():
                continue
            doc = load_yaml(path) or {}
            if doc.get("project_db") != room.database:
                doc["project_db"] = room.database
                dump_yaml(doc, path)
    manifest.save()


def choose_database(manifest: Manifest, settings: Any, connect_fn: Callable[..., Any],
                    say: Callable[[str], None] = print, names: list[str] | None = None) -> bool:
    """Give a pull executing for the first time its database (D164). False,
    having said why, when none can take it."""
    if not needs_choice(manifest):
        return True
    own = str(manifest.project.get("project_db") or "").strip()
    if not is_auto(own):
        return use_named(manifest, own, settings, connect_fn, say)
    names = list(names or config.DEFAULT_PROJECTS_DATABASES)
    say(f"Choosing a Projects database: measuring {len(names)} ...")
    rooms = measure(names, settings, connect_fn)
    for room in rooms:
        say(f"  {room.database}: {room.text()}")
    try:
        room, why = choose(rooms, "", unfinished_pulls(manifest))
    except DatabaseChoiceError as exc:
        say(f"ERROR {exc}")
        return False
    record(manifest, room, why, rooms)
    say(f"This pull's Projects database is {room.database} ({why}), for good.")
    return True


def use_named(manifest: Manifest, name: str, settings: Any, connect_fn: Callable[..., Any],
              say: Callable[[str], None]) -> bool:
    """The blueprint's own database, chosen in Author (D218): used if it can take the pull."""
    say(f"The blueprint names {name}: measuring it ...")
    room = measure([name], settings, connect_fn)[0]
    say(f"  {room.database}: {room.text()}")
    if not room.usable:
        say(f"ERROR {name}, the database the blueprint names, cannot take the pull: {room.text()}; "
            f"it needs more than {MIN_FREE_MB // 1024} GB free. Nothing was built. Free space in it "
            "(clear_projects_db), or choose another, or Auto, in Author's Project DB, export the "
            "blueprint and split again.")
        return False
    sharing = unfinished_pulls(manifest).get(name.lower(), [])
    why = "named in the blueprint" + (f"; shared with {', '.join(sharing)}" if sharing else "")
    record(manifest, room, why, [room])
    say(f"This pull's Projects database is {room.database} ({why}), for good.")
    return True
