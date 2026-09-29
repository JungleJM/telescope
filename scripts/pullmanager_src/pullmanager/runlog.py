"""Everything Execute prints, written to a log as well (D68).

However Execute is started (its own console window, a terminal, the Mac),
what it prints also goes to `runs/<project>/logs/execute-<date>-<time>.log`,
flushed line by line, so the launcher's Pull Log tab can follow it and it is
kept after every window is closed. It is plain text, the same lines as the
terminal (D70).
"""

from __future__ import annotations

import contextlib
import faulthandler
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

from .pulls import logs_folder, older_logs_folder


class Tee:
    """A stream that writes to the console and the log alike.

    The console may be missing (no window to write to) or closed; the log is
    still written.
    """

    def __init__(self, stream: TextIO | None, log: TextIO) -> None:
        self.stream = stream
        self.log = log

    def write(self, text: str) -> int:
        if self.stream is not None:
            try:
                self.stream.write(text)
            except (OSError, ValueError):
                self.stream = None
        self.log.write(text)
        return len(text)

    def flush(self) -> None:
        if self.stream is not None:
            try:
                self.stream.flush()
            except (OSError, ValueError):
                self.stream = None
        self.log.flush()

    def isatty(self) -> bool:
        return bool(self.stream is not None and self.stream.isatty())

    @property
    def encoding(self) -> str:
        return getattr(self.stream, "encoding", None) or "utf-8"


def new_log_path(manifest: str | Path, now: datetime | None = None) -> Path:
    """A new log at the run folder's top, the earlier ones moved into
    `older_logs/` first, so the latest is the one beside the manifest (D142)."""
    folder = logs_folder(manifest)
    folder.mkdir(parents=True, exist_ok=True)
    older = folder.glob("execute-*.log")
    for log in sorted(older):
        target = older_logs_folder(manifest) / log.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            log.replace(target)
        except OSError:
            pass  # one still open elsewhere stays; the new log is still written
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = folder / f"execute-{stamp}.log"
    number = 2
    # Unique in both folders, so moving it aside later replaces nothing.
    while path.exists() or (older_logs_folder(manifest) / path.name).exists():
        path = folder / f"execute-{stamp}-{number}.log"
        number += 1
    return path


@contextlib.contextmanager
def execute_log(manifest: str | Path) -> Iterator[Path]:
    """Copy stdout and stderr into a new log for as long as the block runs."""
    path = new_log_path(manifest)
    with open(path, "x", encoding="utf-8", newline="\n", buffering=1) as log:
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Tee(saved[0], log), Tee(saved[1], log)
        # A crash in native code (the ODBC driver, pyarrow) kills the process
        # with no Python error to print; this writes where it was to the log.
        faulthandler.enable(file=log)
        try:
            yield path
        finally:
            faulthandler.disable()
            sys.stdout, sys.stderr = saved
