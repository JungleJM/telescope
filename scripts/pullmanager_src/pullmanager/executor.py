"""Traversal and planning.

Walks a manifest in order and produces the work a session implies. Nothing
here touches a database; Phase 6 supplies an adapter that executes what this
plans, so the ordering is testable on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import local_sql, server_sql
from .manifest import Manifest, Node, Phase, Run, Session
from .models import BLOCKED, DONE, FAILED, RUNNING, SKIPPED
from .naming import global_temp, projects_table, table_prefix, temp_prefix
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


def session_reads(manifest: Manifest, session: Session, dest: str, prefix: str) -> bool:
    """Whether any cohort the session builds names this upload's temp (D61)."""
    return reads_temp(manifest, session, dest, prefix)


def group_reads(manifest: Manifest, session: Session, dest: str, prefix: str, group: str | None) -> bool:
    """Whether a table group's runs name this upload's temp, to load it on its connection (D134)."""
    return reads_temp(manifest, session, dest, prefix, runs_of=group, grouped=True)


def reads_temp(
    manifest: Manifest, session: Session, dest: str, prefix: str,
    *, runs_of: str | None = None, grouped: bool = False,
) -> bool:
    temp = global_temp(dest, prefix).lower()
    for kind, node, path in iter_units(manifest, session):
        if kind not in ("pk", "run") or not path.is_file():
            continue
        if grouped and (kind != "run" or getattr(node, "group", None) != runs_of):
            continue
        for cohort in (load_yaml(path) or {}).get("cohorts") or []:
            if temp in json.dumps(cohort, default=str).lower():
                return True
    return False


def upload_note(
    manifest: Manifest, session: Session, upload: dict[str, Any], prefix: str, resuming: bool,
    tables: str = "",
) -> str:
    """What the upload phase will do with one upload: D54 as narrowed by D61."""
    dest = str(upload.get("dest_table") or upload.get("name"))
    copy = projects_table(f"upload_{dest}", tables)
    is_pk = str(upload.get("type", "")).lower() == "pk"
    if resuming:
        projects = f"{copy} is kept in Projects, not re-read from the file"
    elif not is_pk and dest in manifest.uploads_landed:
        projects = f"{copy} was landed earlier in this pull; this session uses it"
    elif is_pk:
        projects = f"lands in Projects as {copy}, typed"
    else:
        projects = (
            f"lands in Projects as {copy}, typed, once for the pull; later "
            "sessions use that copy"
        )
    if is_pk or session_reads(manifest, session, dest, prefix):
        cosmos = "then goes up to Cosmos from that copy"
    else:
        cosmos = "not sent to Cosmos: no cohort in this session reads it"
    return f"upload {dest}: {projects}; {cosmos} (D54, D61)"


class UnknownSession(ValueError):
    """A session named to re-pull that the pull does not have (D158)."""


def sessions_to_repull(manifest: Manifest, names: list[str]) -> tuple[list[Session], list[str]]:
    """The sessions to start over, and a line for each control a case brought.

    A case brings its controls: they were sampled against its counts (D59).
    """
    by_name = {session.session_id.lower(): session for session in manifest.sessions}
    unknown = [name for name in names if name.lower() not in by_name]
    if unknown:
        raise UnknownSession(
            f"No session named {', '.join(unknown)} in this pull. Its sessions: "
            f"{', '.join(s.session_id for s in manifest.sessions)}; or `all`."
        )
    chosen = [by_name[name.lower()] for name in dict.fromkeys(n.lower() for n in names)]
    notes: list[str] = []
    for case in list(chosen):
        for session in manifest.sessions:
            if session in chosen:
                continue
            if case.pk_table and case.pk_table in session_cases(manifest, session):
                chosen.append(session)
                notes.append(f"{session.session_id} too: it is a control sampled against "
                             f"{case.session_id}")
    return chosen, notes


def session_cases(manifest: Manifest, session: Session) -> list[str]:
    """The PK tables this session's PK is sampled against as a control."""
    pk = next((phase for phase in session.phases if phase.name == "pk" and phase.yaml), None)
    if pk is None:
        return []
    path = manifest.resolve(pk)
    if not path.is_file():
        return []
    cases = []
    for cohort in (load_yaml(path) or {}).get("cohorts") or []:
        if isinstance(cohort, dict) and cohort.get("dest_table") == session.pk_table:
            cases += [str(item.get("matched_to")) for item in control_samples(cohort)]
    return cases


def reset_sessions(manifest: Manifest, sessions: list[Session], reason: str) -> None:
    """Each starts over as a new pull would; uploads already landed stay (D61)."""
    for session in sessions:
        session.runtime.clear()
        for child in session.children:
            child.reset(reason)


def control_samples(cohort: Any) -> list[dict[str, Any]]:
    """The PK's `split_after_build` levels that sample it as a control (D59)."""
    if not isinstance(cohort, dict):
        return []
    return [
        item for item in cohort.get("split_after_build") or []
        if isinstance(item, dict) and item.get("role") == "control" and item.get("row_mult")
    ]


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
        if not enabled:
            unit.notes.append("no upload cohorts")
        for upload in enabled:
            unit.notes.append(upload_note(manifest, session, upload, temp_prefix(doc), resuming,
                                          table_prefix(doc)))
        return unit

    server_blocks, notes = server_sql.render_phase(doc, unit.unit_id)
    if kind == "pk":
        for cohort in doc.get("cohorts") or []:
            for item in control_samples(cohort):
                notes.append(
                    f"{cohort.get('dest_table')} is then sampled to {item['row_mult']}x "
                    f"{item.get('matched_to')} per batch, by a hash of its key (D59)"
                )
    unit.server_blocks = server_blocks
    unit.notes.extend(notes)
    open_columns = [
        str(d.get("column")) for d in ((getattr(node, "batch", None) or {}).get("runtime") or [])
        if str(d.get("kind", "")).lower() == "column_values"
    ]
    if kind == "run" and open_columns:
        unit.notes.append(
            f"batches by every value of {', '.join(open_columns)} in the PK, found when it "
            "runs (NULL included); each is pulled in turn into this run (D82)"
        )
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
    connection_group: Any = NO_RUN_YET
    for kind, node, path, execute, reason in session_units(
        manifest, session, retry_failed=retry_failed
    ):
        if not execute and not include_settled:
            continue
        unit = plan_unit(manifest, session, kind, node, path, linked_server, resuming=resuming)
        unit.reason = reason if execute else f"included anyway ({reason})"
        if kind == "run":
            group = getattr(node, "group", None)
            if connection_group is not NO_RUN_YET and group != connection_group:
                unit.notes.insert(0, group_connection_note(group))
            connection_group = group
        units.append(unit)
    return units


# Before the session's first run, whichever group it is in shares the
# connection that built the PK.
NO_RUN_YET = object()


def group_connection_note(group: str | None) -> str:
    name = f"table group {group}" if group else "the tables in no group"
    return (
        f"a new Cosmos connection opens for {name}: the PK temp, and the uploads these "
        "tables read, are loaded again from their Projects copies (D134)"
    )


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
