"""Traversal order and resume policy."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..executor import (
    RESUME_FULL,
    RESUME_PARTIAL,
    PlanError,
    excluded_units,
    iter_units,
    plan,
    plan_session,
    session_cohorts,
    should_execute,
    write_sql,
)
from ..manifest import Manifest
from .support import sample_manifest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"


class TraversalTests(unittest.TestCase):
    def test_phases_come_in_routine_order_then_runs(self):
        manifest = sample_manifest()
        kinds = [kind for kind, _, _ in iter_units(manifest, manifest.sessions[0])]
        self.assertEqual(kinds, ["setup", "upload_cohorts", "pk", "run", "run"])

    def test_runs_keep_manifest_order(self):
        manifest = sample_manifest()
        labels = [
            node.label for kind, node, _ in iter_units(manifest, manifest.sessions[0])
            if kind == "run"
        ]
        self.assertEqual(labels, ["UCblackPatients__LA-Female", "UCblackPatients__LA-Male"])


class ShouldExecuteTests(unittest.TestCase):
    def node(self, status, epoch=None):
        manifest = sample_manifest()
        node = manifest.sessions[0].phases[0]
        node.data["status"] = status
        if epoch:
            node.data["epoch"] = epoch
        return node

    def test_pending_runs(self):
        run, _ = should_execute(self.node("pending"), "setup")
        self.assertTrue(run)

    def test_skipped_does_not_run(self):
        run, why = should_execute(self.node("skipped"), "setup")
        self.assertFalse(run)
        self.assertIn("deliberately", why)

    def test_interrupted_running_is_resumed(self):
        run, why = should_execute(self.node("running"), "setup")
        self.assertTrue(run)
        self.assertIn("interrupted", why)

    def test_blocked_is_retried(self):
        run, _ = should_execute(self.node("blocked"), "setup")
        self.assertTrue(run)

    def test_failed_needs_an_explicit_retry(self):
        node = self.node("failed")
        self.assertFalse(should_execute(node, "run")[0])
        self.assertTrue(should_execute(node, "run", retry_failed=True)[0])

    def test_done_in_this_session_is_skipped(self):
        node = self.node("done", epoch="e1")
        run, why = should_execute(node, "setup", current_epoch="e1")
        self.assertFalse(run)
        self.assertIn("already done", why)

    def test_done_under_a_previous_connection_replays(self):
        # Global temps died with that connection, so the status is true but the
        # output is gone.
        node = self.node("done", epoch="e1")
        run, why = should_execute(node, "setup", current_epoch="e2")
        self.assertTrue(run)
        self.assertIn("server state is gone", why)

    def test_partial_resume_keeps_completed_runs(self):
        # A run's durable result is rows in a Projects table, which survive.
        node = self.node("done", epoch="e1")
        run, why = should_execute(node, "run", current_epoch="e2", mode=RESUME_PARTIAL)
        self.assertFalse(run)
        self.assertIn("survive", why)

    def test_partial_resume_still_replays_phases(self):
        node = self.node("done", epoch="e1")
        run, _ = should_execute(node, "pk", current_epoch="e2", mode=RESUME_PARTIAL)
        self.assertTrue(run)

    def test_manifest_without_epochs_is_not_treated_as_stale(self):
        node = self.node("done")
        self.assertFalse(should_execute(node, "setup", current_epoch="e9")[0])


class PlanningTests(unittest.TestCase):
    def setUp(self):
        if not FIXTURES.is_dir():
            self.skipTest(f"fixtures not found at {FIXTURES}")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "split"
        shutil.copytree(FIXTURES, self.root)
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")

    def test_plans_every_unit_of_a_fresh_manifest(self):
        units = plan(self.manifest)
        self.assertEqual(
            [u.kind for u in units], ["setup", "upload_cohorts", "pk", "run"]
        )

    def test_setup_creates_a_shell_for_every_destination(self):
        setup = plan(self.manifest)[0]
        shells = [b for b in setup.local_blocks if "shell" in b.block_id]
        self.assertEqual(len(shells), len(session_cohorts(self.manifest, self.manifest.sessions[0])))
        self.assertTrue(all("CREATE TABLE" in b.sql for b in shells))

    def test_session_cohorts_are_deduplicated(self):
        names = [c["dest_table"] for c in session_cohorts(self.manifest, self.manifest.sessions[0])]
        self.assertEqual(len(names), len(set(names)))

    def test_run_units_render_both_sides(self):
        run = [u for u in plan(self.manifest) if u.kind == "run"][0]
        self.assertTrue(run.server_blocks)
        self.assertTrue(run.local_blocks)

    def test_completed_work_is_excluded_and_reported(self):
        session = self.manifest.sessions[0]
        session.begin_epoch(linked_server="ls")
        for phase in session.phases:
            phase.start()
            phase.finish()
        session.runs[0].start()
        session.runs[0].fail("boom")
        self.manifest.save()

        reloaded = Manifest.load(self.root / "pullmanifest.yaml")
        left_out = excluded_units(reloaded)
        statuses = {label.split("/")[-1]: status for label, status, _ in left_out}
        self.assertIn("failed", statuses.values())

        units = plan(reloaded)
        # A new connection means the phases replay even though they are done.
        self.assertTrue(any(u.kind == "pk" for u in units))

    def test_missing_phase_yaml_is_refused(self):
        (self.root / "sessions" / "Patients" / "pk.yaml").unlink()
        with self.assertRaises(PlanError):
            plan(self.manifest)

    def test_writes_one_file_per_block(self):
        units = plan(self.manifest)
        with tempfile.TemporaryDirectory() as out:
            written = write_sql(units, Path(out))
            self.assertEqual(len(written), sum(len(u.blocks) for u in units))
            self.assertTrue(all(p.read_text(encoding="utf-8").strip() for p in written))
            self.assertTrue(all(p.suffix == ".sql" for p in written))

    def test_plan_is_empty_when_nothing_needs_doing(self):
        session = self.manifest.sessions[0]
        for child in session.children:
            child.skip("not wanted")
        self.assertEqual(plan_session(self.manifest, session), [])
