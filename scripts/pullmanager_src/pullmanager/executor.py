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


def session_resumes(session: Session) -> bool:
    """Whether a session picks up where it left off rather than starting over.

    It resumes once its PK phase is done: the Projects copy of the PK exists,
    so the batches still to run can be drawn from the same population as the
    ones that finished (D52). Anything earlier starts over. A refresh of Cosmos
    or `--repull` resets every node, which makes every session start over.
    """
    pk = next((phase for phase in session.phases if phase.name == "pk"), None)
    return pk is not None and pk.status == DONE


def should_execute(
    node: Node,
    kind: str,
    *,
    resuming: bool = False,
    retry_failed: bool = False,
) -> tuple[bool, str]:
    """Decide whether one unit runs, and say why.

    Starting over, everything runs: setup drops the destinations, so every run
    has to refill them. Resuming, a finished run's rows are in Projects and
    stay; the server-side phases are rebuilt for the runs still to go, except
    the PK query, whose Projects copy the remaining batches are drawn from.
    """
    status = node.status
    if status == FAILED:
        if retry_failed:
            return True, "retrying a failure"
        return False, "failed; --retry-failed reopens it"
    if status == SKIPPED:
        return False, "skipped deliberately"
    if not resuming:
        if status == RUNNING:
            return True, "interrupted while running"
        if status == BLOCKED:
            return True, "was blocked; upstream may succeed this time"
        if status == DONE:
            return True, "starting over: setup empties the destinations"
        return True, "pending"
    if kind == "pk":
        return False, "kept: its Projects copy is what the remaining batches are drawn from"
    if kind in ("setup", "upload_cohorts"):
        return True, "rebuilt for the runs still to go; finished tables are kept"
    if status == DONE:
        return False, "done; its rows are in Projects"
    if status == RUNNING:
        return True, "interrupted while running; its rows are cleared first"
    if status == BLOCKED:
        return True, "was blocked; upstream may succeed this time"
    return True, "pending"


def session_units(
    manifest: Manifest, session: Session, *, retry_failed: bool = False
) -> list[tuple[str, Node, Path, bool, str]]:
    """Every unit with whether it runs next time, and why."""
    resuming = session_resumes(session)
    return [
        (kind, node, path, *should_execute(node, kind, resuming=resuming, retry_failed=retry_failed))
        for kind, node, path in iter_units(manifest, session)
    ]


def session_has_work(manifest: Manifest, session: Session, *, retry_failed: bool = False) -> bool:
    """False when an `--execute` has nothing to do for this session.

    A resuming session with no run left to execute is complete (or holds only
    failures not reopened), so rebuilding its server side would pull nothing.
    """
    units = session_units(manifest, session, retry_failed=retry_failed)
    if session_resumes(session):
        return any(execute for kind, _, _, execute, _ in units if kind == "run")
    return any(execute for _, _, _, execute, _ in units)


def run_destinations(manifest: Manifest, session: Session) -> set[str]:
    """Destinations the session's runs fill, which carry a `_batch` column."""
    dests: set[str] = set()
    for run in session.runs:
        path = manifest.resolve(run)
        if not path.is_file():
            continue
        for cohort in (load_yaml(path) or {}).get("cohorts") or []:
            if isinstance(cohort, dict) and cohort.get("dest_table"):
                dests.add(str(cohort["dest_table"]))
    return dests


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
    *,
    resuming: bool = False,
) -> Unit:
    """Render the SQL one phase or run implies."""
    if not path.is_file():
        raise PlanError(f"{node.label}: phase YAML not found at {path}")
    doc = load_yaml(path) or {}
    unit = Unit(session_id=session.session_id, node=node, kind=kind, yaml_path=path)

    if kind == "setup":
        unit.server_blocks = server_sql.render_setup(doc, unit.unit_id)
        cohorts = session_cohorts(manifest, session)
        unit.local_blocks = local_sql.render_setup(
            doc,
            cohorts,
            unit.unit_id,
            keep=resuming,
            batched=run_destinations(manifest, session),
        )
        unit.notes.append(
            f"setup keeps {len(cohorts)} destination table(s), creating any missing"
            if resuming else
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
) -> list[Unit]:
    """Every unit the next `--execute` would run for this session, in order."""
    if not include_settled and not session_has_work(manifest, session, retry_failed=retry_failed):
        return []
    resuming = session_resumes(session)
    units: list[Unit] = []
    for kind, node, path, execute, reason in session_units(
        manifest, session, retry_failed=retry_failed
    ):
        if not execute and not include_settled:
            continue
        unit = plan_unit(manifest, session, kind, node, path, linked_server, resuming=resuming)
        unit.reason = reason if execute else f"included anyway ({reason})"
        units.append(unit)
    return units


def plan(
    manifest: Manifest,
    *,
    linked_server: str = DRY_RUN_LINKED_SERVER,
    retry_failed: bool = False,
    include_settled: bool = False,
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
            )
        )
    return units


def excluded_units(
    manifest: Manifest, *, retry_failed: bool = False
) -> list[tuple[str, str, str]]:
    """Units the plan leaves out, as (id, status, reason).

    Reported rather than silently dropped: a failure left out should not look
    like success.
    """
    left_out: list[tuple[str, str, str]] = []
    for session in manifest.sessions:
        idle = not session_has_work(manifest, session, retry_failed=retry_failed)
        for kind, node, _, execute, reason in session_units(
            manifest, session, retry_failed=retry_failed
        ):
            if execute and not idle:
                continue
            label = f"{session.session_id}/{kind}" if isinstance(node, Phase) else node.label
            if execute and idle:
                reason = "nothing left to pull in this session"
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
