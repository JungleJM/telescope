"""Manifest loading, validation, status transitions, and round-tripping."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from ..manifest import Manifest, ManifestError
from ..models import BLOCKED, DONE, FAILED, PENDING, RUNNING, SKIPPED
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST, sample_manifest


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.manifest_path = self.tmp / "pullmanifest.yaml"


class LoadTests(unittest.TestCase):
    def test_reads_sessions_phases_and_runs(self):
        manifest = sample_manifest()
        self.assertEqual(len(manifest.sessions), 2)
        self.assertEqual(manifest.sessions[0].session_id, "UCblackPatients")
        self.assertEqual(
            [phase.name for phase in manifest.sessions[0].phases],
            ["setup", "upload_cohorts", "pk"],
        )
        self.assertEqual(len(manifest.sessions[0].runs), 2)

    def test_source_is_template_and_recipes(self):
        # The design prose said `source.preyaml`; makeYaml emits template +
        # recipes. Pin the real contract so drift shows up here.
        self.assertEqual(sorted(sample_manifest().source), ["recipes", "template"])

    def test_exposes_both_pk_source_kinds(self):
        manifest = sample_manifest()
        generated = manifest.sessions[0].phases[2]
        uploaded = manifest.sessions[1].phases[2]
        self.assertEqual(generated.pk_source["kind"], "generated")
        self.assertEqual(uploaded.pk_source["kind"], "uploaded_cohort")
        self.assertEqual(uploaded.pk_source["key_columns"], ["PatientDurableKey"])

    def test_resolves_yaml_paths_against_manifest_directory(self):
        manifest = sample_manifest()
        setup = manifest.sessions[0].phases[0]
        self.assertEqual(manifest.resolve(setup), Path("split/sessions/UCblackPatients/setup.yaml"))


class ValidationTests(unittest.TestCase):
    def assertRejects(self, data, fragment):
        with self.assertRaises(ManifestError) as caught:
            Manifest(data)
        self.assertIn(fragment.lower(), str(caught.exception).lower())

    def test_rejects_unsupported_version(self):
        self.assertRejects({"manifest_version": 99, "sessions": []}, "manifest_version")

    def test_rejects_missing_sessions_list(self):
        self.assertRejects({"manifest_version": 1}, "sessions")

    def test_rejects_non_mapping_root(self):
        with self.assertRaises(ManifestError):
            Manifest(["not", "a", "mapping"])

    def test_rejects_unknown_phase_name(self):
        self.assertRejects(
            {
                "manifest_version": 1,
                "sessions": [
                    {
                        "session_id": "S",
                        "status": "pending",
                        "phases": {"teardown": {"yaml": "x.yaml", "status": "pending"}},
                        "runs": [],
                    }
                ],
            },
            "unknown phase",
        )

    def test_rejects_duplicate_run_id(self):
        run = {"run_id": "R", "yaml": "r.yaml", "status": "pending"}
        self.assertRejects(
            {
                "manifest_version": 1,
                "sessions": [
                    {
                        "session_id": "S",
                        "status": "pending",
                        "phases": {},
                        "runs": [dict(run), dict(run)],
                    }
                ],
            },
            "duplicate run_id",
        )

    def test_rejects_duplicate_session_id(self):
        session = {"session_id": "S", "status": "pending", "phases": {}, "runs": []}
        self.assertRejects(
            {"manifest_version": 1, "sessions": [dict(session), dict(session)]},
            "duplicate session_id",
        )

    def test_rejects_phase_without_yaml_path(self):
        self.assertRejects(
            {
                "manifest_version": 1,
                "sessions": [
                    {
                        "session_id": "S",
                        "status": "pending",
                        "phases": {"setup": {"status": "pending"}},
                        "runs": [],
                    }
                ],
            },
            "missing its `yaml`",
        )

    def test_rejects_unknown_status_value(self):
        data = copy.deepcopy(SAMPLE_MANIFEST)
        data["sessions"][0]["phases"]["setup"]["status"] = "finished"
        with self.assertRaises(Exception):
            Manifest(data)


class TransitionTests(unittest.TestCase):
    def test_start_marks_running_and_stamps_start(self):
        phase = sample_manifest().sessions[0].phases[0]
        phase.start()
        self.assertEqual(phase.status, RUNNING)
        self.assertIsNotNone(phase.data["started_at"])
        self.assertIsNone(phase.data["finished_at"])

    def test_finish_records_rows_outputs_and_duration(self):
        phase = sample_manifest().sessions[0].phases[2]
        phase.start()
        phase.finish(rows=12345, outputs={"global_temp": "##JVM_UCblackPatients"})
        self.assertEqual(phase.status, DONE)
        self.assertEqual(phase.rows, 12345)
        self.assertEqual(phase.outputs["global_temp"], "##JVM_UCblackPatients")
        self.assertIsNotNone(phase.data["finished_at"])
        self.assertGreaterEqual(phase.data["duration"]["seconds"], 0)

    def test_fail_records_message_and_detail(self):
        run = sample_manifest().sessions[0].runs[0]
        run.start()
        run.fail("OPENQUERY failed", detail="Login timeout expired")
        self.assertEqual(run.status, FAILED)
        self.assertEqual(run.error["message"], "OPENQUERY failed")
        self.assertEqual(run.error["detail"], "Login timeout expired")

    def test_retry_clears_previous_error_and_duration(self):
        run = sample_manifest().sessions[0].runs[0]
        run.start()
        run.fail("boom")
        run.start()
        self.assertEqual(run.status, RUNNING)
        self.assertIsNone(run.error)
        self.assertNotIn("duration", run.data)

    def test_skip_and_block_use_note_not_error(self):
        # A skipped phase is not a failure; an `error` block would read like one.
        for action, expected in (("skip", SKIPPED), ("block", BLOCKED)):
            with self.subTest(action=action):
                run = sample_manifest().sessions[0].runs[1]
                getattr(run, action)("upstream PK failed")
                self.assertEqual(run.status, expected)
                self.assertEqual(run.note, "upstream PK failed")
                self.assertIsNone(run.error)


class SessionRollupTests(unittest.TestCase):
    def test_pending_while_untouched(self):
        self.assertEqual(sample_manifest().sessions[0].recompute_status(), PENDING)

    def test_running_when_partially_complete(self):
        session = sample_manifest().sessions[0]
        session.phases[0].start()
        session.phases[0].finish()
        self.assertEqual(session.recompute_status(), RUNNING)

    def test_failure_outranks_every_other_state(self):
        session = sample_manifest().sessions[0]
        for phase in session.phases:
            phase.start()
            phase.finish()
        session.runs[0].start()
        session.runs[0].fail("nope")
        session.runs[1].block("upstream failed")
        self.assertEqual(session.recompute_status(), FAILED)

    def test_done_when_every_child_is_done(self):
        session = sample_manifest().sessions[1]
        for child in session.children:
            child.start()
            child.finish()
        self.assertEqual(session.recompute_status(), DONE)

    def test_done_when_children_mix_done_and_skipped(self):
        session = sample_manifest().sessions[1]
        session.phases[0].start()
        session.phases[0].finish()
        session.phases[1].skip("no upload cohorts")
        session.phases[2].start()
        session.phases[2].finish()
        session.runs[0].start()
        session.runs[0].finish()
        self.assertEqual(session.recompute_status(), DONE)

    def test_skipped_only_when_everything_skipped(self):
        session = sample_manifest().sessions[1]
        for child in session.children:
            child.skip()
        self.assertEqual(session.recompute_status(), SKIPPED)

    def test_blocked_when_blocking_is_the_worst_state(self):
        session = sample_manifest().sessions[1]
        for phase in session.phases:
            phase.start()
            phase.finish()
        session.runs[0].block("upstream failed")
        self.assertEqual(session.recompute_status(), BLOCKED)


class EpochTests(unittest.TestCase):
    """Global temps die with the connection; the epoch is how we know."""

    def test_begin_epoch_records_connection_facts(self):
        session = sample_manifest().sessions[0]
        epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
        self.assertEqual(session.epoch, epoch)
        self.assertEqual(session.runtime["linked_server"], "et4003vpdsq1032")
        self.assertIsNotNone(session.runtime["opened_at"])

    def test_new_epoch_clears_a_stale_linked_server(self):
        # The Cosmos instance name changes every connection, so a value from a
        # previous epoch must never survive into the next one.
        session = sample_manifest().sessions[0]
        session.begin_epoch(linked_server="et4003vpdsql032")
        session.begin_epoch()
        self.assertIsNone(session.runtime["linked_server"])

    def test_each_epoch_is_distinct(self):
        session = sample_manifest().sessions[0]
        self.assertNotEqual(session.begin_epoch(), session.begin_epoch())

    def test_finishing_stamps_the_current_epoch(self):
        session = sample_manifest().sessions[0]
        epoch = session.begin_epoch()
        phase = session.phases[2]
        phase.start()
        phase.finish(rows=12345)
        self.assertEqual(phase.epoch, epoch)

    def test_work_from_the_current_epoch_is_not_stale(self):
        session = sample_manifest().sessions[0]
        session.begin_epoch()
        phase = session.phases[2]
        phase.start()
        phase.finish()
        self.assertFalse(phase.is_stale(session.epoch))

    def test_work_from_a_previous_epoch_is_stale(self):
        session = sample_manifest().sessions[0]
        session.begin_epoch()
        for child in session.children:
            child.start()
            child.finish()

        # Restarting the process opens a new connection; the old temps are gone.
        session.begin_epoch()
        self.assertEqual(len(session.stale_children()), len(session.children))
        self.assertTrue(session.phases[2].is_stale(session.epoch))

    def test_unfinished_work_is_never_stale(self):
        session = sample_manifest().sessions[0]
        session.begin_epoch()
        self.assertFalse(session.phases[0].is_stale(session.epoch))
        session.runs[0].block("upstream failed")
        self.assertFalse(session.runs[0].is_stale(session.epoch))

    def test_manifest_without_epochs_is_never_stale(self):
        # Manifests written before epochs existed must not be read as stale.
        session = sample_manifest().sessions[0]
        phase = session.phases[0]
        phase.start()
        phase.finish()
        self.assertIsNone(phase.epoch)
        self.assertFalse(phase.is_stale("some-new-epoch"))


class RoundTripTests(TempDirTestCase):
    def saved_manifest(self) -> Manifest:
        manifest = sample_manifest()
        manifest.path = self.manifest_path
        return manifest

    def test_status_and_rows_survive_save_and_reload(self):
        manifest = self.saved_manifest()
        manifest.sessions[0].phases[0].start()
        manifest.sessions[0].phases[0].finish(rows=42)
        manifest.save()

        reloaded = Manifest.load(self.manifest_path)
        setup = reloaded.sessions[0].phases[0]
        self.assertEqual(setup.status, DONE)
        self.assertEqual(setup.rows, 42)
        self.assertEqual(reloaded.sessions[0].status, RUNNING)

    def test_unknown_keys_survive(self):
        manifest = self.saved_manifest()
        manifest.data["future_field"] = {"added_by": "a later yamlmanager"}
        manifest.sessions[0].runs[0].data["parquet_hint"] = "keep me"
        manifest.save()

        reloaded = Manifest.load(self.manifest_path)
        self.assertEqual(reloaded.data["future_field"], {"added_by": "a later yamlmanager"})
        self.assertEqual(reloaded.sessions[0].runs[0].data["parquet_hint"], "keep me")

    def test_batch_product_and_multiplier_context_survive(self):
        manifest = self.saved_manifest()
        manifest.save()

        run = Manifest.load(self.manifest_path).sessions[0].runs[0]
        self.assertEqual(run.batch["name"], "LA-Female")
        self.assertEqual(
            [dim["value"] for dim in run.batch["dimensions"]],
            ["LA", "Female"],
        )
        self.assertEqual([dim["name"] for dim in run.batch["runtime"]], ["chunk"])

    def test_save_leaves_no_temp_file_behind(self):
        self.saved_manifest().save()
        self.assertEqual(
            sorted(path.name for path in self.tmp.iterdir()),
            ["pullmanifest.yaml"],
        )

    def test_epoch_survives_save_and_reload(self):
        manifest = self.saved_manifest()
        session = manifest.sessions[0]
        epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
        session.phases[0].start()
        session.phases[0].finish()
        manifest.save()

        reloaded = Manifest.load(self.manifest_path).sessions[0]
        self.assertEqual(reloaded.epoch, epoch)
        self.assertEqual(reloaded.runtime["linked_server"], "et4003vpdsq1032")
        self.assertEqual(reloaded.phases[0].epoch, epoch)
        self.assertFalse(reloaded.phases[0].is_stale(epoch))
        self.assertTrue(reloaded.phases[0].is_stale("a-later-epoch"))

    def test_load_rejects_missing_file(self):
        with self.assertRaises(ManifestError):
            Manifest.load(self.tmp / "nope.yaml")

    def test_bare_yaml_nulls_load_as_none(self):
        # makeYaml writes empty values as bare `key:`; they must come back as
        # None rather than the string "None".
        dump_yaml(SAMPLE_MANIFEST, self.manifest_path)
        self.assertIn("started_at:", self.manifest_path.read_text(encoding="utf-8"))
        reloaded = Manifest.load(self.manifest_path)
        self.assertIsNone(reloaded.sessions[0].phases[0].data["started_at"])


class BusyFileTests(TempDirTestCase):
    """D153: a save the OS refuses, because a reader holds the file, waits."""

    def refuse(self, times):
        """os.replace refused `times` times, then done for real; sleeps recorded."""
        import os
        from unittest import mock

        from .. import yaml_io

        real = os.replace
        calls = {"n": 0}

        def replace(source, target):
            calls["n"] += 1
            if times is None or calls["n"] <= times:
                raise PermissionError(13, "Access is denied")
            return real(source, target)

        self.slept: list[float] = []
        self.said: list[str] = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(yaml_io.os, "replace", side_effect=replace).start()
        mock.patch.object(yaml_io.time, "sleep", side_effect=self.slept.append).start()
        mock.patch("builtins.print", side_effect=lambda *a, **k: self.said.append(" ".join(map(str, a)))).start()

    def test_a_save_refused_a_few_times_lands(self):
        manifest = sample_manifest()
        manifest.path = self.manifest_path
        manifest.save()
        manifest.sessions[0].phases[0].start()
        manifest.sessions[0].phases[0].finish(rows=42)
        self.refuse(3)
        manifest.save()
        from unittest import mock

        mock.patch.stopall()
        self.assertEqual(Manifest.load(self.manifest_path).sessions[0].phases[0].rows, 42)
        self.assertEqual(self.slept, [0.1, 0.25, 0.5])

    def test_after_the_quick_tries_it_waits_a_minute_at_a_time(self):
        manifest = sample_manifest()
        manifest.path = self.manifest_path
        self.refuse(7)
        manifest.save()
        self.assertEqual(self.slept, [0.1, 0.25, 0.5, 1.0, 2.0, 60.0, 60.0])
        self.assertTrue(any("trying again in 60s, 2 of 5" in line for line in self.said))

    def test_refused_for_five_minutes_it_stops_saying_why(self):
        from ..yaml_io import FileBusy

        manifest = sample_manifest()
        manifest.path = self.manifest_path
        self.refuse(None)
        with self.assertRaises(FileBusy) as caught:
            manifest.save()
        self.assertEqual(self.slept.count(60.0), 5)
        self.assertIn("Run window", str(caught.exception))
        self.assertIn("pullmanifest.yaml", str(caught.exception))
