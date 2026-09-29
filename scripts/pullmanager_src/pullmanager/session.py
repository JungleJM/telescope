"""Executing one session.

The Cosmos connection is held open for the whole session, because every
global temp (`##<prefix>_*`) dies with it. That single fact shapes everything here: the
epoch, what a resume must replay, and why uploads travel through the client.
A table group is the exception (D134): its runs get a connection of their own,
into which the PK temp and the uploads they read are loaded again from Projects.

Each step says when it starts and when it ends, as it happens (D136), so a
table that takes hours shows as running rather than as silence.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import local_sql, refresh, server_sql, uploads
from .batches import (
    BatchError,
    chunk_clause,
    count_batch_rows,
    distinct_values,
    open_dimensions,
    sample_down,
    select_batch_rows,
    value_batch,
)
from .db import DatabaseError, Settings, bulk_insert, capture_server_name, connect, execute_script
from .executor import (
    NO_RUN_YET,
    control_samples,
    group_reads,
    Unit,
    iter_units,
    plan_unit,
    run_destinations,
    session_cohorts,
    session_has_work,
    session_reads,
    session_resumes,
    should_execute,
)
from .manifest import Manifest, Phase, Session
from .models import format_duration, now_iso
from .naming import destination, global_temp, temp_prefix
from .normalize import cosmos_database
from .uploads import UploadError
from .yaml_io import load_yaml

# A clash adds a number to the prefix (D50); past this many, something is wrong.
MAX_PREFIX_NUMBER = 99

# The old generator warned past this; a pull this size is usually a mistake in
# the filter rather than an intention.
LARGE_ROW_WARNING = 80_000_000


class SessionError(RuntimeError):
    """Raised when a session cannot proceed."""


def say_now(line: str) -> None:
    """Print a progress line at once, so the log shows it while the step runs."""
    print(line, flush=True)


def step_name(kind: str, node: Any) -> str:
    """How the log names a unit: `setup`, `uploads`, `pk`, `run b2of4-LA-Male`."""
    if kind == "upload_cohorts":
        return "uploads"
    if kind != "run":
        return kind
    batch = str((getattr(node, "batch", None) or {}).get("name") or "")
    return " ".join(filter(None, ("run", getattr(node, "group", None), batch)))


def since(started: float) -> str:
    return format_duration(time.monotonic() - started)


@dataclass
class SessionReport:
    session_id: str
    epoch: str = ""
    linked_server: str = ""
    completed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # (destination, column) -> [declared type, widest value], across every
    # batch and chunk of the session: notes, not warnings (D34, D70).
    widths: dict[tuple[str, str], list] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed

    def record_width(self, dest: str, column: str, declared: str, widest: int) -> None:
        entry = self.widths.setdefault((dest, column), [declared, int(widest)])
        entry[0] = entry[0] or declared
        entry[1] = max(entry[1], int(widest))


class SessionRunner:
    """Runs one session's phases and runs against live connections."""

    def __init__(
        self,
        manifest: Manifest,
        session: Session,
        settings: Settings,
        *,
        connect_fn: Callable[..., Any] = connect,
        retry_failed: bool = False,
        upload_root: Path | None = None,
        say: Callable[[str], None] = say_now,
    ):
        self.manifest = manifest
        self._say_line = say
        # Where a run is, for its lines: `c3of12`, `v2of5 (LA)`, or nothing.
        self._where = ""
        # The table group whose runs this Cosmos connection serves (D134), and
        # whether the PK temp must be built again in it before a run reads it.
        self._connection_group: Any = NO_RUN_YET
        self._pk_temp_missing = False
        self.session = session
        self.settings = settings
        self._connect = connect_fn
        self.retry_failed = retry_failed
        self.upload_root = upload_root or manifest.root
        # Decided before anything runs: executing the PK phase would change it.
        self.resuming = session_resumes(session)
        self.cosmos: Any = None
        self.projects: Any = None
        self.report = SessionReport(session_id=session.session_id)
        # What the split named the temps with, and what this session uses: the
        # same unless another pull holds those names (D50).
        self.planned_prefix = ""
        self.prefix = ""

    # ----------------------------------------------------------- lifecycle

    def open(self) -> None:
        try:
            self._open()
        except Exception:
            self.close()
            raise

    def _open(self) -> None:
        """Open the connection whose lifetime defines the session."""
        doc = self._phase_doc("setup")
        linked_server = self._open_cosmos()
        self._choose_prefix()
        self._begin_epoch(linked_server)

        project_db = doc.get("project_db")
        if not project_db:
            raise SessionError(f"{self.session.session_id}: setup.yaml has no project_db.")
        self.project_db = str(project_db)
        self.projects = self._connect(
            self.settings.projects_connection_string(self.project_db),
            login_timeout=self.settings.login_timeout,
            query_timeout=self.settings.query_timeout,
        )
        self.say(f"connected: Cosmos on {linked_server}, Projects {self.project_db}")
        self.manifest.save()

    def say(self, text: str, depth: int = 0) -> None:
        """One progress line: the time, then the step, indented under its unit."""
        self._say_line(f"  {time.strftime('%H:%M:%S')}  {'  ' * depth}{text}")

    def _open_cosmos(self) -> str:
        """Connect to Cosmos and return the instance it landed on."""
        doc = self._phase_doc("setup")
        self.cosmos = self._connect(
            self.settings.cosmos_connection_string(cosmos_database(doc.get("cosmos_db"))),
            login_timeout=self.settings.login_timeout,
            query_timeout=self.settings.query_timeout,
        )
        # Captured per connection: the instance name changes every time, so a
        # cached one would aim OPENQUERY at a server that is no longer ours.
        linked_server = capture_server_name(self.cosmos)
        self._check_refresh()
        return linked_server

    def _begin_epoch(self, linked_server: str) -> None:
        self.report.epoch = self.session.begin_epoch(linked_server=linked_server)
        self.report.linked_server = linked_server

    def _connect_group(self, group: str | None) -> None:
        """A Cosmos connection of the group's own, ready for its runs (D134).

        The last group's temps die with its connection, the PK's included, so
        the PK temp is built again from its Projects copy before the first run
        reads it, and each upload the group's tables read is loaded again
        from its copy. The temp prefix stays the session's.
        """
        name = f"table group {group}" if group else "tables in no group"
        old, self.cosmos = self.cosmos, None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        linked_server = self._open_cosmos()
        self._begin_epoch(linked_server)
        self._pk_temp_missing = True
        self.say(f"{name}: new Cosmos connection on {linked_server}")
        doc = self._phase_doc("upload_cohorts")
        for cohort in uploads.enabled_uploads(doc):
            dest = uploads.upload_dest(cohort)
            if str(cohort.get("type", "")).lower() == "pk":
                continue  # the PK temp, built again as a run starts
            if not group_reads(self.manifest, self.session, dest, self.planned_prefix or self.prefix, group):
                continue
            self.say(f"{dest}: into Cosmos started", 1)
            started = time.monotonic()
            loaded = self._load_temp_from_copy(dest, destination(self.project_db, uploads.copy_table(dest)))
            self.say(f"{dest}: {loaded:,} rows into Cosmos in {since(started)}", 1)
        self.manifest.save()

    def _check_refresh(self) -> None:
        """Refuse to add to a pull whose Cosmos has been rebuilt under it (D51).

        `--execute` compares before any session opens and starts everything
        over if Cosmos moved. This catches a refresh during the run itself.
        """
        stamps = refresh.read_stamps(self.cosmos)
        used = refresh.databases_used(self.manifest)
        moved = refresh.changes(self.manifest, stamps, used)
        if moved:
            described = "; ".join(f"{name}: {before} -> {after}" for name, before, after in moved)
            raise SessionError(
                f"Cosmos was refreshed while this pull was running ({described}). Nothing "
                "more was pulled. Run --execute again: every session will start over."
            )
        seen = {name: value for name, value in stamps.items() if name.lower() in used}
        for name, value in seen.items():
            self.manifest.cosmos_refresh.setdefault(name, value)
        self.session.runtime["cosmos_created"] = seen

    def _session_temps(self, prefix: str) -> list[str]:
        dests = {str(c["dest_table"]) for c in session_cohorts(self.manifest, self.session)}
        for upload in uploads.enabled_uploads(self._phase_doc("upload_cohorts")):
            dests.add(str(upload.get("dest_table") or upload.get("name")))
        return sorted(global_temp(dest, prefix) for dest in dests if dest)

    def _choose_prefix(self) -> None:
        """Use the planned prefix unless another pull holds one of its temps.

        A global temp lives only while the connection that made it is open, so
        one that already exists belongs to a pull running now. Rather than
        drop it from under that pull, number this session's prefix until none
        of its names are taken (D50).
        """
        self.planned_prefix = self.prefix = temp_prefix(self._phase_doc("setup"))
        for number in range(1, MAX_PREFIX_NUMBER + 1):
            candidate = self.planned_prefix if number == 1 else f"{self.planned_prefix}{number}"
            names = self._session_temps(candidate)
            if not names:
                break
            probe = ", ".join(f"OBJECT_ID(N'tempdb..{name}')" for name in names)
            try:
                cursor = self.cosmos.cursor()
                cursor.execute(f"SELECT {probe};")
                row = cursor.fetchone()
            except Exception as exc:
                self.report.warnings.append(
                    f"Could not check whether another pull holds these temps ({exc}); "
                    f"using ##{candidate}_ as planned."
                )
                break
            if not row or all(value is None for value in row):
                self.prefix = candidate
                break
        else:
            raise SessionError(
                f"Temps named ##{self.planned_prefix}_ through ##{self.planned_prefix}"
                f"{MAX_PREFIX_NUMBER}_ are all in use. Set a different temp_prefix."
            )
        if self.prefix != self.planned_prefix:
            self.report.warnings.append(
                f"Another pull holds temps named ##{self.planned_prefix}_; this session "
                f"uses ##{self.prefix}_ instead."
            )
        self.session.runtime["temp_prefix"] = self.prefix

    def _rename(self, sql: str) -> str:
        """Point planned temp names at the ones this session actually uses."""
        if not self.prefix or self.prefix == self.planned_prefix:
            return sql
        return re.sub(
            f"##{re.escape(self.planned_prefix)}_", f"##{self.prefix}_", sql, flags=re.I
        )

    def _execute(self, connection: Any, sql: str, *, label: str) -> Any:
        return execute_script(connection, self._rename(sql), label=label)

    def close(self) -> None:
        for connection in (self.projects, self.cosmos):
            if connection is None:
                continue
            try:
                connection.close()
            except Exception:
                pass
        self.projects = self.cosmos = None

    def __enter__(self) -> "SessionRunner":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --------------------------------------------------------------- units

    def _phase_doc(self, kind: str) -> dict[str, Any]:
        for name, node, path in iter_units(self.manifest, self.session):
            if name == kind:
                return load_yaml(path) or {}
        raise SessionError(f"{self.session.session_id}: no {kind} phase in the manifest.")

    def execute(self) -> SessionReport:
        """Run every unit that needs running, in order.

        A failed run does not stop its siblings: batches are disjoint appends
        and independent once the PK exists, so one night produces one list of
        every failure. A failed phase does block what follows it, since setup,
        uploads and PK are prerequisites.
        """
        if not session_has_work(self.manifest, self.session, retry_failed=self.retry_failed):
            self.report.skipped.append(f"{self.session.session_id} (nothing left to pull)")
            return self.report
        blocked = False
        for kind, node, path in iter_units(self.manifest, self.session):
            label = node.label
            name = step_name(kind, node)
            if blocked:
                node.block("an earlier phase in this session failed")
                self.report.skipped.append(label)
                self.say(f"{name} blocked: an earlier phase failed")
                self.manifest.save()
                continue

            execute, reason = should_execute(
                node, kind, resuming=self.resuming, retry_failed=self.retry_failed
            )
            if not execute:
                self.report.skipped.append(f"{label} ({reason})")
                self.say(f"{name} skipped: {reason}")
                continue

            node.start()
            self.manifest.save()
            self.say(f"{name} started")
            started = time.monotonic()
            self._where = ""
            try:
                if kind == "run":
                    group = getattr(node, "group", None)
                    if self._connection_group is not NO_RUN_YET and group != self._connection_group:
                        self._connect_group(group)
                    self._connection_group = group
                rows = self._run_unit(kind, node, path)
            except Exception as exc:
                # Nothing a failed unit wrote should be committed along with the
                # next unit's work. The retry clears its batch anyway (D52).
                self._rollback()
                node.fail(str(exc), detail=type(exc).__name__)
                self.report.failed.append((label, str(exc)))
                self.say(f"{name} FAILED after {since(started)}: {exc}")
                self.manifest.save()
                if isinstance(node, Phase):
                    blocked = True
                continue
            node.finish(rows=rows)
            self.report.completed.append(label)
            counted = f", {rows:,} rows" if rows is not None else ""
            self.say(f"{name} done in {since(started)}{counted}")
            self.manifest.save()
        return self.report

    def _rollback(self) -> None:
        for connection in (self.projects, self.cosmos):
            try:
                connection.rollback()
            except Exception:
                pass

    def _run_unit(self, kind: str, node: Any, path: Path) -> int | None:
        if kind == "setup":
            self._run_setup(path)
            return None
        if kind == "upload_cohorts":
            return self._run_uploads(node, path)
        if kind == "pk":
            return self._run_pk(node, path)
        return self._run_run(node, path)

    # --------------------------------------------------------------- setup

    def _run_setup(self, path: Path) -> None:
        """Create the destination tables once; runs then append to them.

        Resuming, the tables are kept: they hold the finished batches.
        """
        doc = load_yaml(path) or {}
        cohorts = session_cohorts(self.manifest, self.session)
        for block in local_sql.render_setup(
            doc,
            cohorts,
            f"{self.session.session_id}/setup",
            keep=self.resuming,
            batched=run_destinations(self.manifest, self.session),
        ):
            self._execute(self.projects, block.sql, label=block.block_id)
        self.projects.commit()
        node_outputs = {"linked_server": self.report.linked_server, "tables": len(cohorts)}
        self.session.phases[0].outputs.update(node_outputs)

    # ------------------------------------------------------------- uploads

    def _run_uploads(self, node: Any, path: Path) -> int | None:
        """Land each upload in Projects, then load its Cosmos temp from that copy (D54).

        Resuming, the copies are kept and the files are not read: the copy is
        the source, so the batches still to run see what the finished ones saw.

        A non-PK upload lands once per pull, by the first session to get here;
        later sessions use that copy (D61). Its Cosmos temp is loaded only
        where this session's cohorts read it: a temp lives as long as its
        session's connection, so each reader loads its own. An uploaded PK is
        landed and loaded in every session, as before.
        """
        doc = load_yaml(path) or {}
        enabled = uploads.enabled_uploads(doc)
        if not enabled:
            return None
        uploaded = 0
        report: dict[str, dict[str, Any]] = {}
        for cohort in enabled:
            dest = uploads.upload_dest(cohort)
            copy = destination(self.project_db, uploads.copy_table(dest))
            is_pk = str(cohort.get("type", "")).lower() == "pk"
            landed = None if is_pk else self.manifest.uploads_landed.get(dest)
            if self.resuming or landed:
                if not self._projects_table_exists(copy):
                    why = (
                        "The finished batches were pulled with it"
                        if self.resuming else
                        f"It was landed earlier in this pull ({landed.get('landed_at')}) and "
                        "the sessions before this one read it"
                    )
                    raise SessionError(
                        f"{copy} is missing. {why}, so landing the file again could mix "
                        "populations. Run --repull."
                    )
                state = "kept" if self.resuming else "landed earlier in this pull"
                self.say(f"{dest}: {state} in Projects", 1)
            else:
                self.say(f"{dest}: into Projects started", 1)
                started = time.monotonic()
                rows = self._land_upload(cohort, copy)
                counted = f"{rows:,} rows" if rows is not None else "copied"
                self.say(f"{dest}: {counted} into Projects in {since(started)}", 1)
                if not is_pk:
                    self.manifest.uploads_landed[dest] = {
                        "table": copy, "rows": rows, "landed_at": now_iso(),
                        "by_session": self.session.session_id,
                    }
                state = "landed"
            if is_pk or self._session_reads(dest):
                self.say(f"{dest}: into Cosmos started", 1)
                started = time.monotonic()
                loaded = self._load_temp_from_copy(dest, copy)
                self.say(f"{dest}: {loaded:,} rows into Cosmos in {since(started)}", 1)
                uploaded += loaded
                cosmos: Any = loaded
            else:
                cosmos = "not read in this session"
                self.say(f"{dest}: not sent to Cosmos, no table here reads it", 1)
            report[dest] = {"projects": state, "cosmos": cosmos}
        node.outputs["uploads"] = report
        return uploaded

    def _session_reads(self, dest: str) -> bool:
        return session_reads(self.manifest, self.session, dest, self.planned_prefix or self.prefix)

    def _land_upload(self, cohort: dict[str, Any], copy: str) -> int | None:
        """The file (or dbtable) into its typed Projects copy, committed.

        Returns the file's row count; None for a dbtable, copied server-side.
        """
        if uploads.upload_kind(cohort) == "dbtable":
            source = cohort.get("source_table") or uploads.upload_dest(cohort)
            self._execute(
                self.projects,
                uploads.render_copy_dbtable(copy, destination(self.project_db, source)),
                label=f"upload {cohort.get('name')} copy",
            )
            self.projects.commit()
            return None
        table = uploads.read_parquet(cohort, self.upload_root)
        self.report.warnings.extend(table.notes)
        self._execute(
            self.projects, uploads.render_create(copy, table.columns),
            label=f"upload {cohort.get('name')} copy",
        )
        if table.rows:
            bulk_insert(
                self.projects, copy, table.column_names, table.rows,
                chunk_size=self.settings.upload_chunk,
            )
        self.projects.commit()
        return len(table.rows)

    def _projects_table_exists(self, table: str) -> bool:
        cursor = self.projects.cursor()
        cursor.execute(f"SELECT OBJECT_ID(N'{table}', N'U');")
        row = cursor.fetchone()
        return bool(row and row[0] is not None)

    def _describe(self, table: str) -> list[tuple[str, str]]:
        """A Projects table's columns and their types."""
        sql, params = uploads.describe_sql(self.project_db, table)
        cursor = self.projects.cursor()
        cursor.execute(sql, params)
        columns = [(str(row[0]), uploads.type_from_info(*row[1:6])) for row in cursor.fetchall()]
        if not columns:
            raise SessionError(f"Could not read the columns of {self.project_db}.dbo.{table}.")
        return columns

    def _load_temp_from_copy(self, dest: str, copy: str) -> int:
        """Create the Cosmos temp with the copy's types, and fill it from the copy."""
        columns = self._describe(uploads.copy_table(dest))
        temp = global_temp(dest, self.prefix)
        self._execute(self.cosmos, uploads.render_create(temp, columns), label=f"upload {dest}")
        cursor = self.projects.cursor()
        cursor.execute(f"SELECT * FROM {copy};")
        rows = [tuple(row) for row in cursor.fetchall()]
        if rows:
            bulk_insert(
                self.cosmos, temp, [name for name, _ in columns], rows,
                chunk_size=self.settings.upload_chunk,
            )
        self.cosmos.commit()
        return len(rows)

    # ------------------------------------------------------------------ pk

    def _run_pk(self, node: Any, path: Path) -> int | None:
        """Build the PK, land it in Projects, and prove its key is unique."""
        doc = load_yaml(path) or {}
        unit = plan_unit(
            self.manifest, self.session, "pk", node, path, self.report.linked_server,
            resuming=self.resuming,
        )
        rows = self._run_pair(unit)
        node.outputs["global_temp"] = global_temp(self.session.pk_table or "", self.prefix)
        node.outputs["local_table"] = destination(self.project_db, self._pk_copy())
        sampled = self._sample_control(node, doc)
        total = self._verify_pk_uniqueness(doc)
        self._write_pk_parquet(node, doc)
        if sampled is not None:
            return sampled
        return rows if rows is not None else total

    def _write_pk_parquet(self, node: Any, doc: dict[str, Any]) -> None:
        """The whole PK as a parquet as soon as it lands, before any run (D87).

        Where Artifacts would put it, and replaced by Artifacts later. A
        failure here warns: the pull does not need the file. An uploaded PK
        is a file already.
        """
        if not self._pk_is_generated():
            return
        from .artifacts import pk_parquet_path, write_whole_table
        from .pulls import run_folder

        pk_table = self.session.pk_table or ""
        path = pk_parquet_path(self.manifest.path, self._pk_cohort(doc) or {}, doc, pk_table)
        self.say(f"{pk_table}: writing parquet", 1)
        started = time.monotonic()
        try:
            rows = write_whole_table(self.projects, self.project_db, self._pk_copy(), path)
        except Exception as exc:  # noqa: BLE001 - reported; the pull goes on
            self.report.warnings.append(
                f"{node.label}: the PK was not written to parquet ({exc}). The pull goes "
                "on; Artifacts writes it once the pull has finished."
            )
            return
        try:
            shown = path.relative_to(run_folder(self.manifest.path)).as_posix()
        except ValueError:
            shown = str(path)
        node.outputs["pk_parquet"] = {"file": shown, "rows": rows}
        self.say(f"{pk_table}: {rows:,} rows written to {shown} in {since(started)}", 1)

    def _pk_cohort(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (c for c in doc.get("cohorts") or []
             if isinstance(c, dict) and c.get("dest_table") == self.session.pk_table),
            None,
        )

    def _pk_sampled(self) -> bool:
        return bool(control_samples(self._pk_cohort(self._phase_doc("pk"))))

    def _sample_control(self, node: Any, doc: dict[str, Any]) -> int | None:
        """Keep `row_mult` times the case's rows, batch by batch (D59).

        Done to the PK's Projects copy once it has landed, so that copy is the
        sample, and every run is drawn from it. The rows kept are the first in
        hash order of the key: pseudo-random, the same on every run. Returns
        how many were kept, or None when this PK is not a sampled control.
        """
        cohort = self._pk_cohort(doc)
        samples = control_samples(cohort)
        if not samples:
            return None
        pk_table = str(self.session.pk_table)
        if len(samples) > 1:
            raise SessionError(f"{node.label}: {pk_table} is a control in more than one multiplier.")
        item = samples[0]
        case = str(item.get("matched_to") or "")
        if not case:
            raise SessionError(
                f"{node.label}: {pk_table} is a control but names no case. Export the split again."
            )
        if not self._projects_table_exists(destination(self.project_db, case)):
            raise SessionError(
                f"{node.label}: {pk_table} is sampled against {case}, whose PK is not in "
                f"{self.project_db}. Its session comes earlier in the manifest: run it first."
            )
        keys = server_sql.pk_key(cohort)
        row_mult = float(item["row_mult"])
        kept_total = 0
        per_batch: dict[str, dict[str, int]] = {}
        seen: set[str] = set()
        cursor = self.projects.cursor()
        for batch in [run.batch for run in self.session.runs] or [None]:
            stratum = json.dumps((batch or {}).get("dimensions") or [], sort_keys=True, default=str)
            if stratum in seen:
                continue
            seen.add(stratum)
            label = str((batch or {}).get("name") or local_sql.UNBATCHED_LABEL)
            cases = self._count(count_batch_rows(self.project_db, case, batch))
            controls = self._count(count_batch_rows(self.project_db, self._pk_copy(), batch))
            keep = int(cases * row_mult)
            if controls < keep:
                self.report.warnings.append(
                    f"{node.label}: {label} has {controls:,} controls for {cases:,} cases; "
                    f"{row_mult:g}x would be {keep:,}, so all are kept."
                )
            try:
                selection = sample_down(self.project_db, self._pk_copy(), batch, keys, keep)
            except BatchError as exc:
                raise SessionError(f"{node.label}: {exc}") from exc
            cursor.execute(selection.sql, selection.params)
            kept = min(controls, keep)
            kept_total += kept
            per_batch[label] = {"cases": cases, "controls": kept}
        self.projects.commit()
        node.outputs["control_sample"] = {
            "matched_to": case, "row_mult": row_mult, "per_batch": per_batch,
        }
        return kept_total

    def _count(self, selection: Any) -> int:
        cursor = self.projects.cursor()
        cursor.execute(selection.sql, selection.params)
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _pk_copy(self) -> str:
        """The PK's Projects copy, which uniqueness, batches and chunks read.

        A generated PK lands under its own name; an uploaded one as its
        `upload_` copy (D54).
        """
        pk_table = self.session.pk_table or ""
        return pk_table if self._pk_is_generated() else uploads.copy_table(pk_table)

    def _verify_pk_uniqueness(self, doc: dict[str, Any]) -> int | None:
        """A non-unique key makes ORDER BY arbitrary, so chunks stop being stable."""
        keys = self._pk_key_columns(doc)
        if not keys:
            self.report.warnings.append(
                f"PK {self.session.pk_table} declares no key, so its uniqueness cannot be "
                "checked and a chunk: cannot order it. Give the PK cohort dedup_keys "
                "or key_column."
            )
            return None
        table = destination(self.project_db, self._pk_copy())
        columns = ", ".join(f"[{k}]" for k in keys)
        cursor = self.projects.cursor()
        # COUNT(DISTINCT a, b) is not T-SQL; count the distinct rows instead.
        cursor.execute(
            f"SELECT COUNT_BIG(1), "
            f"(SELECT COUNT_BIG(1) FROM (SELECT DISTINCT {columns} FROM {table}) AS d) "
            f"FROM {table};"
        )
        row = cursor.fetchone()
        if not row:
            return None
        total, distinct = int(row[0]), int(row[1])
        if total != distinct:
            raise SessionError(
                f"PK {table} has {total} rows but only {distinct} distinct "
                f"{', '.join(keys)}. Row chunking orders by that key, so duplicates make "
                "a chunk mean different rows each run. Add dedup_keys to the PK cohort."
            )
        if total >= LARGE_ROW_WARNING:
            self.report.warnings.append(
                f"PK {table} has {total:,} rows, past the {LARGE_ROW_WARNING:,} warning "
                "threshold. Check the filter before running the fact pulls."
            )
        return total

    def _pk_key_columns(self, doc: dict[str, Any]) -> list[str]:
        """The PK's key, by the one rule (D69); an uploaded PK's from its source."""
        keys = server_sql.pk_key(self._pk_cohort(doc))
        if keys:
            return keys
        pk_source = next((p.pk_source for p in self.session.phases if p.pk_source), None)
        if pk_source and pk_source.get("key_columns"):
            return [str(k) for k in pk_source["key_columns"]]
        return []

    # ----------------------------------------------------------------- run

    def _run_run(self, node: Any, path: Path) -> int | None:
        unit = plan_unit(
            self.manifest, self.session, "run", node, path, self.report.linked_server,
            resuming=self.resuming,
        )
        open_dims = open_dimensions(node.batch)
        size = self._chunk_size(node)
        if size is None and not open_dims:
            self._materialize_batch(node)
            return self._run_pair(unit)

        # Chunks run inside their batch (D53), and so do the values of a
        # `values: all` dimension, found now that the PK is in Projects (D82):
        # clear the batch's rows once, then refill the PK temp and land each
        # value, and each chunk of it, in turn. All land under the run's own
        # label. A failure fails the run, and a retry clears and redoes it all.
        slices = self._value_slices(node, open_dims) if open_dims else [(None, node.batch)]
        self._run_blocks([b for b in unit.local_blocks if b.meta.get("clears")], self.projects)
        server_total: dict[str, int] = {}
        local_rows: dict[str, int] = {}
        for position, (value_label, batch) in enumerate(slices, start=1):
            value_where = ""
            if value_label is not None:
                value_where = node.outputs["value"] = f"v{position}of{len(slices)} ({value_label})"
            self._where = value_where
            chunks = 1
            if size is not None:
                total = self._count_batch(node, batch)
                chunks = max(1, math.ceil(total / size))
                node.outputs["batch_pk_rows_total"] = total
            for index in range(chunks):
                if size is not None:
                    node.outputs["chunk"] = f"c{index + 1}of{chunks}"
                    self._where = " ".join(filter(None, (value_where, node.outputs["chunk"])))
                self.manifest.save()
                self._materialize_batch(node, chunk_index=index, batch=batch)
                server_rows, local_rows = self._execute_unit(unit, clear=False)
                for dest, count in server_rows.items():
                    server_total[dest] = server_total.get(dest, 0) + count
        self._check_counts(server_total, local_rows)
        return next(iter(server_total.values()), None)

    def _value_slices(self, node: Any, dims: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """One batch per combination of values the open dimensions take in
        this batch's PK rows, NULL included: nothing is left out (D82)."""
        selection = distinct_values(self.project_db, self._pk_copy(), node.batch, dims)
        cursor = self.projects.cursor()
        cursor.execute(selection.sql, selection.params)
        found = [tuple(row) for row in cursor.fetchall()]
        node.outputs["values_found"] = len(found)
        if not found:
            self.report.warnings.append(
                f"{node.label}: no PK rows, so no values of "
                f"{', '.join(str(d.get('column')) for d in dims)} to pull."
            )
        return [
            ("-".join("NULL" if v is None else str(v) for v in values), value_batch(node.batch, dims, values))
            for values in found
        ]

    def _chunk_size(self, node: Any) -> int | None:
        """Rows per chunk, or None for a run that is not chunked."""
        if not node.batch:
            return None
        # The open dimensions are resolved per value; only the chunk matters here.
        batch = {**node.batch, "runtime": [
            d for d in node.batch.get("runtime") or [] if d not in open_dimensions(node.batch)
        ]}
        try:
            _, size = chunk_clause(batch, self._pk_key_columns(self._phase_doc("pk")))
        except BatchError as exc:
            raise SessionError(f"{node.label}: {exc}") from exc
        return int(size) if size else None

    def _count_batch(self, node: Any, batch: dict[str, Any] | None = None) -> int:
        pk_table = self.session.pk_table
        if not pk_table:
            raise SessionError(f"{node.label}: the session has no pk_table to chunk.")
        selection = count_batch_rows(self.project_db, self._pk_copy(), batch or node.batch)
        cursor = self.projects.cursor()
        cursor.execute(selection.sql, selection.params)
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _pk_is_generated(self) -> bool:
        pk_source = next((p.pk_source for p in self.session.phases if p.pk_source), None)
        return not pk_source or pk_source.get("kind") == "generated"

    def _materialize_batch(self, node: Any, chunk_index: int = 0,
                           batch: dict[str, Any] | None = None) -> None:
        """Narrow the PK temp to just this batch, leaving cohort SQL untouched.

        The run YAML joins the PK temp by name, so replacing its contents is
        enough; nothing in the rendered SQL needs to know about batching.
        `batch` narrows further, to one value of a `values: all` dimension.
        """
        batch = batch or node.batch
        if not batch:
            # Resuming, the PK query did not run, so its temp does not exist:
            # rebuild it whole from the Projects copy. A sampled control's
            # temp still holds every row it was built with, so it too is
            # rebuilt from its copy, the sample (D59). An uploaded PK was
            # rebuilt by the upload phase. A table group's new connection has
            # no PK temp at all, of either kind (D134).
            needed = self._pk_temp_missing or (
                (self.resuming or self._pk_sampled()) and self._pk_is_generated()
            )
            if not needed:
                return
            batch = {"name": local_sql.UNBATCHED_LABEL, "dimensions": [], "runtime": []}
        pk_table = self.session.pk_table
        if not pk_table:
            raise SessionError(f"{node.label}: the session has no pk_table to narrow.")

        doc = self._phase_doc("pk")
        keys = self._pk_key_columns(doc)
        try:
            selection = select_batch_rows(
                self.project_db, self._pk_copy(), batch, keys, chunk_index=chunk_index
            )
        except BatchError as exc:
            raise SessionError(f"{node.label}: {exc}") from exc

        cursor = self.projects.cursor()
        cursor.execute(selection.sql, selection.params)
        columns = [column[0] for column in cursor.description or []]
        rows = [tuple(row) for row in cursor.fetchall()]
        if not rows:
            self.report.warnings.append(
                f"{node.label}: batch ({selection.description}) matched no PK rows."
            )

        temp = global_temp(pk_table, self.prefix)
        if self._pk_is_generated():
            pk_doc_cohort = next(
                (c for c in doc.get("cohorts") or [] if isinstance(c, dict)
                 and c.get("dest_table") == pk_table),
                None,
            )
            if pk_doc_cohort is None:
                raise SessionError(f"{node.label}: no PK cohort named {pk_table!r} in pk.yaml.")
            shell, _ = server_sql.render_cohort(pk_doc_cohort, doc)
            create_only = shell.split("INSERT INTO")[0]
        else:
            # An uploaded PK's temp takes its copy's types.
            create_only = uploads.render_create(temp, self._describe(self._pk_copy()))
        started = time.monotonic()
        self._execute(self.cosmos, create_only, label=f"{node.label} batch shell")
        if rows:
            bulk_insert(
                self.cosmos, temp, columns, rows, chunk_size=self.settings.upload_chunk
            )
        self.cosmos.commit()
        self._pk_temp_missing = False
        self.say(f"{self._at()}{pk_table}: {len(rows):,} PK rows into Cosmos in {since(started)}", 1)
        node.outputs["batch_pk_rows"] = len(rows)
        node.outputs["batch"] = selection.description

    # ------------------------------------------------------------- helpers

    def _at(self) -> str:
        """Where in its run a line is, as its prefix: `c3of12 `, or nothing."""
        return f"{self._where} " if self._where else ""

    def _run_pair(self, unit: Unit) -> int | None:
        """Server blocks, then the local transfer, then compare both counts."""
        server_rows, local_rows = self._execute_unit(unit)
        self._check_counts(server_rows, local_rows)
        return next(iter(server_rows.values()), None)

    def _run_blocks(self, blocks: list[Any], connection: Any) -> None:
        for block in blocks:
            self._execute(connection, block.sql, label=block.block_id)
        connection.commit()

    def _execute_unit(self, unit: Unit, *, clear: bool = True) -> tuple[dict[str, int], dict[str, int]]:
        """Run a unit's SQL; return Cosmos and Projects row counts per destination.

        One cohort at a time (D55): build its temp, land it in Projects and
        commit, before the next is pulled, so a failure loses at most the
        cohort in flight. Clears run first, together, since they only empty.
        """
        server_rows: dict[str, int] = {}
        local_rows: dict[str, int] = {}
        clears = [b for b in unit.local_blocks if b.meta.get("clears")]
        transfers = [b for b in unit.local_blocks if not b.meta.get("clears")]
        if clear:
            for block in clears:
                self._execute(self.projects, block.sql, label=block.block_id)
                self.projects.commit()
        for block in unit.server_blocks:
            self.say(f"{self._at()}{block.dest_table} started", 1)
            started = time.monotonic()
            outcome = self._execute(self.cosmos, block.sql, label=block.block_id)
            for row in outcome.rows_of("DestTable", "RowCount"):
                server_rows[str(row["DestTable"])] = int(row["RowCount"])
            self.cosmos.commit()
            built = since(started)
            started = time.monotonic()
            for local in [b for b in transfers if b.dest_table == block.dest_table]:
                self._land(local, local_rows)
            rows = server_rows.get(str(block.dest_table))
            counted = f"{rows:,} rows" if rows is not None else "built"
            self.say(
                f"{self._at()}{block.dest_table}: {counted} "
                f"(Cosmos {built}, into Projects {since(started)})", 1,
            )
        landed = {b.dest_table for b in unit.server_blocks}
        for local in [b for b in transfers if b.dest_table not in landed]:
            self._land(local, local_rows)
        return server_rows, local_rows

    def _land(self, block: Any, local_rows: dict[str, int]) -> None:
        """Transfer one cohort into Projects and commit it."""
        outcome = self._execute(self.projects, block.sql, label=block.block_id)
        for row in outcome.rows_of("DestTable", "Side", "RowCount"):
            if row["Side"] == "projects":
                local_rows[str(row["DestTable"])] = int(row["RowCount"])
        for row in outcome.rows_of("DestTable", "Column", "MaxLength"):
            if row["MaxLength"] is None:
                continue  # every value empty: nothing measured
            self.report.record_width(
                str(row["DestTable"]), str(row["Column"]),
                str(row.get("DeclaredType") or ""), row["MaxLength"],
            )
        self.projects.commit()

    def _check_counts(self, server_rows: dict[str, int], local_rows: dict[str, int]) -> None:
        for dest, count in server_rows.items():
            if count >= LARGE_ROW_WARNING:
                self.report.warnings.append(
                    f"{dest} produced {count:,} rows, past the "
                    f"{LARGE_ROW_WARNING:,} warning threshold."
                )
            landed = local_rows.get(dest)
            if landed is not None and landed != count:
                self.report.warnings.append(
                    f"{dest}: Cosmos reported {count:,} rows but {landed:,} landed in "
                    "Projects. The transfer did not carry everything."
                )
