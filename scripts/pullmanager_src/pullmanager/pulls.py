"""Finding a pull by its project's name, and listing the pulls there are (D66).

A pull lives in `runs/<project>/split/pullmanifest.yaml` (D57), `<project>`
named from the transfer YAML's file name. So `--execute IBD_Ancestry`,
`--execute "IBD Ancestry"` and `--execute IBD_Ancestry_transfer.yaml` all mean
`runs/IBD_Ancestry/split/pullmanifest.yaml`, and a manifest path still works.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .lock import LockInfo, age_words, clock_time, live_lock
from .manifest import Manifest, ManifestError
from .models import DONE, FAILED, PENDING, RUNNING, SKIPPED
from .yaml_io import load_yaml

RUNS_DIR = "runs"
SPLIT_DIR = "split"
LOGS_DIR = "logs"
MANIFEST_FILENAME = "pullmanifest.yaml"
# Dropped from a transfer YAML's file name to name its run folder (D57).
RUN_NAME_SUFFIXES = ("_transfer", "_temp")
YAML_SUFFIXES = (".yaml", ".yml")


class PullNotFound(RuntimeError):
    """Raised when a name matches no pull; the message lists the ones there are."""


def run_folder_name(template: str | Path) -> str:
    """`<project>` in `runs/<project>/` (D57): the transfer YAML's file name
    without `.yaml` and without `_transfer` or `_temp`.

    Only the file name: the folders above it (the project share) play no part.
    The same rule as `makeYaml.run_folder_name`; a test holds the two together.
    """
    stem = Path(str(template).replace("\\", "/")).stem
    for suffix in RUN_NAME_SUFFIXES:
        if stem.endswith(suffix) and stem != suffix:
            stem = stem[: -len(suffix)]
            break
    return re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_") or "project"


def project_name(text: str) -> str:
    """A typed name as its run folder: `IBD Ancestry` is `IBD_Ancestry`.

    A transfer file's name loses `.yaml` and `_transfer` as the split's does.
    Anything else is only cleaned: a dot becomes an underscore rather than
    ending the name.
    """
    text = str(text).strip()
    if text.lower().endswith(YAML_SUFFIXES):
        return run_folder_name(text)
    name = Path(text.replace("\\", "/")).name
    return run_folder_name(name + ".yaml")


def home_folders(cwd: Path | None = None) -> list[Path]:
    """Where `runs/` is looked for: the working directory, then the folder
    holding `pullmanager.py`, which the extracted runtime sits in (D63)."""
    here = Path(cwd or Path.cwd()).resolve()
    beside = Path(__file__).resolve().parents[2]
    return [here] if beside == here else [here, beside]


def run_folder(manifest: str | Path) -> Path:
    """`runs/<project>` for its split's manifest; the manifest's own folder
    for one kept anywhere else."""
    manifest = Path(manifest)
    return manifest.parent.parent if manifest.parent.name == SPLIT_DIR else manifest.parent


def logs_folder(manifest: str | Path) -> Path:
    """Where Execute writes what it prints (D68): `runs/<project>/logs`."""
    return run_folder(manifest) / LOGS_DIR


def newest_log(manifest: str | Path) -> Path | None:
    """The latest `execute-<date>-<time>.log`; the names sort by time."""
    try:
        logs = sorted(logs_folder(manifest).glob("execute-*.log"))
    except OSError:
        return None
    return logs[-1] if logs else None


def is_manifest_file(path: Path) -> bool:
    """A split's manifest, rather than a transfer YAML: it lists sessions."""
    if path.name.lower() == MANIFEST_FILENAME:
        return True
    try:
        data = load_yaml(path)
    except Exception:
        return False
    return isinstance(data, dict) and "sessions" in data


def manifest_in(home: Path, name: str) -> Path | None:
    """`<home>/runs/<name>/split/pullmanifest.yaml`, matching the name in any case."""
    runs = home / RUNS_DIR
    if not runs.is_dir():
        return None
    # Listed rather than looked up, so the folder's own spelling comes back.
    matches = [
        folder for folder in sorted(runs.iterdir())
        if folder.name.lower() == name.lower()
        and (folder / SPLIT_DIR / MANIFEST_FILENAME).is_file()
    ]
    exact = [folder for folder in matches if folder.name == name]
    chosen = (exact or matches or [None])[0]
    return chosen / SPLIT_DIR / MANIFEST_FILENAME if chosen else None


def resolve(argument: str, cwd: Path | None = None) -> Path:
    """The manifest a name or path means. Raises PullNotFound, listing pulls."""
    here = Path(cwd or Path.cwd())
    given = Path(argument) if Path(argument).is_absolute() else here / argument
    if given.is_file() and is_manifest_file(given):
        return given
    if given.is_dir():
        for candidate in (given / MANIFEST_FILENAME, given / SPLIT_DIR / MANIFEST_FILENAME):
            if candidate.is_file():
                return candidate
    # A path that is not there is a mistyped path, not a project's name.
    written_as_path = bool(re.search(r"[\\/]", argument)) or (
        Path(argument).name.lower() == MANIFEST_FILENAME
    )
    if written_as_path and not given.is_file():
        others = not_found_message(argument, "", here).splitlines()[1:]
        raise PullNotFound("\n".join([f"Manifest not found: {given}", *others]))
    name = project_name(argument)
    for home in home_folders(here):
        found = manifest_in(home, name)
        if found:
            return found
    raise PullNotFound(not_found_message(argument, name, here))


def not_found_message(argument: str, name: str, cwd: Path) -> str:
    looked = " and ".join(str(home) for home in home_folders(cwd))
    lines = [
        f"No pull named {argument!r}: looked for "
        f"{Path(RUNS_DIR) / name / SPLIT_DIR / MANIFEST_FILENAME} in {looked}."
    ]
    pulls = find_pulls(cwd)
    if pulls:
        lines.append("The pulls there are: " + ", ".join(p.name for p in pulls) + ".")
    else:
        lines.append("There are no pulls yet. Export the transfer YAML's split first.")
    return "\n".join(lines)


@dataclass
class Pull:
    name: str
    manifest: Path
    home: Path
    progress: str
    interrupted: bool = False  # the manifest says running, but no Execute is
    lock: LockInfo | None = None  # the live lock of the Execute pulling it

    @property
    def state(self) -> str:
        if self.lock:
            return (
                f"executing since {clock_time(self.lock.started)}, heartbeat "
                f"{age_words(self.lock.age())} ago ({self.progress})"
            )
        if self.interrupted:
            return f"stopped mid-run ({self.progress})"
        return self.progress


def manifest_state(path: Path) -> tuple[str, bool]:
    """What the manifest says of its sessions, in a few words, and whether it
    says one is running."""
    try:
        manifest = Manifest.load(path)
    except (ManifestError, OSError, ValueError) as exc:
        return f"unreadable ({exc})", False
    for session in manifest.sessions:
        session.recompute_status()  # in memory only, from its phases and runs
    statuses = [session.status for session in manifest.sessions]
    running = RUNNING in statuses
    total = len(statuses)
    if not total:
        return "no sessions", False
    settled = sum(1 for status in statuses if status in (DONE, SKIPPED))
    failed = sum(1 for status in statuses if status == FAILED)
    if all(status == PENDING for status in statuses):
        return "not started", running
    if settled == total:
        return "finished", running
    words = f"{settled} of {total} sessions done"
    return (f"{words}, {failed} failed" if failed else words), running


def pull_at(name: str, manifest: Path, home: Path) -> Pull:
    progress, running = manifest_state(manifest)
    held = live_lock(manifest)
    return Pull(name, manifest, home, progress, interrupted=running and not held, lock=held)


def find_pulls(cwd: Path | None = None) -> list[Pull]:
    """Every pull under `runs/` in the home folders, the working directory's first."""
    pulls: list[Pull] = []
    seen: set[Path] = set()
    for home in home_folders(cwd):
        runs = home / RUNS_DIR
        if not runs.is_dir():
            continue
        for folder in sorted(runs.iterdir(), key=lambda p: p.name.lower()):
            manifest = folder / SPLIT_DIR / MANIFEST_FILENAME
            if not manifest.is_file() or manifest.resolve() in seen:
                continue
            seen.add(manifest.resolve())
            pulls.append(pull_at(folder.name, manifest, home))
    return pulls


def execute_command(manifest: Path, cwd: Path | None = None,
                    option: str = "--execute") -> tuple[str, Path]:
    """The command that pulls this manifest, and the folder to type it in.

    By its project's name when it sits where names find it
    (`runs/<name>/split/`); by its path otherwise.
    """
    manifest = Path(manifest).resolve()
    split, run_dir = manifest.parent, manifest.parent.parent
    if (
        manifest.name == MANIFEST_FILENAME
        and split.name == SPLIT_DIR
        and run_dir.parent.name == RUNS_DIR
    ):
        return f"python pullmanager.py {option} {run_dir.name}", run_dir.parent.parent
    here = Path(cwd or Path.cwd()).resolve()
    return f'python pullmanager.py {option} "{shown(manifest, here)}"', here


def shown(path: Path, cwd: Path | None = None) -> str:
    """A path as short as it can be written: relative when inside the folder."""
    path = Path(path).resolve()
    here = Path(cwd or Path.cwd()).resolve()
    try:
        return str(path.relative_to(here))
    except ValueError:
        return str(path)


def listing(cwd: Path | None = None, heading: str = "Which pull? Name one:",
            option: str = "--execute") -> list[str]:
    """Every pull, its state and its command: what `--execute` (or
    `--artifacts`) alone prints, and `--running` with its own heading."""
    here = Path(cwd or Path.cwd()).resolve()
    pulls = find_pulls(here)
    if not pulls:
        return [
            f"No pulls under {Path(RUNS_DIR)}{os.sep} yet. Export a transfer YAML's split "
            "first (Export split in the launcher).",
        ]
    width = max(len(p.name) for p in pulls)
    state_width = max(len(p.state) for p in pulls)
    lines = [heading, ""]
    for pull in pulls:
        command, folder = execute_command(pull.manifest, here, option)
        where = "" if folder.resolve() == here else f"   (in {folder})"
        lines.append(f"  {pull.name:<{width}}  {pull.state:<{state_width}}  {command}{where}")
    return lines
