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
from .models import RUNNING
from . import config
from .pulls import MANIFEST_FILENAME, RUNS_DIR, newest_log, run_folder_name
from .pulls import sql_folder as pulls_sql_folder
from .perkey import shown as per_key_shown

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

    A blank split or SQL folder is the project's own: the split writes its
    manifest into `runs/<project>` and the rest under `pull_files/split`, and
    the SQL goes to `pull_files/sql`, so two projects never share one (D57,
    D142).
    """

    template: str = ""
    split_dir: str = ""
    sql_dir: str = ""
    # The runs folder, relative to the working folder: datascope.json's, filled
    # in by the window from its own working folder (D111). Not remembered.
    runs: str = config.RUNS_DEFAULT

    def run_dir(self) -> Path:
        return Path(self.runs or config.RUNS_DEFAULT) / run_folder_name(_require(self.template, "transfer YAML"))

    def split_folder(self) -> Path:
        """Where Export split writes: the folder that holds the manifest."""
        chosen = self.split_dir.strip()
        return Path(chosen) if chosen else self.run_dir()

    def sql_folder(self) -> Path:
        chosen = self.sql_dir.strip()
        return Path(chosen) if chosen else pulls_sql_folder(self.run_dir())

    def manifest(self) -> Path:
        return self.split_folder() / MANIFEST_FILENAME


@dataclass
class Options:
    retry_failed: bool = False
    repull: bool = False  # start every session over, finished ones included
    repull_sessions: tuple[str, ...] = ()  # only these start over (D158); `all` is repull


def _require(value: str, what: str) -> str:
    if not str(value).strip():
        raise LauncherError(f"Choose a {what} first.")
    return str(value).strip()


def _yaml_inputs(paths: Paths) -> list[str]:
    # The bundle's dictionary, always: a chosen one, once saved, went on being
    # passed after the bundle's was newer (D150).
    return ["--template", _require(paths.template, "transfer YAML")]


def _resume_flags(options: Options) -> list[str]:
    flags = []
    if options.retry_failed:
        flags.append("--retry-failed")
    if options.repull or any(name.lower() == "all" for name in options.repull_sessions):
        flags.append("--repull")
    else:
        for name in options.repull_sessions:
            flags += ["--repull-session", name]
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


def command_backup_all(tools: Tools) -> list[str]:
    """Back up every pull to the backup folder (D149)."""
    return [sys.executable, str(tools.pullmanager), "--backup"]


def command_scan_runs(tools: Tools) -> list[str]:
    """What every pull built against what it packaged (D152)."""
    return [sys.executable, str(tools.pullmanager), "--scan-runs"]


def command_audit_dictionary(tools: Tools) -> list[str]:
    """The dictionary against Cosmos's columns (D155)."""
    return [sys.executable, str(tools.pullmanager), "--audit-dictionary"]


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
        self._held_cr = False

    def read(self, path: Path | None) -> tuple[bool, str]:
        """(switched, text). Switched means a different log from last time,
        and the text is then all of it; otherwise only what is new."""
        switched = path != self.path
        if switched:
            self.path = path
            self._position = 0
            # A character cut in two by a read is completed by the next.
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self._held_cr = False
        if path is None:
            return switched, ""
        try:
            with open(path, "rb") as handle:
                handle.seek(self._position)
                data = handle.read()
        except OSError:
            return switched, ""
        self._position += len(data)
        return switched, self._newlines(self._decoder.decode(data))

    def _newlines(self, text: str) -> str:
        """Windows writes `\\r\\n`; the Pull Log shows `\\n`. A `\\r` that ends a
        read waits for the next, in case its `\\n` is there."""
        if self._held_cr:
            text = "\r" + text
        self._held_cr = text.endswith("\r")
        if self._held_cr:
            text = text[:-1]
        return text.replace("\r\n", "\n")


@dataclass
class StatusRow:
    session: str
    kind: str
    name: str
    status: str
    rows: str = ""
    duration: str = ""
    detail: str = ""
    # Where it is in the manifest (D144): a phase's name or a run's run_id;
    # for a table, the step that landed it. Whether it holds an error.
    key: str = ""
    has_error: bool = False
    # A table's rows per join key (D157): median, 90th percentile, maximum.
    median: str = ""
    p90: str = ""
    max: str = ""


def in_flight_text(outputs: dict) -> str:
    """A running step's position and table (D166): `c2of13 · OtherHospitalizations,
    since 11:01`; '' when it records none."""
    flight = outputs.get("in_flight") or {}
    if not flight.get("table"):
        return ""
    where = " ".join(str(outputs[k]) for k in ("value", "chunk") if outputs.get(k))
    table = f"{flight['table']}, since {flight['since']}" if flight.get("since") else str(flight["table"])
    return f"{where} · {table}" if where else table


def manifest_rows(manifest_path: Path, text: str | None = None) -> list[StatusRow]:
    """The manifest flattened into one row per phase and run; from `text`
    when the file has been read already (D154)."""
    manifest = Manifest.load(manifest_path) if text is None else Manifest.from_text(text, manifest_path)
    rows: list[StatusRow] = []
    for session in manifest.sessions:
        rows.append(StatusRow(session.session_id, "session", session.session_id, session.status,
                              key=session.session_id))
        for child in [*session.phases, *session.runs]:
            is_phase = child in session.phases
            name = child.name if is_phase else " ".join(filter(None, (
                child.group, (child.batch or {}).get("name")))) or child.label
            duration = (child.data.get("duration") or {}).get("display", "")
            detail = (child.error or {}).get("message") or child.note or ""
            if child.status == RUNNING:
                detail = in_flight_text(child.outputs) or detail
            # A run's rows are its tables', listed under it; never one total.
            shown = None if not is_phase else child.rows
            rows.append(
                StatusRow(
                    session=session.session_id,
                    kind="phase" if is_phase else "run",
                    name=str(name),
                    status=child.status,
                    rows="" if shown is None else f"{shown:,}",
                    duration=duration,
                    detail=str(detail),
                    key=child.name if is_phase else child.run_id,
                    has_error=bool((child.error or {}).get("message")),
                )
            )
            # Each table the step landed, under it, with its own rows (D137).
            per_key = child.outputs.get("per_key") or {}
            for dest, count in (child.outputs.get("table_rows") or {}).items():
                measured = per_key.get(dest) or {}
                rows.append(StatusRow(session.session_id, "table", str(dest), "",
                                      rows=f"{count:,}",
                                      key=child.name if is_phase else child.run_id,
                                      median=per_key_shown(measured.get("median")),
                                      p90=per_key_shown(measured.get("p90")),
                                      max=per_key_shown(measured.get("max"))))
    return rows


# ------------------------------------------------------- the manifest as text

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: list[str], start: int) -> int:
    """The line after the block that opens at `start`: the first later line,
    not blank, indented no deeper than the line that opens it (for a list
    item, its dash)."""
    depth = _indent(lines[start])
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and _indent(lines[index]) <= depth:
            return index
    return len(lines)


def _find(lines: list[str], pattern: str, start: int = 0, end: int | None = None) -> int | None:
    import re

    compiled = re.compile(pattern)
    for index in range(start, len(lines) if end is None else end):
        if compiled.match(lines[index]):
            return index
    return None


def _item_with(lines: list[str], key: str, value: str, start: int = 0,
               end: int | None = None) -> int | None:
    """The first line of the list item holding `key: value`, wherever in the
    item that key is written: a run's item begins with its `yaml:` line."""
    import re

    found = _find(lines, rf"^\s*(-\s+)?{re.escape(key)}:\s*['\"]?{re.escape(value)}['\"]?\s*$",
                  start, end)
    if found is None:
        return None
    if lines[found].lstrip().startswith("-"):
        return found
    depth = _indent(lines[found])
    for index in range(found - 1, start - 1, -1):
        line = lines[index]
        if line.lstrip().startswith("- ") and _indent(line) == depth - 2:
            return index
        if line.strip() and _indent(line) < depth - 2:
            break
    return found


def _error_line(lines: list[str], start: int, end: int) -> int | None:
    """The `error:` line of the entry from `start` to `end`, if it holds one:
    a block under it, or a flow mapping on its own line; not an empty one."""
    for index in range(start + 1, end):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith("error:"):
            continue
        rest = stripped[len("error:"):].strip()
        if rest in ("", "null", "~"):
            nxt = index + 1
            if nxt < end and lines[nxt].strip() and _indent(lines[nxt]) > _indent(line):
                return index
            return None
        return None if rest in ("{}",) else index
    return None


def manifest_line(text: str, row: StatusRow) -> int | None:
    """The 0-based line in the manifest's text that a Status row stands for
    (D144): the entry's error, when it has one; else the entry itself; for a
    table, its line in its step's `table_rows`. Found by the entry's own id,
    never by searching for what the error says."""
    import re

    lines = text.splitlines()
    session = _item_with(lines, "session_id", row.session)
    if session is None:
        return None
    end = _block_end(lines, session)
    if row.kind == "session":
        return session
    entry = None
    if row.key:
        entry = _item_with(lines, "run_id", row.key, session, end)
        if entry is None:
            phases = _find(lines, r"^\s*phases:\s*$", session, end)
            if phases is not None:
                entry = _find(lines, rf"^\s+{re.escape(row.key)}:\s*$", phases + 1,
                              _block_end(lines, phases))
    if entry is None:
        return None
    entry_end = _block_end(lines, entry)
    if row.kind == "table":
        found = _find(lines, rf"^\s+{re.escape(row.name)}:\s*\d", entry, entry_end)
        return entry if found is None else found
    error = _error_line(lines, entry, entry_end)
    return error if error is not None and row.has_error else entry


def manifest_span(text: str, row: StatusRow) -> tuple[int, int] | None:
    """The 0-based lines, first and after-last, to highlight for a Status
    row: its whole error, when it has one; else its one line."""
    line = manifest_line(text, row)
    if line is None:
        return None
    lines = text.splitlines()
    if lines[line].strip().startswith("error:"):
        return line, _block_end(lines, line)
    return line, line + 1


def manifest_colours(text: str) -> list[tuple[int, str]]:
    """Each 0-based line to colour and how (D144): a `status:` line by its
    status, as the Status tab colours it; each line of an error that holds a
    message, `error`."""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("status:"):
            marks.append((index, stripped[len("status:"):].strip()))
        elif stripped.startswith("error:"):
            end = _block_end(lines, index)
            if _error_line(lines, index - 1, end) == index:
                marks.extend((line, "error") for line in range(index, end))
                index = end
                continue
        index += 1
    return marks


def try_manifest_rows(manifest_path: Path, text: str | None = None) -> tuple[list[StatusRow], str]:
    """Rows, or a message saying why there are none. Never raises."""
    if text is None and not manifest_path.is_file():
        return [], f"No manifest yet at {manifest_path}. Export a split first."
    try:
        return manifest_rows(manifest_path, text), ""
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
