"""A pull backed up before Artifacts replaces it (D149).

The pull's run folder, without `pull_files/` (the split and its SQL, rebuilt
from the transfer YAML), is mirrored to `<backup folder>/<project>/`: a file
already there with the same size and time is left, one gone from the run folder
is removed, so the backup stays an exact copy and a second backup copies only
what changed. The backup folder is `datascope.json`'s `backup`, set in Run.
When it cannot be reached, or none is set, the copy goes to `backup/` in the
runs folder instead, so nothing is replaced without a copy somewhere.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config
from .pulls import PULL_FILES_DIR

# Left out of a backup: what the transfer YAML rebuilds, what only an Execute
# in progress holds, and Python's cache.
SKIPPED_DIRS = {PULL_FILES_DIR, "__pycache__"}
SKIPPED_SUFFIXES = (".lock", ".pyc", ".tmp")
# Times on a network share or FAT drive can be 2 seconds coarse.
SAME_TIME_SECONDS = 2


@dataclass
class Backup:
    """Where one pull went, and what the copy did."""

    pull: str
    destination: Path
    copied: int = 0
    unchanged: int = 0
    removed: int = 0
    # Why it went to runs/backup instead of the backup folder, if it did.
    fell_back: str = ""

    def line(self) -> str:
        return (f"{self.pull}: backed up to {self.destination} ({self.copied} copied, "
                f"{self.unchanged} unchanged, {self.removed} removed)")


def files_to_back_up(run_folder: Path) -> list[Path]:
    """The run folder's files, relative to it, the skipped ones left out."""
    found = []
    for folder, dirs, files in os.walk(run_folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(files):
            if not name.endswith(SKIPPED_SUFFIXES):
                found.append((Path(folder) / name).relative_to(run_folder))
    return found


def mirror(source: Path, destination: Path) -> tuple[int, int, int]:
    """Make `destination` an exact copy of `source`'s backed-up files; returns
    (copied, unchanged, removed)."""
    copied = unchanged = removed = 0
    wanted = files_to_back_up(source)
    keep = {rel.as_posix() for rel in wanted}
    for rel in wanted:
        origin, target = source / rel, destination / rel
        stat = origin.stat()
        if target.is_file():
            there = target.stat()
            if (there.st_size == stat.st_size
                    and abs(there.st_mtime - stat.st_mtime) <= SAME_TIME_SECONDS):
                unchanged += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".tmp")
        shutil.copy2(origin, partial)  # whole or not at all
        os.replace(partial, target)
        copied += 1
    if destination.is_dir():
        for folder, dirs, files in os.walk(destination, topdown=False):
            for name in files:
                path = Path(folder) / name
                if path.relative_to(destination).as_posix() not in keep:
                    path.unlink()
                    removed += 1
            if Path(folder) != destination and not any(Path(folder).iterdir()):
                Path(folder).rmdir()
    return copied, unchanged, removed


def local_folder(run_folder: Path) -> Path:
    """`backup/` in the runs folder this pull is in (D149)."""
    return run_folder.parent / "backup"


def backup_pull(run_folder: Path, home: Path) -> Backup:
    """Mirror one pull to the backup folder, or to runs/backup when that
    cannot be reached or none is set. Raises OSError only when neither can be
    written."""
    run_folder = Path(run_folder).resolve()
    local = local_folder(run_folder)
    try:
        chosen = config.backup_dir(home)
    except config.ConfigError as exc:
        chosen, why = None, f"datascope.json could not be read ({exc})"
    else:
        why = "no backup folder is set" if chosen is None else ""
    if chosen is not None and not chosen.is_dir():
        why, chosen = f"the backup folder {chosen} could not be reached", None
    if chosen is not None:
        try:
            result = Backup(run_folder.name, chosen / run_folder.name)
            result.copied, result.unchanged, result.removed = mirror(run_folder, result.destination)
            return result
        except OSError as exc:
            why = f"the backup folder {chosen} could not be written ({exc})"
    result = Backup(run_folder.name, local / run_folder.name, fell_back=why)
    result.copied, result.unchanged, result.removed = mirror(run_folder, result.destination)
    return result


def has_parquets(run_folder: Path) -> bool:
    """Whether the pull has been packaged, so there is something to lose."""
    return any(any(folder.glob("*.parquet")) for folder in Path(run_folder).glob("*_parquets"))
