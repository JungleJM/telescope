"""Traversal order and resume policy."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..executor import (
    PlanError,
    excluded_units,
    iter_units,
    plan,
    plan_session,
    session_cohorts,
    session_has_work,
    session_resumes,
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
    def node(self, status):
        manifest = sample_manifest()
        node = manifest.sessions[0].phases[0]
        node.data["status"] = status
        return node

    def test_pending_runs(self):
        self.assertTrue(should_execute(self.node("pending"), "setup")[0])

    def test_skipped_does_not_run(self):
        run, why = should_execute(self.node("skipped"), "setup")
        self.assertFalse(run)
        self.assertIn("deliberately", why)

    def test_interrupted_running_is_resumed(self):
        run, why = should_execute(self.node("running"), "run", resuming=True)
        self.assertTrue(run)
        self.assertIn("cleared first", why)

    def test_blocked_is_retried(self):
        self.assertTrue(should_execute(self.node("blocked"), "setup")[0])

    def test_failed_needs_an_explicit_retry(self):
        node = self.node("failed")
        for resuming in (False, True):
            with self.subTest(resuming=resuming):
                self.assertFalse(should_execute(node, "run", resuming=resuming)[0])
                self.assertTrue(
                    should_execute(node, "run", resuming=resuming, retry_failed=True)[0]
                )

    def test_starting_over_reruns_finished_work(self):
        # Setup drops the destinations, so a finished run must refill them.
        run, why = should_execute(self.node("done"), "run", resuming=False)
        self.assertTrue(run)
        self.assertIn("starting over", why)

    def test_resuming_keeps_finished_runs(self):
        run, why = should_execute(self.node("done"), "run", resuming=True)
        self.assertFalse(run)
        self.assertIn("rows are in Projects", why)

    def test_resuming_rebuilds_setup_and_uploads(self):
        for kind in ("setup", "upload_cohorts"):
            with self.subTest(kind=kind):
                self.assertTrue(should_execute(self.node("done"), kind, resuming=True)[0])

    def test_resuming_never_reruns_the_pk_query(self):
        # The remaining batches come from the Projects copy, the population
        # the finished ones came from.
        run, why = should_execute(self.node("done"), "pk", resuming=True)
        self.assertFalse(run)
        self.assertIn("Projects copy", why)


class ResumePolicyTests(unittest.TestCase):
    def session(self, pk_status, run_statuses):
        manifest = sample_manifest()
        session = manifest.sessions[0]
        for phase in session.phases:
            phase.data["status"] = "done" if phase.name != "pk" else pk_status
        for run, status in zip(session.runs, run_statuses):
            run.data["status"] = status
        return manifest, session

    def test_a_session_resumes_once_its_pk_is_done(self):
        self.assertTrue(session_resumes(self.session("done", ["pending", "pending"])[1]))
        self.assertFalse(session_resumes(self.session("failed", ["pending", "pending"])[1]))

    def test_a_finished_session_has_no_work(self):
        manifest, session = self.session("done", ["done", "done"])
        self.assertFalse(session_has_work(manifest, session))

    def test_only_failures_left_need_the_flag(self):
        manifest, session = self.session("done", ["done", "failed"])
        self.assertFalse(session_has_work(manifest, session))
        self.assertTrue(session_has_work(manifest, session, retry_failed=True))

    def test_resetting_starts_a_finished_session_over(self):
        manifest, session = self.session("done", ["done", "done"])
        manifest.reset_all("re-pulled")
        self.assertFalse(session_resumes(session))
        self.assertTrue(session_has_work(manifest, session))
        self.assertTrue(all(child.status == "pending" for child in session.children))


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

    def test_only_run_destinations_carry_a_batch_column(self):
        # Batches are selected from the PK's copy, so it must not grow one.
        shells = {b.dest_table: b.sql for b in plan(self.manifest)[0].local_blocks}
        self.assertIn("[_batch]", shells["OtherHospitalizations"])
        self.assertNotIn("[_batch]", shells["Patients"])

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

        # Only a failure is left, so there is nothing to do without the flag...
        self.assertEqual(plan(reloaded), [])
        # ...and with it the session resumes: server side rebuilt, PK kept,
        # the failed run cleared of any rows it landed, then pulled.
        units = plan(reloaded, retry_failed=True)
        self.assertEqual([u.kind for u in units], ["setup", "upload_cohorts", "run"])
        self.assertIn("IF OBJECT_ID", units[0].local_blocks[0].sql)
        self.assertIn("DELETE FROM", units[-1].local_blocks[0].sql)

    def test_the_upload_note_says_what_will_happen(self):
        # D61: it said every upload goes up to Cosmos, in every session.
        from ..yaml_io import dump_yaml, load_yaml

        path = self.root / "sessions" / "Patients" / "upload_cohorts.yaml"
        doc = load_yaml(path)
        doc["upload_cohorts"].append(dict(doc["upload_cohorts"][0], name="Unused", dest_table="Unused"))
        dump_yaml(doc, path)
        notes = [u for u in plan(self.manifest) if u.kind == "upload_cohorts"][0].notes
        read, unused = notes
        self.assertIn("upload HospitalICDCodes: lands in Projects", read)
        self.assertIn("once for the pull", read)
        self.assertIn("then goes up to Cosmos", read)
        self.assertIn("upload Unused:", unused)
        self.assertIn("not sent to Cosmos", unused)
        self.manifest.uploads_landed["HospitalICDCodes"] = {"table": "x"}
        again = [u for u in plan(self.manifest) if u.kind == "upload_cohorts"][0].notes
        self.assertIn("landed earlier in this pull", again[0])

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
