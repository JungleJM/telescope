"""Executing one session.

The Cosmos connection is held open for the whole session, because every
`##JVM_*` table dies with it. That single fact shapes everything here: the
epoch, what a resume must replay, and why uploads travel through the client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import local_sql, refresh, server_sql, uploads
from .batches import BatchError, select_batch_rows
from .db import DatabaseError, Settings, bulk_insert, capture_server_name, connect, execute_script
from .executor import (
    Unit,
    iter_units,
    plan_unit,
    run_destinations,
    session_cohorts,
    session_has_work,
    session_resumes,
    should_execute,
)
from .manifest import Manifest, Phase, Session
from .naming import destination, global_temp
from .normalize import cosmos_database
from .uploads import UploadError
from .yaml_io import load_yaml

# The old generator warned past this; a pull this size is usually a mistake in
# the filter rather than an intention.
LARGE_ROW_WARNING = 80_000_000


class SessionError(RuntimeError):
    """Raised when a session cannot proceed."""


@dataclass
class SessionReport:
    session_id: str
    epoch: str = ""
    linked_server: str = ""
    completed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


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
    ):
        self.manifest = manifest
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
        self.cosmos = self._connect(
            self.settings.cosmos_connection_string(cosmos_database(doc.get("cosmos_db"))),
            login_timeout=self.settings.login_timeout,
            query_timeout=self.settings.query_timeout,
        )
        # Captured per connection: the instance name changes every time, so a
        # cached one would aim OPENQUERY at a server that is no longer ours.
        linked_server = capture_server_name(self.cosmos)
        self._check_refresh()
        epoch = self.session.begin_epoch(linked_server=linked_server)
        self.report.epoch = epoch
        self.report.linked_server = linked_server

        project_db = doc.get("project_db")
        if not project_db:
            raise SessionError(f"{self.session.session_id}: setup.yaml has no project_db.")
        self.project_db = str(project_db)
        self.projects = self._connect(
            self.settings.projects_connection_string(self.project_db),
            login_timeout=self.settings.login_timeout,
            query_timeout=self.settings.query_timeout,
        )
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
            if blocked:
                node.block("an earlier phase in this session failed")
                self.report.skipped.append(label)
                self.manifest.save()
                continue

            execute, reason = should_execute(
                node, kind, resuming=self.resuming, retry_failed=self.retry_failed
            )
            if not execute:
                self.report.skipped.append(f"{label} ({reason})")
                continue

            node.start()
            self.manifest.save()
            try:
                rows = self._run_unit(kind, node, path)
            except Exception as exc:
                # Nothing a failed unit wrote should be committed along with the
                # next unit's work. The retry clears its batch anyway (D52).
                self._rollback()
                node.fail(str(exc), detail=type(exc).__name__)
                self.report.failed.append((label, str(exc)))
                self.manifest.save()
                if isinstance(node, Phase):
                    blocked = True
                continue
            node.finish(rows=rows)
            self.report.completed.append(label)
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
            return self._run_uploads(path)
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
            execute_script(self.projects, block.sql, label=block.block_id)
        self.projects.commit()
        node_outputs = {"linked_server": self.report.linked_server, "tables": len(cohorts)}
        self.session.phases[0].outputs.update(node_outputs)

    # ------------------------------------------------------------- uploads

    def _run_uploads(self, path: Path) -> int | None:
        doc = load_yaml(path) or {}
        enabled = uploads.enabled_uploads(doc)
        if not enabled:
            return None
        uploaded = 0
        for cohort in enabled:
            kind = uploads.upload_kind(cohort)
            if kind == "csv":
                plan = uploads.plan_csv_upload(cohort, self.upload_root)
            else:
                plan = uploads.plan_dbtable_upload(self.projects, cohort, self.project_db)
            self.report.warnings.extend(plan.notes)
            uploaded += uploads.materialize(
                self.cosmos, plan, chunk_size=self.settings.upload_chunk
            )
            self.cosmos.commit()
        return uploaded

    # ------------------------------------------------------------------ pk

    def _run_pk(self, node: Any, path: Path) -> int | None:
        """Build the PK, land it in Projects, and prove its key is unique."""
        doc = load_yaml(path) or {}
        unit = plan_unit(
            self.manifest, self.session, "pk", node, path, self.report.linked_server,
            resuming=self.resuming,
        )
        rows = self._run_pair(unit)
        node.outputs["global_temp"] = global_temp(self.session.pk_table or "")
        node.outputs["local_table"] = destination(self.project_db, self.session.pk_table or "")
        self._verify_pk_uniqueness(doc)
        return rows

    def _verify_pk_uniqueness(self, doc: dict[str, Any]) -> None:
        """A non-unique key makes ORDER BY arbitrary, so chunks stop being stable."""
        keys = self._pk_key_columns(doc)
        if not keys:
            self.report.warnings.append(
                "PK declares no key_column, so chunk ordering cannot be verified as stable."
            )
            return
        table = destination(self.project_db, self.session.pk_table or "")
        columns = ", ".join(f"[{k}]" for k in keys)
        cursor = self.projects.cursor()
        cursor.execute(f"SELECT COUNT_BIG(1), COUNT_BIG(DISTINCT {columns}) FROM {table};")
        row = cursor.fetchone()
        if not row:
            return
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

    def _pk_key_columns(self, doc: dict[str, Any]) -> list[str]:
        for cohort in doc.get("cohorts") or []:
            if not isinstance(cohort, dict):
                continue
            key = cohort.get("key_column") or cohort.get("key_columns")
            if isinstance(key, str):
                return [key]
            if isinstance(key, list) and key:
                return [str(k) for k in key]
        pk_source = next((p.pk_source for p in self.session.phases if p.pk_source), None)
        if pk_source and pk_source.get("key_columns"):
            return [str(k) for k in pk_source["key_columns"]]
        return []

    # ----------------------------------------------------------------- run

    def _run_run(self, node: Any, path: Path) -> int | None:
        self._materialize_batch(node)
        unit = plan_unit(
            self.manifest, self.session, "run", node, path, self.report.linked_server,
            resuming=self.resuming,
        )
        return self._run_pair(unit)

    def _pk_is_generated(self) -> bool:
        pk_source = next((p.pk_source for p in self.session.phases if p.pk_source), None)
        return not pk_source or pk_source.get("kind") == "generated"

    def _materialize_batch(self, node: Any) -> None:
        """Narrow the PK temp to just this batch, leaving cohort SQL untouched.

        The run YAML joins the PK temp by name, so replacing its contents is
        enough; nothing in the rendered SQL needs to know about batching.
        """
        batch = node.batch
        if not batch:
            # Resuming, the PK query did not run, so its temp does not exist:
            # rebuild it whole from the Projects copy. An uploaded PK was
            # rebuilt by the upload phase.
            if not (self.resuming and self._pk_is_generated()):
                return
            batch = {"name": local_sql.UNBATCHED_LABEL, "dimensions": [], "runtime": []}
        pk_table = self.session.pk_table
        if not pk_table:
            raise SessionError(f"{node.label}: the session has no pk_table to narrow.")

        doc = self._phase_doc("pk")
        keys = self._pk_key_columns(doc)
        try:
            selection = select_batch_rows(self.project_db, pk_table, batch, keys)
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

        temp = global_temp(pk_table)
        pk_doc_cohort = next(
            (c for c in doc.get("cohorts") or [] if isinstance(c, dict)
             and c.get("dest_table") == pk_table),
            None,
        )
        if pk_doc_cohort is None:
            raise SessionError(f"{node.label}: no PK cohort named {pk_table!r} in pk.yaml.")
        shell, _ = server_sql.render_cohort(pk_doc_cohort, doc)
        create_only = shell.split("INSERT INTO")[0]
        execute_script(self.cosmos, create_only, label=f"{node.label} batch shell")
        if rows:
            bulk_insert(
                self.cosmos, temp, columns, rows, chunk_size=self.settings.upload_chunk
            )
        self.cosmos.commit()
        node.outputs["batch_pk_rows"] = len(rows)
        node.outputs["batch"] = selection.description

    # ------------------------------------------------------------- helpers

    def _run_pair(self, unit: Unit) -> int | None:
        """Server blocks, then the local transfer, then compare both counts."""
        server_rows: dict[str, int] = {}
        for block in unit.server_blocks:
            outcome = execute_script(self.cosmos, block.sql, label=block.block_id)
            for row in outcome.rows_of("DestTable", "RowCount"):
                server_rows[str(row["DestTable"])] = int(row["RowCount"])
        self.cosmos.commit()

        local_rows: dict[str, int] = {}
        for block in unit.local_blocks:
            outcome = execute_script(self.projects, block.sql, label=block.block_id)
            for row in outcome.rows_of("DestTable", "Side", "RowCount"):
                if row["Side"] == "projects":
                    local_rows[str(row["DestTable"])] = int(row["RowCount"])
            for row in outcome.rows_of("DestTable", "Column", "MaxLength"):
                if row["MaxLength"] is None:
                    continue
                self.report.warnings.append(
                    f"{row['DestTable']}.{row['Column']} declared {row['DeclaredType']}, "
                    f"widest value {row['MaxLength']}"
                    if row.get("DeclaredType") else
                    f"{row['DestTable']}.{row['Column']} widest value {row['MaxLength']}"
                )
        self.projects.commit()

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
        return next(iter(server_rows.values()), None)
