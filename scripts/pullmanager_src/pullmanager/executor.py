"""Traversal and planning.

Walks a manifest in order and produces the work a session implies. Nothing
here touches a database; Phase 6 supplies an adapter that executes what this
plans, so the ordering is testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import local_sql, server_sql
from .manifest import Manifest, Node, Phase, Run, Session
from .models import BLOCKED, DONE, FAILED, RUNNING, SKIPPED
from .normalize import normalize_bool
from .sql import SqlBlock
from .yaml_io import load_yaml

DRY_RUN_LINKED_SERVER = "DRY-RUN-INSTANCE"


class PlanError(ValueError):
    """Raised when a manifest cannot be turned into work."""


@dataclass
class Unit:
    """One phase or run, with the SQL it implies."""

    session_id: str
    node: Node
    kind: str  # "setup" | "upload_cohorts" | "pk" | "run"
    yaml_path: Path
    server_blocks: list[SqlBlock] = field(default_factory=list)
    local_blocks: list[SqlBlock] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = "pending"

    @property
    def unit_id(self) -> str:
        return f"{self.session_id}/{self.kind}" if isinstance(self.node, Phase) else self.node.label

    @property
    def blocks(self) -> list[SqlBlock]:
        return [*self.server_blocks, *self.local_blocks]


def iter_units(manifest: Manifest, session: Session) -> Iterator[tuple[str, Node, Path]]:
    """Phases in routine order, then runs in manifest order."""
    for phase in session.phases:
        yield phase.name, phase, manifest.resolve(phase)
    for run in session.runs:
        yield "run", run, manifest.resolve(run)


RESUME_FULL = "full"
RESUME_PARTIAL = "partial"


def should_execute(
    node: Node,
    kind: str,
    *,
    current_epoch: str | None = None,
    mode: str = RESUME_FULL,
    retry_failed: bool = False,
) -> tuple[bool, str]:
    """Decide whether one unit runs, and say why.

    `done` does not mean "its output still exists". Global temps die with the
    connection, so work completed under a previous epoch has left nothing on
    the server even though the status still reads done. A run is different: its
    durable result is rows in a Projects table, which survive, so it is the one
    kind of unit a partial resume can skip.
    """
    status = node.status
    if status == FAILED:
        if retry_failed:
            return True, "retrying a failure"
        return False, "failed; --retry-failed reopens it"
    if status == RUNNING:
        return True, "interrupted while running"
    if status == BLOCKED:
        return True, "was blocked; upstream may succeed this time"
    if status == SKIPPED:
        return False, "skipped deliberately"
    if status == DONE:
        if not node.is_stale(current_epoch):
            return False, "already done in this session"
        if kind == "run" and mode == RESUME_PARTIAL:
            return False, "done; its rows are in a Projects table and survive"
        return True, "done under a previous connection; server state is gone"
    return True, "pending"


def session_cohorts(manifest: Manifest, session: Session) -> list[dict[str, Any]]:
    """Every cohort the session will write, gathered for the setup shells."""
    seen: dict[str, dict[str, Any]] = {}
    for _, node, path in iter_units(manifest, session):
        if not path.is_file():
            continue
        doc = load_yaml(path) or {}
        for cohort in doc.get("cohorts") or []:
            if not isinstance(cohort, dict) or not cohort.get("dest_table"):
                continue
            if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
                continue
            seen.setdefault(str(cohort["dest_table"]), cohort)
    return list(seen.values())


def plan_unit(
    manifest: Manifest,
    session: Session,
    kind: str,
    node: Node,
    path: Path,
    linked_server: str,
) -> Unit:
    """Render the SQL one phase or run implies."""
    if not path.is_file():
        raise PlanError(f"{node.label}: phase YAML not found at {path}")
    doc = load_yaml(path) or {}
    unit = Unit(session_id=session.session_id, node=node, kind=kind, yaml_path=path)

    if kind == "setup":
        unit.server_blocks = server_sql.render_setup(doc, unit.unit_id)
        cohorts = session_cohorts(manifest, session)
        unit.local_blocks = local_sql.render_setup(doc, cohorts, unit.unit_id)
        unit.notes.append(
            f"setup creates {len(cohorts)} destination table(s); runs append to them"
        )
        return unit

    if kind == "upload_cohorts":
        uploads = doc.get("upload_cohorts") or []
        enabled = [
            u for u in uploads
            if isinstance(u, dict) and normalize_bool(u.get("push_this_cycle"), default=True)
        ]
        unit.notes.append(
            f"{len(enabled)} upload cohort(s); uploaded through the client, since there is "
            "no linked server from Cosmos back to Projects"
            if enabled else "no upload cohorts"
        )
        return unit

    server_blocks, notes = server_sql.render_phase(doc, unit.unit_id)
    unit.server_blocks = server_blocks
    unit.notes.extend(notes)
    unit.local_blocks = local_sql.render_phase(doc, unit.unit_id, linked_server)
    return unit


def plan_session(
    manifest: Manifest,
    session: Session,
    *,
    linked_server: str = DRY_RUN_LINKED_SERVER,
    retry_failed: bool = False,
    include_settled: bool = False,
    mode: str = RESUME_FULL,
    current_epoch: str | None = None,
) -> list[Unit]:
    """Every unit this session would execute, in order.

    `current_epoch` defaults to None, which models opening a fresh connection:
    anything completed under a previous epoch is stale.
    """
    units: list[Unit] = []
    for kind, node, path in iter_units(manifest, session):
        execute, reason = should_execute(
            node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
        )
        if not execute and not include_settled:
            continue
        unit = plan_unit(manifest, session, kind, node, path, linked_server)
        unit.reason = reason if execute else f"included anyway ({reason})"
        units.append(unit)
    return units


def plan(
    manifest: Manifest,
    *,
    linked_server: str = DRY_RUN_LINKED_SERVER,
    retry_failed: bool = False,
    include_settled: bool = False,
    mode: str = RESUME_FULL,
) -> list[Unit]:
    units: list[Unit] = []
    for session in manifest.sessions:
        units.extend(
            plan_session(
                manifest,
                session,
                linked_server=linked_server,
                retry_failed=retry_failed,
                include_settled=include_settled,
                mode=mode,
            )
        )
    return units


def excluded_units(
    manifest: Manifest,
    *,
    mode: str = RESUME_FULL,
    retry_failed: bool = False,
    current_epoch: str | None = None,
) -> list[tuple[str, str, str]]:
    """Units the plan leaves out, as (id, status, reason).

    Reported rather than silently dropped: a resume that rebuilds the server
    side but omits the run that failed would finish with nothing transferred,
    which should not look like success.
    """
    left_out: list[tuple[str, str, str]] = []
    for session in manifest.sessions:
        for kind, node, _ in iter_units(manifest, session):
            execute, reason = should_execute(
                node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
            )
            if not execute:
                label = f"{session.session_id}/{kind}" if isinstance(node, Phase) else node.label
                left_out.append((label, node.status, reason))
    return left_out


def write_sql(units: list[Unit], out_dir: Path) -> list[Path]:
    """Write every block to an inspectable file named after its manifest id."""
    out_dir = Path(out_dir)
    written: list[Path] = []
    for unit in units:
        for block in unit.blocks:
            safe = block.block_id.replace("/", "__")
            path = out_dir / unit.session_id / f"{safe}.{block.side}.sql"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(block.sql.rstrip("\n") + "\n", encoding="utf-8")
            written.append(path)
    return written
