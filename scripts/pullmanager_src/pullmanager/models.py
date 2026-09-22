"""Status vocabulary and timing helpers shared by manifest nodes."""

from __future__ import annotations

import uuid
from datetime import datetime

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
BLOCKED = "blocked"

ALL_STATUSES = (PENDING, RUNNING, DONE, FAILED, SKIPPED, BLOCKED)

# Statuses that mean "do not execute again on a plain resume".
SETTLED_STATUSES = (DONE, SKIPPED)


class StatusError(ValueError):
    """Raised when an unknown status string is supplied."""


def validate_status(status: str) -> str:
    if status not in ALL_STATUSES:
        raise StatusError(
            f"Unknown status {status!r}. Expected one of: {', '.join(ALL_STATUSES)}"
        )
    return status


def new_epoch() -> str:
    """Identify one live server connection.

    Global temp tables die with the connection, so work recorded under a
    previous epoch is known to be gone from the server even though the manifest
    still says `done`.
    """
    return f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    """Local timestamp with UTC offset, e.g. 2026-09-22T14:03:00-05:00."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_duration(seconds: float) -> str:
    """Render a duration as "1h 2m 3s" / "5m 32s" / "45s"."""
    total = int(round(seconds))
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{sign}{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{sign}{minutes}m {secs}s"
    return f"{sign}{secs}s"


def duration_block(started_at: str | None, finished_at: str | None) -> dict | None:
    """Build the manifest `duration` mapping from two ISO timestamps."""
    if not started_at or not finished_at:
        return None
    seconds = (parse_iso(finished_at) - parse_iso(started_at)).total_seconds()
    return {"seconds": int(round(seconds)), "display": format_duration(seconds)}
