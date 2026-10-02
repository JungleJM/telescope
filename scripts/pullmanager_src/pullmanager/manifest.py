"""Load, mutate, and write back `pullmanifest.yaml`.

The manifest is YAML Manager's plan on the way in and Pullmanager's status
document on the way out. Nodes wrap the loaded mappings in place so keys this
version does not understand survive a round trip untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from . import MANIFEST_VERSION
from .models import (
    BLOCKED,
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    SETTLED_STATUSES,
    SKIPPED,
    duration_block,
    new_epoch,
    now_iso,
    validate_status,
)
from .yaml_io import dump_yaml, load_yaml, parse_yaml, replace_patiently

PHASE_ORDER = ("setup", "upload_cohorts", "pk")

# How the latest Execute ended (D140), as `last_execute.how` records it.
FINISHED = "finished"
FINISHED_WITH_ERRORS = "finished with errors"
STOPPED_BY_USER = "stopped by user"
STOPPED_WITH_ERRORS = "stopped with errors"


class ManifestError(ValueError):
    """Raised when a manifest does not match the expected contract."""


class Node:
    """A manifest entry carrying status and timing fields."""

    def __init__(self, data: dict[str, Any], label: str, session: "Session | None" = None):
        self._data = data
        self.label = label
        self._session = session

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def status(self) -> str:
        return self._data.get("status", PENDING)

    @status.setter
    def status(self, value: str) -> None:
        self._data["status"] = validate_status(value)

    @property
    def yaml(self) -> str | None:
        return self._data.get("yaml")

    @property
    def rows(self) -> int | None:
        return self._data.get("rows")

    @property
    def outputs(self) -> dict[str, Any]:
        return self._data.setdefault("outputs", {})

    @property
    def error(self) -> dict[str, Any] | None:
        return self._data.get("error")

    @property
    def note(self) -> str | None:
        return self._data.get("note")

    @property
    def epoch(self) -> str | None:
        """The server connection this node last completed under."""
        return self._data.get("epoch")

    def is_stale(self, current_epoch: str | None) -> bool:
        """True when this finished under a connection that no longer exists.

        Server-side output (global temps, uploaded tables) from a stale node is
        gone even though the status still reads `done`. Local Projects tables
        are permanent and survive regardless.
        """
        if self.status != DONE:
            return False
        return self.epoch is not None and self.epoch != current_epoch

    def start(self) -> None:
        self.status = RUNNING
        self._data["started_at"] = now_iso()
        self._data["finished_at"] = None
        self._data["error"] = None
        self._data.pop("duration", None)
        # A rerun's rows are its own: none carried over from the last attempt.
        self._data.pop("rows", None)
        self.outputs.pop("table_rows", None)

    def finish(self, rows: int | None = None, outputs: dict[str, Any] | None = None) -> None:
        self.status = DONE
        self._stamp_finish()
        if rows is not None:
            self._data["rows"] = rows
        if outputs:
            self.outputs.update(outputs)
        self._data["error"] = None

    def fail(self, message: str, detail: str | None = None) -> None:
        self.status = FAILED
        self._stamp_finish()
        self._data["error"] = {"message": message, "detail": detail}

    def reset(self, reason: str | None = None) -> None:
        """Back to pending, as if never run: everything must be pulled again."""
        self.status = PENDING
        for key in ("started_at", "finished_at", "rows", "error", "epoch", "duration"):
            self._data.pop(key, None)
        self._data["outputs"] = {}
        if reason:
            self._data["note"] = reason
        else:
            self._data.pop("note", None)

    def skip(self, reason: str | None = None) -> None:
        self._settle_without_running(SKIPPED, reason)

    def block(self, reason: str | None = None) -> None:
        self._settle_without_running(BLOCKED, reason)

    def _settle_without_running(self, status: str, reason: str | None) -> None:
        # Reasons go in `note`, not `error`: a skipped phase is not a failure,
        # and an `error` block on it would read like one.
        self.status = status
        if reason:
            self._data["note"] = reason

    def _stamp_finish(self) -> None:
        self._data["finished_at"] = now_iso()
        if self._session is not None and self._session.epoch is not None:
            self._data["epoch"] = self._session.epoch
        duration = duration_block(self._data.get("started_at"), self._data["finished_at"])
        if duration is not None:
            self._data["duration"] = duration

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label} {self.status}>"


class Phase(Node):
    def __init__(self, name: str, data: dict[str, Any], session: "Session"):
        super().__init__(data, f"{session.label}/{name}", session)
        self.name = name

    @property
    def pk_source(self) -> dict[str, Any] | None:
        return self._data.get("pk_source")


class Run(Node):
    def __init__(self, data: dict[str, Any], session: "Session"):
        super().__init__(data, str(data.get("run_id")), session)

    @property
    def run_id(self) -> str:
        return self._data["run_id"]

    @property
    def batch(self) -> dict[str, Any] | None:
        return self._data.get("batch")

    @property
    def group(self) -> str | None:
        """Its table group (D134), pulled on a Cosmos connection of its own; None if none."""
        return self._data.get("group")


class Session(Node):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data, str(data.get("session_id")))
        raw_phases = data.get("phases") or {}
        unknown = set(raw_phases) - set(PHASE_ORDER)
        if unknown:
            raise ManifestError(
                f"Session {self.label!r} has unknown phase(s): {', '.join(sorted(unknown))}. "
                f"Expected only: {', '.join(PHASE_ORDER)}"
            )
        self.phases = [
            Phase(name, raw_phases[name], self)
            for name in PHASE_ORDER
            if name in raw_phases
        ]
        self.runs = [Run(run, self) for run in data.get("runs") or []]

    @property
    def session_id(self) -> str:
        return self._data["session_id"]

    @property
    def pk_table(self) -> str | None:
        return self._data.get("pk_table")

    @property
    def runtime(self) -> dict[str, Any]:
        """Facts discovered when the session's connection opened."""
        return self._data.setdefault("runtime", {})

    @property
    def epoch(self) -> str | None:
        return self.runtime.get("epoch")

    def begin_epoch(self, linked_server: str | None = None) -> str:
        """Open a new server connection scope for this session.

        `linked_server` is always overwritten, never left in place: the Cosmos
        instance name changes on every connection, so carrying the previous
        one forward would point later SQL at a server that is no longer ours.
        """
        epoch = new_epoch()
        self.runtime["epoch"] = epoch
        self.runtime["opened_at"] = now_iso()
        self.runtime["linked_server"] = linked_server
        return epoch

    def stale_children(self) -> list[Node]:
        """Nodes marked done whose server-side output died with a past epoch."""
        return [child for child in self.children if child.is_stale(self.epoch)]

    @property
    def children(self) -> list[Node]:
        return [*self.phases, *self.runs]

    def recompute_status(self) -> str:
        statuses = [child.status for child in self.children]
        if not statuses:
            return self.status
        if FAILED in statuses:
            rolled = FAILED
        elif RUNNING in statuses:
            rolled = RUNNING
        elif BLOCKED in statuses:
            rolled = BLOCKED
        elif all(status == SKIPPED for status in statuses):
            rolled = SKIPPED
        elif all(status in SETTLED_STATUSES for status in statuses):
            rolled = DONE
        elif any(status in SETTLED_STATUSES for status in statuses):
            rolled = RUNNING
        else:
            rolled = PENDING
        self.status = rolled
        return rolled


class Manifest:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        if not isinstance(data, dict):
            raise ManifestError("Manifest root must be a mapping.")
        version = data.get("manifest_version")
        if version != MANIFEST_VERSION:
            raise ManifestError(
                f"Unsupported manifest_version {version!r}. This Pullmanager reads version {MANIFEST_VERSION}."
            )
        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, list):
            raise ManifestError("Manifest `sessions` must be a list.")
        self._data = data
        self.path = path
        self.sessions = [Session(session) for session in raw_sessions]
        self._validate()

    def _validate(self) -> None:
        seen_sessions: set[str] = set()
        seen_runs: set[str] = set()
        for session in self.sessions:
            if not session.data.get("session_id"):
                raise ManifestError("Every session requires a `session_id`.")
            if session.session_id in seen_sessions:
                raise ManifestError(f"Duplicate session_id {session.session_id!r}.")
            seen_sessions.add(session.session_id)
            validate_status(session.status)
            for phase in session.phases:
                if not phase.yaml:
                    raise ManifestError(f"Phase {phase.label!r} is missing its `yaml` path.")
                validate_status(phase.status)
            for run in session.runs:
                if not run.data.get("run_id"):
                    raise ManifestError(f"A run in session {session.session_id!r} is missing `run_id`.")
                if run.run_id in seen_runs:
                    raise ManifestError(f"Duplicate run_id {run.run_id!r}.")
                seen_runs.add(run.run_id)
                if not run.yaml:
                    raise ManifestError(f"Run {run.run_id!r} is missing its `yaml` path.")
                validate_status(run.status)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def project(self) -> dict[str, Any]:
        return self._data.get("project") or {}

    @property
    def source(self) -> dict[str, Any]:
        return self._data.get("source") or {}

    @property
    def root(self) -> Path:
        """Directory the manifest's relative `yaml` paths resolve against."""
        if self.path is None:
            raise ManifestError("Manifest has no path; cannot resolve relative YAML paths.")
        return self.path.parent

    def resolve(self, node: Node) -> Path:
        if not node.yaml:
            raise ManifestError(f"Node {node.label!r} has no `yaml` path to resolve.")
        return self.root / node.yaml

    @property
    def cosmos_refresh(self) -> dict[str, str]:
        """Each Cosmos database's `create_date` when this manifest last ran (D51)."""
        return self._data.setdefault("cosmos_refresh", {})

    @property
    def cosmos_refresh_instances(self) -> dict[str, dict[str, str]]:
        """Each instance's (by server name) own `create_date`s, as last seen (D183)."""
        return self._data.setdefault("cosmos_refresh_instances", {})

    @property
    def last_execute(self) -> dict[str, Any]:
        """When the latest Execute started and ended, its exit code and how
        it ended (D140). Empty before the first; `ended_at` stays empty when
        the process was killed, which is what says it did not end by itself."""
        return self._data.get("last_execute") or {}

    def execute_started(self) -> None:
        self._data["last_execute"] = {"started_at": now_iso(), "ended_at": None,
                                      "exit_code": None, "how": None}
        self.save()

    def execute_ended(self, exit_code: int | None, how: str) -> None:
        record = self._data.setdefault("last_execute", {"started_at": None})
        record.update(ended_at=now_iso(), exit_code=exit_code, how=how)
        self.save()

    @property
    def uploads_landed(self) -> dict[str, Any]:
        """Uploads already landed in Projects in this pull, by destination (D61)."""
        return self._data.setdefault("uploads_landed", {})

    def reset_all(self, reason: str) -> None:
        """Every session starts over, finished work included.

        The uploads land again too: a re-pull reads the files afresh (D61).
        """
        self._data.pop("uploads_landed", None)
        for session in self.sessions:
            session.runtime.clear()
            for child in session.children:
                child.reset(reason)

    def iter_nodes(self) -> Iterator[tuple[Session, Node]]:
        for session in self.sessions:
            for child in session.children:
                yield session, child

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        path = Path(path)
        if not path.is_file():
            raise ManifestError(f"Manifest not found: {path}")
        return cls(load_yaml(path), path=path)

    @classmethod
    def from_text(cls, text: str, path: str | Path) -> "Manifest":
        """A manifest already read, so a reader opens the file once (D154)."""
        return cls(parse_yaml(text), path=Path(path))

    def save(self, path: str | Path | None = None) -> Path:
        """Write the manifest atomically so a crash cannot truncate it."""
        target = Path(path) if path else self.path
        if target is None:
            raise ManifestError("No path to save manifest to.")
        for session in self.sessions:
            session.recompute_status()
        tmp = target.with_name(target.name + ".tmp")
        dump_yaml(self._data, tmp)
        replace_patiently(tmp, target)  # a reader may hold it for a moment (D153)
        self.path = target
        return target
