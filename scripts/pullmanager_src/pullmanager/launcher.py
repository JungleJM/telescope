"""Logic behind the desktop launcher, with no tkinter in it.

The launcher is a front end over the command line, not a second
implementation: every action runs the same command a person would type, as a
subprocess. That keeps database work out of the UI thread, means a long pull
cannot freeze the window, gives Stop something real to terminate, and
guarantees the GUI never behaves differently from the CLI.

Everything testable lives here. The tkinter view only wires widgets to it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .manifest import Manifest, ManifestError

SETTINGS_FILENAME = ".pullmanager-gui.json"
MANIFEST_FILENAME = "pullmanifest.yaml"


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
    """

    template: str = ""
    datadictionary: str = ""
    split_dir: str = "split"
    sql_dir: str = "sql"

    def manifest(self) -> Path:
        return Path(self.split_dir) / MANIFEST_FILENAME


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
        "--export-split", "--out-dir", _require(paths.split_dir, "split folder"),
    ]


def command_dry_run(tools: Tools, paths: Paths, options: Options) -> list[str]:
    return [
        sys.executable, str(tools.pullmanager), "--dry-run", str(paths.manifest()),
        "--out-dir", _require(paths.sql_dir, "SQL folder"), *_resume_flags(options),
    ]


def command_execute(tools: Tools, paths: Paths, options: Options) -> list[str]:
    return [
        sys.executable, str(tools.pullmanager), "--execute", str(paths.manifest()),
        *_resume_flags(options),
    ]


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
    """Remembered choices live in the working directory, beside your files.

    Not inside the extracted bundle, which is replaced on every update.
    """
    return Path(directory or Path.cwd()) / SETTINGS_FILENAME


def load_settings(directory: Path | None = None) -> Paths:
    path = settings_path(directory)
    if not path.is_file():
        return Paths()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Paths()
    known = {f for f in Paths.__dataclass_fields__}
    return Paths(**{k: str(v) for k, v in data.items() if k in known})


def save_settings(paths: Paths, directory: Path | None = None) -> Path:
    path = settings_path(directory)
    path.write_text(json.dumps(asdict(paths), indent=2) + "\n", encoding="utf-8")
    return path
