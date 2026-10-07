"""Finding a pull by its project's name, and listing the pulls there are (D66).

A pull lives in `runs/<project>/split/pullmanifest.yaml` (D57), `<project>`
named from the blueprint's file name. So `--execute IBD_Ancestry`,
`--execute "IBD Ancestry"` and `--execute IBD_Ancestry_blueprint.yaml` all mean
`runs/IBD_Ancestry/split/pullmanifest.yaml`, and a manifest path still works.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .lock import LockInfo, age_words, clock_time, live_lock
from .manifest import (
    FINISHED,
    FINISHED_WITH_ERRORS,
    STOPPED_BY_USER,
    STOPPED_WITH_ERRORS,
    Manifest,
    ManifestError,
)
from .models import DONE, FAILED, PENDING, RUNNING, SKIPPED
from .yaml_io import load_yaml

RUNS_DIR = "runs"  # the default; datascope.json may say otherwise (D111)
# The run folder (D142): the manifest, the latest log and the parquets at its
# top; earlier logs in older_logs/; the split and SQL under pull_files/.
PULL_FILES_DIR = "pull_files"
SPLIT_DIR = "split"
SQL_DIR = "sql"
OLDER_LOGS_DIR = "older_logs"
MANIFEST_FILENAME = "pullmanifest.yaml"
# Dropped from a blueprint's file name to name its run folder (D57, D162);
# `_transfer` is what blueprints were called before, `_temp` intakes before D95.
RUN_NAME_SUFFIXES = ("_blueprint", "_transfer", "_intake", "_temp")
BLUEPRINT_SUFFIX = "_blueprint.yaml"
YAML_SUFFIXES = (".yaml", ".yml")


class PullNotFound(RuntimeError):
    """Raised when a name matches no pull; the message lists the ones there are."""


def run_folder_name(template: str | Path) -> str:
    """`<project>` in `runs/<project>/` (D57): the blueprint's file name
    without `.yaml` and without `_blueprint`, `_transfer`, `_intake` or `_temp`
    (D95, D162).

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

    A blueprint's name loses `.yaml` and `_transfer` as the split's does.
    Anything else is only cleaned: a dot becomes an underscore rather than
    ending the name.
    """
    text = str(text).strip()
    if text.lower().endswith(YAML_SUFFIXES):
        return run_folder_name(text)
    name = Path(text.replace("\\", "/")).name
    return run_folder_name(name + ".yaml")


def working_blueprints(home: Path) -> dict[str, Path]:
    """Each project's working blueprint, by its run folder's name (D162):
    `YAMLs/temp/<project>_blueprint.yaml`, else a blueprint, under either name,
    beside scope.py, where bundles placed them before."""
    home = Path(home)
    temp = home / "YAMLs" / "temp"
    candidates = sorted(temp.glob(f"*{BLUEPRINT_SUFFIX}")) if temp.is_dir() else []
    candidates += sorted(home.glob(f"*{BLUEPRINT_SUFFIX}")) + sorted(home.glob("*_transfer.yaml"))
    found: dict[str, Path] = {}
    seen: set[str] = set()
    for path in candidates:
        project = run_folder_name(path.name)
        if project.lower() not in seen:
            seen.add(project.lower())
            found[project] = path
    return found


def record_blueprint(run_dir: Path) -> Path | None:
    """The blueprint the split was made from, kept beside its manifest (D142)."""
    run_dir = Path(run_dir)
    try:
        for path in sorted(run_dir.glob("*.yaml")):
            if path.name.lower() != MANIFEST_FILENAME and run_folder_name(path.name).lower() == run_dir.name.lower():
                return path
    except OSError:
        return None
    return None


def home_folders(cwd: Path | None = None) -> list[Path]:
    """Where `runs/` is looked for: the working directory, then the folder
    holding `scope.py`, which the extracted runtime sits beside (D63, D123)."""
    here = Path(cwd or Path.cwd()).resolve()
    beside = Path(__file__).resolve().parents[2]
    return [here] if beside == here else [here, beside]


def run_folder(manifest: str | Path) -> Path:
    """`runs/<project>`: the manifest's own folder (D142)."""
    return Path(manifest).parent


def logs_folder(manifest: str | Path) -> Path:
    """Where Execute writes its log (D68): the run folder itself, where the
    latest sits; the ones before it are in `older_logs/` (D142)."""
    return run_folder(manifest)


def older_logs_folder(manifest: str | Path) -> Path:
    return run_folder(manifest) / OLDER_LOGS_DIR


def split_folder(manifest: str | Path) -> Path:
    return run_folder(manifest) / PULL_FILES_DIR / SPLIT_DIR


def sql_folder(run_dir: str | Path) -> Path:
    return Path(run_dir) / PULL_FILES_DIR / SQL_DIR


def newest_log(manifest: str | Path) -> Path | None:
    """The latest `execute-<date>-<time>.log`; the names sort by time."""
    try:
        logs = sorted(logs_folder(manifest).glob("execute-*.log"))
        if not logs:
            logs = sorted(older_logs_folder(manifest).glob("execute-*.log"))
    except OSError:
        return None
    return logs[-1] if logs else None


def is_manifest_file(path: Path) -> bool:
    """A split's manifest, rather than a blueprint: it lists sessions."""
    if path.name.lower() == MANIFEST_FILENAME:
        return True
    try:
        data = load_yaml(path)
    except Exception:
        return False
    return isinstance(data, dict) and "sessions" in data


def manifest_in(home: Path, name: str) -> Path | None:
    """`<home>/runs/<name>/split/pullmanifest.yaml`, matching the name in any case."""
    runs = config.runs_dir(home)
    if not runs.is_dir():
        return None
    # Listed rather than looked up, so the folder's own spelling comes back.
    matches = [
        folder for folder in sorted(runs.iterdir())
        if folder.name.lower() == name.lower()
        and (folder / MANIFEST_FILENAME).is_file()
    ]
    exact = [folder for folder in matches if folder.name == name]
    chosen = (exact or matches or [None])[0]
    return chosen / MANIFEST_FILENAME if chosen else None


def resolve(argument: str, cwd: Path | None = None) -> Path:
    """The manifest a name or path means. Raises PullNotFound, listing pulls."""
    here = Path(cwd or Path.cwd())
    given = Path(argument) if Path(argument).is_absolute() else here / argument
    if given.is_file() and is_manifest_file(given):
        return given
    if given.is_dir():
        candidate = given / MANIFEST_FILENAME
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
        f"{config.runs_setting(cwd) / name / MANIFEST_FILENAME} in {looked}."
    ]
    pulls = find_pulls(cwd)
    if pulls:
        lines.append("The pulls there are: " + ", ".join(p.name for p in pulls) + ".")
    else:
        lines.append("There are no pulls yet. Export the blueprint's split first.")
    return "\n".join(lines)


@dataclass
class Pull:
    name: str
    manifest: Path
    home: Path
    progress: str
    interrupted: bool = False  # the manifest says running, but no Execute is
    lock: LockInfo | None = None  # the live lock of the Execute pulling it
    # Once it has run and is not executing (D140): finished, finished with
    # errors, stopped by user or stopped with errors. Empty: never executed.
    outcome: str = ""

    @property
    def state(self) -> str:
        if self.lock:
            return (
                f"executing since {clock_time(self.lock.started)}, heartbeat "
                f"{age_words(self.lock.age())} ago ({self.progress})"
            )
        if not self.outcome or self.outcome == FINISHED:
            return self.progress
        return f"{self.outcome} ({self.progress})"


def manifest_state(path: Path) -> tuple[str, bool, str]:
    """What the manifest says of its sessions, in a few words, whether it
    says one is running, and how it stands if it is not executing (D140)."""
    try:
        manifest = Manifest.load(path)
    except (ManifestError, OSError, ValueError) as exc:
        return f"unreadable ({exc})", False, STOPPED_WITH_ERRORS
    progress, running = sessions_state(manifest)
    return progress, running, outcome(manifest, progress, running)


def outcome(manifest: Manifest, progress: str, running: bool) -> str:
    """How a pull that is not executing stands (D140), from its sessions and
    what its last Execute wrote as it ended. Every session done is finished,
    however the process ended; a stop by hand is the user's; an Execute that
    never wrote its end, or left a step running, stopped on an error."""
    last = manifest.last_execute
    if progress == "not started" and not last:
        return ""
    if progress == "finished":
        return FINISHED
    if last.get("how") == STOPPED_BY_USER:
        return STOPPED_BY_USER
    if running or last.get("how") == STOPPED_WITH_ERRORS or (last and not last.get("ended_at")):
        return STOPPED_WITH_ERRORS
    return FINISHED_WITH_ERRORS


def sessions_state(manifest: Manifest) -> tuple[str, bool]:
    """Its sessions in a few words, and whether one says it is running."""
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


def record_stopped_by_user(manifest_path: Path, exit_code: int | None) -> None:
    """Stop ends Execute from outside, so it cannot write its own end (D140):
    the window that stopped it writes it, unless Execute already did."""
    manifest = Manifest.load(manifest_path)
    if not manifest.last_execute.get("ended_at"):
        manifest.execute_ended(exit_code, STOPPED_BY_USER)


def pull_at(name: str, manifest: Path, home: Path) -> Pull:
    progress, running, how = manifest_state(manifest)
    held = live_lock(manifest)
    return Pull(name, manifest, home, progress, interrupted=running and not held, lock=held,
                outcome=how)


def find_pulls(cwd: Path | None = None) -> list[Pull]:
    """Every pull under `runs/` in the home folders, the working directory's first."""
    pulls: list[Pull] = []
    seen: set[Path] = set()
    for home in home_folders(cwd):
        runs = config.runs_dir(home)
        if not runs.is_dir():
            continue
        for folder in sorted(runs.iterdir(), key=lambda p: p.name.lower()):
            manifest = folder / MANIFEST_FILENAME
            if not manifest.is_file() or manifest.resolve() in seen:
                continue
            seen.add(manifest.resolve())
            pulls.append(pull_at(folder.name, manifest, home))
    return pulls


def execute_command(manifest: Path, cwd: Path | None = None,
                    option: str = "--execute") -> tuple[str, Path]:
    """The command that pulls this manifest, and the folder to type it in.

    By its project's name when it sits where names find it
    (`runs/<name>/`); by its path otherwise.
    """
    manifest = Path(manifest).resolve()
    run_dir = manifest.parent
    here = Path(cwd or Path.cwd()).resolve()
    if manifest.name == MANIFEST_FILENAME:
        for home in [here, *home_folders(here)]:
            if config.runs_dir(home).resolve() == run_dir.parent:
                return f"python scope.py {option} {run_dir.name}", Path(home).resolve()
        if run_dir.parent.name == RUNS_DIR:
            return f"python scope.py {option} {run_dir.name}", run_dir.parent.parent
    return f'python scope.py {option} "{shown(manifest, here)}"', here


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
            f"No pulls under {config.runs_setting(here)}{os.sep} yet. Export a blueprint's split "
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
