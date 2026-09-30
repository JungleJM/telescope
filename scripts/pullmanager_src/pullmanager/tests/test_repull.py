"""Re-pulling chosen sessions (D158): a finished pull of a case, its control,
and a session that has nothing to do with them."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path

from .. import cli
from ..executor import UnknownSession, sessions_to_repull
from ..launcher import Options, _resume_flags
from ..manifest import Manifest
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST


def finished_pull(root: Path) -> Path:
    """UCblack (a case), UCwhite (its control, 4x) and Crohnsblack, all done."""
    data = copy.deepcopy(SAMPLE_MANIFEST)
    template = data["sessions"][0]
    sessions = []
    for name in ("UCblackPatients", "UCwhitePatients", "CrohnsblackPatients"):
        session = copy.deepcopy(template)
        session["session_id"] = session["cohort"] = session["pk_table"] = name
        session["status"] = "done"
        for key, phase in session["phases"].items():
            phase["yaml"] = f"sessions/{name}/{key}.yaml"
            phase["status"] = "done"
            phase["outputs"] = {"table_rows": {name: 10}} if key == "pk" else {}
        for run in session["runs"]:
            run["yaml"] = run["yaml"].replace("UCblackPatients", name)
            run["run_id"] = run["run_id"].replace("UCblackPatients", name)
            run["status"] = "done"
        cohort = {"name": name, "dest_table": name, "type": "PK"}
        if name == "UCwhitePatients":
            cohort["split_after_build"] = [{"role": "control", "row_mult": 4,
                                            "matched_to": "UCblackPatients"}]
        dump_yaml({"cohorts": [cohort]}, root / "sessions" / name / "pk.yaml")
        sessions.append(session)
    data["sessions"] = sessions
    data["uploads_landed"] = {"IBD_Meds": {"table": "upload_IBD_Meds", "rows": 715}}
    path = root / "pullmanifest.yaml"
    dump_yaml(data, path)
    return path


class RepullTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = finished_pull(Path(self._tmp.name))
        self.manifest = Manifest.load(self.path)

    def statuses(self, manifest):
        return {s.session_id: s.status for s in manifest.sessions}

    def test_a_control_alone_starts_over_alone(self):
        chosen, notes = sessions_to_repull(self.manifest, ["ucwhitepatients"])
        self.assertEqual([s.session_id for s in chosen], ["UCwhitePatients"])
        self.assertEqual(notes, [])

    def test_a_case_brings_its_control(self):
        chosen, notes = sessions_to_repull(self.manifest, ["UCblackPatients"])
        self.assertEqual([s.session_id for s in chosen], ["UCblackPatients", "UCwhitePatients"])
        self.assertIn("UCwhitePatients too", notes[0])

    def test_an_unknown_name_lists_the_sessions(self):
        with self.assertRaises(UnknownSession) as caught:
            sessions_to_repull(self.manifest, ["UCwhite"])
        self.assertIn("UCwhitePatients", str(caught.exception))

    def test_only_the_chosen_sessions_start_over(self):
        # IBD_Ancestry: the white sessions landed nothing and finished done.
        args = argparse.Namespace(repull_session=["UCwhitePatients"], repull=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.repull_chosen(self.manifest, args))
        self.manifest.save()
        reloaded = Manifest.load(self.path)
        self.assertEqual(self.statuses(reloaded), {
            "UCblackPatients": "done", "UCwhitePatients": "pending", "CrohnsblackPatients": "done"})
        white = reloaded.sessions[1]
        self.assertEqual(white.phases[2].outputs, {})
        self.assertIn("IBD_Meds", reloaded.uploads_landed, "uploads already landed stay (D61)")
        self.assertFalse(args.repull)

    def test_all_is_repull(self):
        args = argparse.Namespace(repull_session=["all"], repull=False)
        self.assertTrue(cli.repull_chosen(self.manifest, args))
        self.assertTrue(args.repull)

    def test_an_unknown_name_stops_before_anything_changes(self):
        args = argparse.Namespace(repull_session=["Nope"], repull=False)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(cli.repull_chosen(self.manifest, args))
        self.assertIn("No session named Nope", err.getvalue())
        self.assertTrue(all(s == "done" for s in self.statuses(self.manifest).values()))

    def test_run_passes_each_chosen_session(self):
        self.assertEqual(_resume_flags(Options(repull_sessions=("UCwhitePatients", "CrohnswhitePatients"))),
                         ["--repull-session", "UCwhitePatients", "--repull-session", "CrohnswhitePatients"])
        self.assertEqual(_resume_flags(Options(repull_sessions=("all",))), ["--repull"])
        self.assertEqual(_resume_flags(Options(repull=True, repull_sessions=("X",))), ["--repull"])


if __name__ == "__main__":
    unittest.main()
