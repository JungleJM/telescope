"""The run scan (D152): a finished pull, then what it left behind changed."""

from __future__ import annotations

import unittest

from ..manifest import Manifest
from ..scan import scan_manifest, scan_runs
from .test_session import SessionTestCase


class ScanTests(SessionTestCase):
    def pulled(self):
        """A clean pull whose Cosmos and Projects counts agree: 3 rows each."""
        with self.runner(cosmos={"rows": 3}, projects={"rows": 3, "pk_rows": 3}) as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        return Manifest.load(self.root / "pullmanifest.yaml")

    def package(self, manifest, rows=3):
        """Parquets for every finished table, `rows` each, where Artifacts puts them."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        from ..artifacts import file_name, parquets_folder, plan

        for spec in plan(manifest).tables:
            if spec.kind not in ("pk", "run", "kept"):  # kept: released as the pull ran (D177, D206)
                continue
            for part in spec.parts:
                path = parquets_folder(manifest.path) / spec.folder / file_name(spec.dest, part.label)
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(pa.table({"k": list(range(rows))}), path)
        (parquets_folder(manifest.path) / "contents.md").write_text("packaged\n", encoding="utf-8")

    def test_a_pull_that_landed_everything_checks_out(self):
        manifest = self.pulled()
        self.package(manifest)
        self.assertEqual(scan_manifest(manifest), {})

    def test_parquets_with_fewer_rows_than_built_are_lost_rows(self):
        manifest = self.pulled()
        self.package(manifest, rows=2)
        lost = scan_manifest(manifest)["lost_rows"]
        self.assertIn("Patients", lost)
        self.assertIn("built 3, parquets 2", lost["Patients"])

    def test_the_white_controls_are_empty_and_short(self):
        # IBD_Ancestry: Cosmos built them, nothing landed, the sample kept 0.
        manifest = self.pulled()
        pk = manifest.sessions[0].phases[2]
        pk.outputs["table_rows"] = {"Patients": 0}
        pk.outputs["cosmos_rows"] = 1_232_900
        pk.outputs["control_sample"] = {"matched_to": "Cases", "row_mult": 4,
                                        "per_batch": {"b1of3-Female": {"cases": 88_776, "controls": 0}}}
        manifest.save()
        found = scan_manifest(Manifest.load(self.root / "pullmanifest.yaml"))
        self.assertIn("Patients", found["empty"])
        self.assertEqual(found["short_controls"]["Patients"],
                         ["b1of3-Female kept 0 for 88,776 cases (4x)"])

    def test_a_pk_that_landed_fewer_than_cosmos_built_is_lost_rows(self):
        manifest = self.pulled()
        pk = manifest.sessions[0].phases[2]
        pk.outputs["cosmos_rows"] = 400_000
        manifest.save()
        found = scan_manifest(Manifest.load(self.root / "pullmanifest.yaml"))
        self.assertIn("Cosmos built 400,000, 3 landed", found["lost_rows"]["Patients"])

    def test_a_run_that_recorded_no_count_is_said(self):
        manifest = self.pulled()
        run = manifest.sessions[0].runs[0]
        run.outputs["table_rows"] = {}
        manifest.save()
        found = scan_manifest(Manifest.load(self.root / "pullmanifest.yaml"))
        self.assertTrue(any(item.startswith("OtherHospitalizations (") for item in found["no_count"]))

    def test_an_unpackaged_pull_is_said_once(self):
        self.assertIn("not_packaged", scan_manifest(self.pulled()))

    def test_the_report_holds_only_what_did_not_check_out(self):
        import shutil

        manifest = self.pulled()
        self.package(manifest, rows=2)
        home = self.root.parent / "home"
        folder = home / "runs" / "IBD"
        shutil.copytree(self.root, folder)
        path, text, count = scan_runs(home)
        self.assertEqual(path, home / "runs" / "run_scan.yaml")
        self.assertTrue(text.startswith("# run scan, "))
        self.assertIn("IBD:\n  lost_rows:\n", text)
        self.assertGreater(count, 0)
        from ..yaml_io import load_yaml

        self.assertIn("lost_rows", load_yaml(path)["IBD"])


if __name__ == "__main__":
    unittest.main()
