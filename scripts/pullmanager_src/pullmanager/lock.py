"""The lock a running Execute holds on its manifest (D67).

`running` in the manifest cannot say whether a pull is running now: a pull
that crashed or was stopped leaves it too. So `--execute` writes
`pullmanifest.lock` beside the manifest, and a background thread rewrites its
heartbeat every 30 seconds, so a long query does not stop it. A lock whose
heartbeat is more than 2 minutes old is stale: its process stopped without
cleaning up. The process id is recorded to show, never checked: on Windows the
standard library cannot safely ask whether a process is alive
(`os.kill(pid, 0)` ends it).

makeYaml reads the same file before `--export-split` replaces a manifest; it
keeps its own copy of the reading rule, since it runs without this package.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HEARTBEAT_SECONDS = 30
STALE_SECONDS = 120


class LockHeld(RuntimeError):
    """Raised when another Execute holds the manifest's lock."""


def lock_path(manifest: str | Path) -> Path:
    """`pullmanifest.lock` beside `pullmanifest.yaml`."""
    return Path(manifest).with_suffix(".lock")


def clock_time(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).strftime("%H:%M")


def age_words(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


@dataclass
class LockInfo:
    """What a lock file says. Times are seconds since the epoch."""

    path: Path
    pid: int | None
    machine: str
    started: float
    heartbeat: float
    token: str = ""

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.heartbeat

    def live(self, now: float | None = None) -> bool:
        return self.age(now) < STALE_SECONDS

    def summary(self, now: float | None = None) -> str:
        """`Executing since 14:03, last heartbeat 20s ago`."""
        return (
            f"Executing since {clock_time(self.started)}, "
            f"last heartbeat {age_words(self.age(now))} ago"
        )

    def holder(self) -> str:
        who = f"process {self.pid}" if self.pid else "a process"
        return f"{who} on {self.machine}" if self.machine else who

    def free_at(self) -> str:
        """When it would count as stopped, if its heartbeat stopped now."""
        return clock_time(self.heartbeat + STALE_SECONDS)


def read_lock(manifest: str | Path) -> LockInfo | None:
    """The manifest's lock, or None. Never raises.

    A lock that cannot be parsed is dated by the file's own time, so a write
    caught halfway still counts as live for as long as its file is fresh.
    """
    path = lock_path(manifest)
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def number(key: str) -> float:
        try:
            return float(data[key])
        except (KeyError, TypeError, ValueError):
            return stat.st_mtime

    try:
        pid = int(data["pid"])
    except (KeyError, TypeError, ValueError):
        pid = None
    return LockInfo(
        path=path,
        pid=pid,
        machine=str(data.get("machine") or ""),
        started=number("started"),
        heartbeat=number("heartbeat"),
        token=str(data.get("token") or ""),
    )


def live_lock(manifest: str | Path, now: float | None = None) -> LockInfo | None:
    held = read_lock(manifest)
    return held if held and held.live(now) else None


def pull_name(manifest: str | Path) -> str:
    """`IBD_Ancestry` for `runs/IBD_Ancestry/split/pullmanifest.yaml`."""
    manifest = Path(manifest)
    return manifest.parent.parent.name if manifest.parent.name == "split" else str(manifest)


def held_message(held: LockInfo, manifest: str | Path, now: float | None = None) -> str:
    return (
        f"{pull_name(manifest)} is already executing: {held.holder()}, since "
        f"{clock_time(held.started)}, last heartbeat {age_words(held.age(now))} ago. "
        f"If it has stopped, it counts as stopped 2 minutes after its last heartbeat, "
        f"at {held.free_at()}; run it again then. Its lock is {held.path}."
    )


class PullLock:
    """Held by `--execute` for as long as it runs: `with PullLock(path): ...`."""

    def __init__(self, manifest: str | Path, *, interval: float = HEARTBEAT_SECONDS,
                 clock=time.time) -> None:
        self.manifest = Path(manifest)
        self.path = lock_path(manifest)
        self.interval = interval
        self.clock = clock
        self.token = uuid.uuid4().hex
        self.started = 0.0
        self.replaced: LockInfo | None = None  # a stale lock taken over
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "PullLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def _payload(self) -> str:
        return json.dumps({
            "pid": os.getpid(),
            "machine": socket.gethostname(),
            "started": self.started,
            "heartbeat": self.clock(),
            "token": self.token,
            "manifest": str(self.manifest),
        }, indent=2) + "\n"

    def acquire(self) -> None:
        """Take the lock, or raise LockHeld naming who has it."""
        self.started = self.clock()
        for _ in range(2):
            try:
                # Exclusive create: two Executes starting together cannot both win.
                with open(self.path, "x", encoding="utf-8") as handle:
                    handle.write(self._payload())
                break
            except FileExistsError:
                held = read_lock(self.manifest)
                if held and held.live(self.clock()):
                    raise LockHeld(held_message(held, self.manifest, self.clock()))
                self.replaced = held
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        else:
            raise LockHeld(f"Could not take the lock {self.path}; another Execute took it first.")
        self._stop.clear()
        self._thread = threading.Thread(target=self._beat, name="pull-heartbeat", daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            self.heartbeat()

    def heartbeat(self) -> None:
        """Rewrite the lock with the time now; a failed write waits for the next."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(self._payload(), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def release(self) -> None:
        """Stop the heartbeat and remove the lock, if it is still this one."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        held = read_lock(self.manifest)
        if held and held.token == self.token:
            try:
                self.path.unlink()
            except OSError:
                pass
