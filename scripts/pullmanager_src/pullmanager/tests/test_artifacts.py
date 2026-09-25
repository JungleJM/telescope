"""`--artifacts`: a pull's finished tables as parquets (D72)."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from .. import artifacts, cli
from ..lock import lock_path
from ..manifest import Manifest
from ..yaml_io import dump_yaml, load_yaml

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"
DB = "PROJECTD33A929"

# INFORMATION_SCHEMA rows: name, type, length, precision, scale, datetime precision.
PATIENTS = [
    ("PatientDurableKey", "bigint", None, 19, 0, None),
    ("Sex", "varchar", 50, None, None, None),
    ("BirthDate", "date", None, None, None, 0),
]
HOSPITALIZATIONS = [
    ("InpatientEncounterKey", "bigint", None, 19, 0, None),
    ("PatientDurableKey", "bigint", None, 19, 0, None),
    ("InpatientAdmissionInstant", "datetime2", None, None, None, 7),
    ("LengthOfStayInDays", "int", None, 10, 0, None),
    ("_batch", "nvarchar", 200, None, None, None),
]


def pyarrow_or_skip(case):
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        case.skipTest("packaging needs pyarrow")


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        params = list(params or [])
        self.db.executed.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            self._rows = list(self.db.columns.get(params[0], []))
            return
        match = re.match(r"SELECT (.+) FROM \S+\.dbo\.(\w+)(?: WHERE (.+))?;$", sql, re.S)
        assert match, sql
        names = re.findall(r"\[(\w+)\] AS|\[(\w+)\]", match.group(1))
        names = [a or b for a, b in names]
        rows = self.db.rows[match.group(2)]
        where = match.group(3) or ""
        if "[_batch] IN" in where:
            rows = [r for r in rows if r["_batch"] in params]
        for column in re.findall(r"\[(\w+)\] = \?", where):
            value = params.pop(0)
            rows = [r for r in rows if r[column] == value]
        self._rows = [tuple(r[n] for n in names) for r in rows]

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, size):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows


class FakeProjects:
    def __init__(self, columns, rows):
        self.columns, self.rows = columns, rows
        self.executed: list = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def patients(n=3):
    return [{"PatientDurableKey": 10_000_000_000 + i, "Sex": ("Female", "Male")[i % 2],
             "BirthDate": dt.date(1980, 1, 1 + i)} for i in range(n)]


def hospitalizations(labels=("all",)):
    rows = []
    for i, label in enumerate(labels * 2):
        rows.append({"InpatientEncounterKey": 500 + i, "PatientDurableKey": 10_000_000_000 + i,
                     "InpatientAdmissionInstant": dt.datetime(2021, 5, 1, 8, 30, i),
                     "LengthOfStayInDays": None if i == 0 else i, "_batch": label})
    return rows


class ArtifactTestCase(unittest.TestCase):
    def setUp(self):
        if not FIXTURES.is_dir():
            self.skipTest(f"fixtures not found at {FIXTURES}")
        pyarrow_or_skip(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name).resolve()
        self.split = self.work / "runs" / "IBD_Ancestry" / "split"
        shutil.copytree(FIXTURES, self.split)
        self.out = self.work / "runs" / "IBD_Ancestry" / "parquets"

    def set_status(self, phases="done", runs="done"):
        data = load_yaml(self.split / "pullmanifest.yaml")
        for session in data["sessions"]:
            for node in session["phases"].values():
                node["status"] = phases
            for node in session["runs"]:
                node["status"] = runs
        dump_yaml(data, self.split / "pullmanifest.yaml")

    def make_batched(self, separate=True):
        """Two runs, Female and Male, on a sex dimension."""
        runs_dir = self.split / "sessions" / "Patients" / "runs"
        doc = load_yaml(runs_dir / "run.yaml")
        runs = []
        for i, value in enumerate(("Female", "Male"), start=1):
            dim = {"name": "sex", "kind": "column_values", "column": "Sex", "value": value}
            if separate:
                dim["separate"] = True
            batch = {"name": f"b{i}of2-{value}", "dimensions": [dim], "runtime": []}
            doc["pull_context"]["batch"] = batch
            dump_yaml(doc, runs_dir / f"{value}.yaml")
            runs.append({"run_id": f"Patients__{value}", "yaml": f"sessions/Patients/runs/{value}.yaml",
                         "status": "done", "batch": batch})
        data = load_yaml(self.split / "pullmanifest.yaml")
        data["sessions"][0]["runs"] = runs
        dump_yaml(data, self.split / "pullmanifest.yaml")

    def projects(self, labels=("all",)):
        return FakeProjects(
            {"Patients": PATIENTS, "OtherHospitalizations": HOSPITALIZATIONS},
            {"Patients": patients(), "OtherHospitalizations": hospitalizations(labels)},
        )

    def package(self, db=None):
        manifest = Manifest.load(self.split / "pullmanifest.yaml")
        return artifacts.package(manifest, db or self.projects(), self.out, log=lambda _: None)

    def read(self, relative):
        import pyarrow.parquet as pq

        return pq.read_table(self.out / relative)


class PackageTests(ArtifactTestCase):
    def test_every_finished_table_lands_with_its_types_and_no_batch_column(self):
        self.set_status()
        result = self.package()
        self.assertEqual(result.left_out, [])
        patients_table = self.read("Cosmos/Patients.parquet")
        self.assertEqual(patients_table.column("PatientDurableKey").to_pylist(),
                         [r["PatientDurableKey"] for r in patients()])
        self.assertEqual(str(patients_table.schema.field("PatientDurableKey").type), "int64")
        self.assertEqual(str(patients_table.schema.field("BirthDate").type), "date32[day]")
        facts = self.read("Cosmos/OtherHospitalizations.parquet")
        self.assertNotIn("_batch", facts.column_names)
        self.assertEqual(facts.num_rows, 2)
        self.assertEqual(facts.column("LengthOfStayInDays").to_pylist(), [None, 1])
        self.assertEqual(str(facts.schema.field("InpatientAdmissionInstant").type), "timestamp[us]")
        spec = next(s for s in result.tables if s.dest == "OtherHospitalizations")
        self.assertEqual([c for c, _ in spec.columns][-1], "LengthOfStayInDays")
        self.assertEqual(dict(spec.columns)["InpatientEncounterKey"], "BIGINT")

    def test_uploads_are_copied_from_the_split(self):
        self.set_status()
        self.package()
        copied = self.out / "uploads" / "HospitalICDCodes.parquet"
        self.assertEqual(copied.read_bytes(),
                         (self.split / "uploads" / "hospital_icd_codes.parquet").read_bytes())

    def test_unfinished_tables_are_left_out_saying_why(self):
        # The manifest decides, not what exists in Projects.
        self.set_status(phases="done", runs="failed")
        result = self.package()
        self.assertTrue((self.out / "Cosmos" / "Patients.parquet").is_file())
        self.assertFalse((self.out / "Cosmos" / "OtherHospitalizations.parquet").exists())
        why = dict(result.left_out)["OtherHospitalizations"]
        self.assertIn("1 of its 1 run(s) are not done (failed)", why)

    def test_a_pk_phase_not_done_leaves_the_pk_out(self):
        data = load_yaml(self.split / "pullmanifest.yaml")
        data["sessions"][0]["phases"]["pk"]["status"] = "running"
        dump_yaml(data, self.split / "pullmanifest.yaml")
        result = self.package()
        self.assertIn("Patients", dict(result.left_out))

    def test_a_large_table_is_read_in_chunks(self):
        self.set_status()
        db = self.projects()
        db.rows["Patients"] = patients(25)
        with mock.patch.object(artifacts, "FETCH_ROWS", 10):
            self.package(db)
        self.assertEqual(self.read("Cosmos/Patients.parquet").num_rows, 25)

    def test_each_packaging_replaces_the_last(self):
        self.set_status()
        stale = self.out / "Cosmos" / "Dropped.parquet"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old")
        self.package()
        self.assertFalse(stale.exists())
        self.assertTrue((self.out / "Cosmos" / "Patients.parquet").is_file())


class SeparateTests(ArtifactTestCase):
    """A dimension marked separate_parquets gives one file per value."""

    def test_each_value_gets_its_own_files(self):
        self.set_status()
        self.make_batched(separate=True)
        self.package(self.projects(labels=("b1of2-Female", "b2of2-Male")))
        female = self.read("Cosmos/OtherHospitalizations_Female.parquet")
        self.assertEqual(female.num_rows, 2)
        self.assertEqual(self.read("Cosmos/OtherHospitalizations_Male.parquet").num_rows, 2)
        self.assertFalse((self.out / "Cosmos" / "OtherHospitalizations.parquet").exists())
        # The PK has no _batch: it is split on its own Sex column.
        self.assertEqual(self.read("Cosmos/Patients_Female.parquet").column("Sex").to_pylist(),
                         ["Female", "Female"])
        self.assertEqual(self.read("Cosmos/Patients_Male.parquet").num_rows, 1)

    def test_without_the_flag_the_batches_stay_together(self):
        self.set_status()
        self.make_batched(separate=False)
        self.package(self.projects(labels=("b1of2-Female", "b2of2-Male")))
        self.assertEqual(self.read("Cosmos/OtherHospitalizations.parquet").num_rows, 4)

    def test_a_sneakpeek_table_keeps_its_suffix_last(self):
        self.assertEqual(artifacts.file_name("OtherDiagnoses_sp", "LA"), "OtherDiagnoses_LA_sp.parquet")
        self.assertEqual(artifacts.file_name("OtherDiagnoses", "LA"), "OtherDiagnoses_LA.parquet")


class SneakPeekFolderTests(ArtifactTestCase):
    def test_sneakpeek_tables_go_to_their_own_folder(self):
        self.set_status()
        for rel in ("sessions/Patients/pk.yaml", "sessions/Patients/runs/run.yaml"):
            doc = load_yaml(self.split / rel)
            for cohort in doc["cohorts"]:
                cohort["cosmos_db"] = "COSMOS_SneakPeek"
            dump_yaml(doc, self.split / rel)
        self.package()
        self.assertTrue((self.out / "SneakPeek" / "Patients.parquet").is_file())
        self.assertTrue((self.out / "SneakPeek" / "OtherHospitalizations.parquet").is_file())
        self.assertFalse((self.out / "Cosmos").exists())


class CommandTests(ArtifactTestCase):
    def run_artifacts(self, db):
        args = argparse.Namespace(env=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.artifacts(Manifest.load(self.split / "pullmanifest.yaml"), args,
                                 connect_fn=lambda *a, **k: db)
        return code, out.getvalue()

    def test_it_says_what_it_wrote_and_left_out(self):
        self.set_status(runs="failed")
        db = self.projects()
        code, out = self.run_artifacts(db)
        self.assertEqual(code, 0, out)
        self.assertIn("left out OtherHospitalizations", out)
        self.assertIn("Artifacts finished: 2 table(s)", out)
        self.assertTrue(db.closed)

    def test_a_pull_still_executing_is_not_packaged(self):
        self.set_status()
        now = time.time()
        lock_path(self.split / "pullmanifest.yaml").write_text(json.dumps(
            {"pid": 4242, "machine": "VM", "started": now, "heartbeat": now}), encoding="utf-8")
        db = self.projects()
        code, out = self.run_artifacts(db)
        self.assertEqual(code, 1)
        self.assertIn("already executing", out)
        self.assertEqual(db.executed, [])
        self.assertFalse(self.out.exists())
