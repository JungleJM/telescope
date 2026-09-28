"""Logic behind the desktop launcher, with no tkinter in it.

The launcher is a front end over the command line, not a second
implementation: every action runs the same command a person would type, as a
subprocess. That keeps database work out of the UI thread, means a long pull
cannot freeze the window, gives Stop something real to terminate, and
guarantees the GUI never behaves differently from the CLI.

Execute runs in a console window of its own (D68), as it would from a
terminal: started from the window with its output piped back, it failed on the
VM before printing a line (exit code 0xC0000142). The window follows its log.

Everything testable lives here. The tkinter view only wires widgets to it.
"""

from __future__ import annotations

import codecs
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .lock import LockInfo
from .manifest import Manifest, ManifestError
from . import config
from .pulls import MANIFEST_FILENAME, RUNS_DIR, newest_log, run_folder_name

# Windows can give Execute a console window of its own; elsewhere it runs
# unseen and its output is read from its log.
CAN_OPEN_CONSOLE = hasattr(subprocess, "CREATE_NEW_CONSOLE")

SETTINGS_FILENAME = ".pullmanager-gui.json"
# Where it lives in the working folder: under runs/, with everything else the
# app makes, not loose beside your files. Once it was at the folder's top.
SETTINGS_FOLDER = "runs"  # the default; datascope.json's runs folder where it names one
# What an older launcher saved as if chosen: it meant "the default" (D57).
OLD_DEFAULT_FOLDERS = {"split_dir": "split", "sql_dir": "sql"}


class LauncherError(RuntimeError):
    """Raised when the tools cannot be found or a command cannot be built."""


@dataclass
class Tools:
    """Where the two command-line programs live."""

    pullmanager: Path
    make_yaml: Path


def locate_tools(package_dir: Path | None = None) -> Tools:
    """Find pullmanager.py and makeYaml.py from this package's location.

    Two layouts are supported: the extracted bundle, where both sit under one
    root, and the source tree, where makeYaml is a sibling of pullmanager_src.
    """
    package_dir = Path(package_dir or Path(__file__).resolve().parent)
    root = package_dir.parent
    candidates = [
        Tools(root / "pullmanager.py", root / "scripts" / "makeYaml.py"),
        Tools(root / "pullmanager.py", root.parent / "makeYaml.py"),
    ]
    for tools in candidates:
        if tools.pullmanager.is_file() and tools.make_yaml.is_file():
            return tools
    raise LauncherError(
        "Could not find pullmanager.py and makeYaml.py next to the launcher. "
        "Run it from an extracted bundle."
    )


@dataclass
class Paths:
    """What the user has chosen. Blank optional fields fall back to defaults.

    `template` is a transfer YAML (D49): recipes already written out, so there
    is no recipes file to choose. Settings saved by an older launcher may still
    name one; unknown keys are ignored on load.

    A blank split or SQL folder is the project's own, `runs/<project>/split`
    and `runs/<project>/sql`, so two projects never share one (D57).
    """

    template: str = ""
    datadictionary: str = ""
    split_dir: str = ""
    sql_dir: str = ""
    # The runs folder, relative to the working folder: datascope.json's, filled
    # in by the window from its own working folder (D111). Not remembered.
    runs: str = config.RUNS_DEFAULT

    def run_dir(self) -> Path:
        return Path(self.runs or config.RUNS_DEFAULT) / run_folder_name(_require(self.template, "transfer YAML"))

    def split_folder(self) -> Path:
        chosen = self.split_dir.strip()
        return Path(chosen) if chosen else self.run_dir() / "split"

    def sql_folder(self) -> Path:
        chosen = self.sql_dir.strip()
        return Path(chosen) if chosen else self.run_dir() / "sql"

    def manifest(self) -> Path:
        return self.split_folder() / MANIFEST_FILENAME


@dataclass
class Options:
    retry_failed: bool = False
    repull: bool = False  # start every session over, finished ones included


def _require(value: str, what: str) -> str:
    if not str(value).strip():
        raise LauncherError(f"Choose a {what} first.")
    return str(value).strip()


def _yaml_inputs(paths: Paths) -> list[str]:
    args = ["--template", _require(paths.template, "transfer YAML")]
    # Optional: blank means the tool's own default, which is the bundled copy.
    if paths.datadictionary.strip():
        args += ["--datadictionary", paths.datadictionary.strip()]
    return args


def _resume_flags(options: Options) -> list[str]:
    flags = []
    if options.retry_failed:
        flags.append("--retry-failed")
    if options.repull:
        flags.append("--repull")
    return flags


def command_validate(tools: Tools, paths: Paths) -> list[str]:
    return [sys.executable, str(tools.make_yaml), *_yaml_inputs(paths), "--validate"]


def command_export_split(tools: Tools, paths: Paths) -> list[str]:
    return [
        sys.executable, str(tools.make_yaml), *_yaml_inputs(paths),
        "--export-split", "--out-dir", str(paths.split_folder()),
    ]


def command_dry_run(tools: Tools, paths: Paths, options: Options) -> list[str]:
    return [
        sys.executable, str(tools.pullmanager), "--dry-run", str(paths.manifest()),
        "--out-dir", str(paths.sql_folder()), *_resume_flags(options),
    ]


def execute_target(paths: Paths) -> str:
    """What `--execute` is given: the project's name (D66), or the manifest's
    path when a split folder was typed, since a name finds only `runs/`."""
    if paths.split_dir.strip():
        return str(paths.manifest())
    return paths.run_dir().name


def command_execute(
    tools: Tools, paths: Paths, options: Options, keep_open: bool = False
) -> list[str]:
    command = [
        sys.executable, str(tools.pullmanager), "--execute", execute_target(paths),
        *_resume_flags(options),
    ]
    return command + ["--keep-open"] if keep_open else command


def command_artifacts(tools: Tools, paths: Paths) -> list[str]:
    """Package the pull's finished tables (D72), by its name as Execute is."""
    return [sys.executable, str(tools.pullmanager), "--artifacts", execute_target(paths)]


def child_environment() -> dict[str, str]:
    """Stream output live, in UTF-8, whatever the console code page is.

    Unbuffered, or a long pull would print nothing until it finished. UTF-8,
    or a Windows cp1252 console would mangle anything outside ASCII.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class CommandRunner:
    """One subprocess at a time, its output delivered through a queue.

    A reader thread feeds the queue; the UI drains it with poll(), so tkinter
    is only ever touched from its own thread.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self.returncode: int | None = None
        self.command: list[str] = []

    @property
    def running(self) -> bool:
        return self._process is not None and self.returncode is None

    def start(self, command: list[str], cwd: str | Path | None = None) -> None:
        if self.running:
            raise LauncherError("A command is already running. Stop it first.")
        self.command = list(command)
        self.returncode = None
        self._queue = queue.Queue()
        self._process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment(),
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._queue.put(line.rstrip("\n"))
        self._process.wait()
        self._queue.put(None)

    def poll(self) -> list[str]:
        """Lines produced since the last poll; notices when the process ends."""
        lines: list[str] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                if self._process is not None:
                    self.returncode = self._process.returncode
                break
            lines.append(item)
        return lines

    def stop(self) -> None:
        """Terminate the running command.

        Abrupt by design. The manifest node it was working on stays `running`,
        which a resume already treats as interrupted and replays; SQL Server
        rolls back the open transaction when the connection drops.
        """
        if self._process is not None and self.returncode is None:
            self._process.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        """Block until the command ends. For tests and scripted use."""
        if self._process is None:
            return None
        self._process.wait(timeout=timeout)
        if self._reader is not None:
            self._reader.join(timeout=timeout)
        return self._process.returncode


class ConsoleRunner:
    """Execute in a console window of its own (D68).

    Its output goes to that window and its log, never through the launcher,
    which follows the log instead. Only the latest is tracked: a finished pull
    whose window waits for `exit` does not stop another from starting.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self.command: list[str] = []
        self.started = 0.0
        self.returncode: int | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self.poll() is None

    def start(self, command: list[str], cwd: str | Path | None = None) -> None:
        if CAN_OPEN_CONSOLE:
            streams = {"creationflags": subprocess.CREATE_NEW_CONSOLE}
        else:
            streams = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                       "stderr": subprocess.DEVNULL}
        self.command = list(command)
        self.returncode = None
        self.started = time.time()
        self._process = subprocess.Popen(
            command, cwd=str(cwd) if cwd else None, env=child_environment(), **streams
        )

    def poll(self) -> int | None:
        if self._process is not None:
            self.returncode = self._process.poll()
        return self.returncode

    def stop(self) -> None:
        if self.alive:
            self._process.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        if self._process is None:
            return None
        self._process.wait(timeout=timeout)
        return self.poll()


def exit_code_words(code: int | None) -> str:
    """`3221225794 (0xC0000142)`: Windows failures read better in hex."""
    if code is None:
        return "none"
    if code > 0xFFFF:
        return f"{code} (0x{code & 0xFFFFFFFF:08X})"
    return str(code)


def pull_log(manifest: Path | None, lock: LockInfo | None = None) -> Path | None:
    """The log to show: the one the live Execute writes, else the newest."""
    if manifest is None:
        return None
    if lock is not None and lock.log:
        path = Path(lock.log)  # written absolute by Execute
        if path.is_file():
            return path
    return newest_log(manifest)


class LogFollower:
    """What a growing log has added since it was last read."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._position = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, path: Path | None) -> tuple[bool, str]:
        """(switched, text). Switched means a different log from last time,
        and the text is then all of it; otherwise only what is new."""
        switched = path != self.path
        if switched:
            self.path = path
            self._position = 0
            # A character cut in two by a read is completed by the next.
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        if path is None:
            return switched, ""
        try:
            with open(path, "rb") as handle:
                handle.seek(self._position)
                data = handle.read()
        except OSError:
            return switched, ""
        self._position += len(data)
        return switched, self._decoder.decode(data)


@dataclass
class StatusRow:
    session: str
    kind: str
    name: str
    status: str
    rows: str = ""
    duration: str = ""
    detail: str = ""


def manifest_rows(manifest_path: Path) -> list[StatusRow]:
    """The manifest flattened into one row per phase and run."""
    manifest = Manifest.load(manifest_path)
    rows: list[StatusRow] = []
    for session in manifest.sessions:
        rows.append(StatusRow(session.session_id, "session", session.session_id, session.status))
        for child in [*session.phases, *session.runs]:
            is_phase = child in session.phases
            name = child.name if is_phase else (child.batch or {}).get("name") or child.label
            duration = (child.data.get("duration") or {}).get("display", "")
            detail = (child.error or {}).get("message") or child.note or ""
            rows.append(
                StatusRow(
                    session=session.session_id,
                    kind="phase" if is_phase else "run",
                    name=str(name),
                    status=child.status,
                    rows="" if child.rows is None else f"{child.rows:,}",
                    duration=duration,
                    detail=str(detail),
                )
            )
    return rows


def try_manifest_rows(manifest_path: Path) -> tuple[list[StatusRow], str]:
    """Rows, or a message saying why there are none. Never raises."""
    if not manifest_path.is_file():
        return [], f"No manifest yet at {manifest_path}. Export a split first."
    try:
        return manifest_rows(manifest_path), ""
    except (ManifestError, OSError, ValueError) as exc:
        return [], f"Could not read {manifest_path}: {exc}"


def settings_path(directory: Path | None = None) -> Path:
    """Remembered choices live in the working directory's runs/ folder.

    Not inside the extracted bundle, which is replaced on every update.
    """
    return config.runs_dir(Path(directory or Path.cwd())) / SETTINGS_FILENAME


def move_old_settings(directory: Path | None = None) -> None:
    """A settings file at the working folder's top, where it used to be, moves
    to runs/; if one is already there, that one is kept and the old removed."""
    old = Path(directory or Path.cwd()) / SETTINGS_FILENAME
    if not old.is_file():
        return
    new = settings_path(directory)
    try:
        if not new.is_file():
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
        old.unlink()
    except OSError:
        pass  # remembering choices is a convenience; the old file still loads


def load_settings(directory: Path | None = None) -> Paths:
    move_old_settings(directory)
    path = settings_path(directory)
    if not path.is_file():
        path = Path(directory or Path.cwd()) / SETTINGS_FILENAME  # could not be moved
    if not path.is_file():
        return Paths()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Paths()
    known = {f for f in Paths.__dataclass_fields__} - {"runs"}
    chosen = {k: str(v) for k, v in data.items() if k in known}
    for key, old_default in OLD_DEFAULT_FOLDERS.items():
        if chosen.get(key, "").strip() == old_default:
            chosen[key] = ""
    return Paths(**chosen)


def save_settings(paths: Paths, directory: Path | None = None) -> Path:
    path = settings_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    remembered = {k: v for k, v in asdict(paths).items() if k != "runs"}
    path.write_text(json.dumps(remembered, indent=2) + "\n", encoding="utf-8")
    return path
