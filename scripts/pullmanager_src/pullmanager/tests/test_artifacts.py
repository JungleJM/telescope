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
        if match.group(2) in self.db.fail:
            raise RuntimeError(f"[42000] Invalid column name in {match.group(2)}")
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
        self.fail: set[str] = set()  # tables whose SELECT fails, as a driver error would
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
        self.split = self.work / "runs" / "IBD_Ancestry"
        shutil.copytree(FIXTURES, self.split)
        self.out = self.work / "runs" / "IBD_Ancestry"  # the run folder: parquet folders at its top (D142)

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
        patients_table = self.read("cosmos_parquets/Patients.parquet")
        self.assertEqual(patients_table.column("PatientDurableKey").to_pylist(),
                         [r["PatientDurableKey"] for r in patients()])
        self.assertEqual(str(patients_table.schema.field("PatientDurableKey").type), "int64")
        self.assertEqual(str(patients_table.schema.field("BirthDate").type), "date32[day]")
        facts = self.read("cosmos_parquets/OtherHospitalizations.parquet")
        self.assertNotIn("_batch", facts.column_names)
        self.assertEqual(facts.num_rows, 2)
        self.assertEqual(facts.column("LengthOfStayInDays").to_pylist(), [None, 1])
        self.assertEqual(str(facts.schema.field("InpatientAdmissionInstant").type), "timestamp[us]")
        spec = next(s for s in result.tables if s.dest == "OtherHospitalizations")
        self.assertEqual([c for c, _ in spec.columns][-1], "LengthOfStayInDays")
        self.assertEqual(dict(spec.columns)["InpatientEncounterKey"], "BIGINT")

    def test_a_prefixed_pull_is_read_from_its_prefixed_tables_into_plain_parquets(self):
        # D163: in Projects as manval_Patients; the parquet stays Patients.parquet.
        self.set_status()
        for path in (self.split / "sessions").rglob("*.yaml"):
            doc = load_yaml(path)
            doc["table_prefix"] = "manval"
            dump_yaml(doc, path)
        db = FakeProjects(
            {"manval_Patients": PATIENTS, "manval_OtherHospitalizations": HOSPITALIZATIONS},
            {"manval_Patients": patients(), "manval_OtherHospitalizations": hospitalizations()},
        )
        result = self.package(db)
        self.assertEqual(result.left_out, [])
        self.assertEqual(self.read("cosmos_parquets/Patients.parquet").num_rows, 3)
        self.assertEqual(self.read("cosmos_parquets/OtherHospitalizations.parquet").num_rows, 2)

    def test_uploads_are_copied_from_the_split(self):
        self.set_status()
        self.package()
        copied = self.out / "uploads_parquets" / "HospitalICDCodes.parquet"
        self.assertEqual(copied.read_bytes(),
                         (self.split / "uploads" / "hospital_icd_codes.parquet").read_bytes())

    def test_unfinished_tables_are_left_out_saying_why(self):
        # The manifest decides, not what exists in Projects.
        self.set_status(phases="done", runs="failed")
        result = self.package()
        self.assertTrue((self.out / "cosmos_parquets" / "Patients.parquet").is_file())
        self.assertFalse((self.out / "cosmos_parquets" / "OtherHospitalizations.parquet").exists())
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
        self.assertEqual(self.read("cosmos_parquets/Patients.parquet").num_rows, 25)

    def test_each_packaging_replaces_the_last(self):
        self.set_status()
        stale = self.out / "cosmos_parquets" / "Dropped.parquet"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old")
        self.package()
        self.assertFalse(stale.exists())
        self.assertTrue((self.out / "cosmos_parquets" / "Patients.parquet").is_file())

    def test_packaging_leaves_the_rest_of_the_run_folder_alone(self):
        # D142: the parquet folders sit in the run folder, beside the manifest,
        # the logs and pull_files/. Only the parquet folders are replaced.
        self.set_status()
        kept = [self.out / "pullmanifest.yaml", self.out / "execute-20260929-081215.log",
                self.out / "older_logs" / "execute-20260928-120000.log",
                self.out / "pull_files" / "split" / "uploads" / "codes.parquet",
                self.out / "pull_files" / "sql" / "P" / "a.sql"]
        for path in kept[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep", encoding="utf-8")
        self.package()
        self.package()
        for path in kept:
            self.assertTrue(path.is_file(), path)


class SeparateTests(ArtifactTestCase):
    """A dimension marked separate_parquets gives one file per value."""

    def test_each_value_gets_its_own_files(self):
        self.set_status()
        self.make_batched(separate=True)
        self.package(self.projects(labels=("b1of2-Female", "b2of2-Male")))
        female = self.read("cosmos_parquets/OtherHospitalizations_Female.parquet")
        self.assertEqual(female.num_rows, 2)
        self.assertEqual(self.read("cosmos_parquets/OtherHospitalizations_Male.parquet").num_rows, 2)
        self.assertFalse((self.out / "cosmos_parquets" / "OtherHospitalizations.parquet").exists())
        # The PK has no _batch: it is split on its own Sex column.
        self.assertEqual(self.read("cosmos_parquets/Patients_Female.parquet").column("Sex").to_pylist(),
                         ["Female", "Female"])
        self.assertEqual(self.read("cosmos_parquets/Patients_Male.parquet").num_rows, 1)

    def test_without_the_flag_the_batches_stay_together(self):
        self.set_status()
        self.make_batched(separate=False)
        self.package(self.projects(labels=("b1of2-Female", "b2of2-Male")))
        self.assertEqual(self.read("cosmos_parquets/OtherHospitalizations.parquet").num_rows, 4)

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
        self.assertTrue((self.out / "sneakpeek_parquets" / "Patients.parquet").is_file())
        self.assertTrue((self.out / "sneakpeek_parquets" / "OtherHospitalizations.parquet").is_file())
        self.assertFalse((self.out / "cosmos_parquets").exists())


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
        self.assertIn("2 table(s) in 2 parquet file(s)", out)
        self.assertIn("1 left out, 0 failed", out)
        self.assertTrue(db.closed)

    def test_it_says_when_each_file_starts_and_how_it_went(self):
        self.set_status()
        code, out = self.run_artifacts(self.projects())
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        start = lines.index("  writing  cosmos_parquets/Patients.parquet ...")
        self.assertRegex(
            lines[start + 1],
            r"^  wrote    cosmos_parquets/Patients\.parquet  \(3 rows, [\d.,]+ (bytes|KB), \d+\.\ds\)$",
        )
        listed = lines[next(i for i, l in enumerate(lines) if l.startswith("Files written, in")) + 1:]
        self.assertTrue(any(l.startswith("  cosmos_parquets/Patients.parquet  3 rows") for l in listed), out)
        self.assertIn("  contents.md", listed)
        self.assertRegex(out, r"Artifacts finished in \d+\.\ds: 3 table\(s\) in 3 parquet file\(s\), 7 rows")

    def test_a_table_that_fails_is_reported_and_the_rest_are_packaged(self):
        self.set_status()
        db = self.projects()
        db.fail.add("Patients")
        code, out = self.run_artifacts(db)
        self.assertEqual(code, 1, out)
        self.assertIn("  FAILED   Patients: RuntimeError: [42000] Invalid column name in Patients", out)
        self.assertFalse((self.out / "cosmos_parquets" / "Patients.parquet").exists())
        self.assertFalse(list(self.out.rglob("*.tmp")))
        self.assertTrue((self.out / "cosmos_parquets" / "OtherHospitalizations.parquet").is_file())
        self.assertIn("Failed (not packaged; the rest were):\n  Patients:", out)
        self.assertIn("1 failed", out)
        contents = (self.split / "contents.md").read_text(encoding="utf-8")
        self.assertIn("OtherHospitalizations", contents)

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
        self.assertFalse((self.out / "cosmos_parquets").exists())


class AfterPullTests(ArtifactTestCase):
    """D141: a clean pull packages itself; any other is left for Artifacts."""

    def execute(self, db, *, code=0, runs="done"):
        from unittest import mock

        def pulled(manifest, args, connect_fn, done):
            # The pull, as the manifest records it; Execute then decides.
            for session in manifest.sessions:
                for node in [*session.phases, *session.runs]:
                    node.status = "done" if node in session.phases else runs
                    done.append(node.label)
            manifest.save()
            return code

        args = argparse.Namespace(env=None, repull=False, retry_failed=False)
        out = io.StringIO()
        with mock.patch.object(cli, "_execute", pulled), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = cli.execute(Manifest.load(self.split / "pullmanifest.yaml"), args,
                                 connect_fn=lambda *a, **k: db)
        return result, out.getvalue()

    def test_a_clean_pull_is_packaged_by_execute(self):
        # Before, only the PK's parquet existed until Artifacts was run by hand.
        code, out = self.execute(self.projects())
        self.assertEqual(code, 0, out)
        self.assertTrue((self.out / "cosmos_parquets" / "OtherHospitalizations.parquet").is_file(), out)
        self.assertTrue((self.split / "contents.md").is_file())
        self.assertIn("packaging it (Artifacts)", out)

    def test_a_pull_with_a_failure_is_not_packaged(self):
        db = self.projects()
        code, out = self.execute(db, code=1, runs="failed")
        self.assertEqual(code, 1, out)
        self.assertIn("Not packaged", out)
        self.assertEqual(db.executed, [])
        self.assertFalse((self.out / "cosmos_parquets").exists())

    def test_packaging_that_fails_leaves_the_exit_code_the_pulls(self):
        db = self.projects()
        db.fail.add("Patients")
        code, out = self.execute(db)
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING Artifacts did not write every table", out)
        self.assertTrue((self.out / "cosmos_parquets" / "OtherHospitalizations.parquet").is_file())


class LoaderTests(ArtifactTestCase):
    """D75, D89: the files written beside contents.md open what was packaged."""

    def write(self):
        from ..loaders import write_loaders

        self.set_status()
        self.package()
        return write_loaders(self.out, self.out, "IBD_Ancestry")

    def run_script(self, name):
        import subprocess
        import sys

        return subprocess.run([sys.executable, str(self.out / name)], capture_output=True,
                              text=True, timeout=120, cwd=str(self.work))

    def test_the_load_scripts_viewer_and_how_to_are_written_at_the_run_folders_root(self):
        written = self.write()
        self.assertEqual(sorted(p.relative_to(self.out).as_posix() for p in written), [
            "HOW_TO.md", "load_parquets.R", "load_parquets.py",
            "utils.py", "utils/client/transcription_viewer.py", "utils/client/viewparquets.py",
        ])
        self.assertIn(self.out.resolve().as_posix(), (self.out / "load_parquets.R").read_text())
        self.assertFalse(list(self.out.glob("examine_parquets.*")))

    def test_the_viewer_is_the_stock_copy(self):
        from ..loaders import STOCK_DIR

        from ..loaders import stamp_bundle

        self.write()
        self.assertEqual((self.out / "utils" / "client" / "viewparquets.py").read_bytes(),
                         stamp_bundle((STOCK_DIR.parent / "utils" / "client" / "viewparquets.py").read_bytes()))

    def test_the_pull_folder_gets_a_utilities_window_with_the_client_tools_only(self):
        # D148: the user's own window, with only what the client is meant to have.
        import importlib.util

        old = self.out / "viewparquets.py"  # where an earlier Artifacts put it
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("# old\n", encoding="utf-8")
        self.write()
        self.assertFalse(old.exists())
        spec = importlib.util.spec_from_file_location("client_utilities", self.out / "utils.py")
        window = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(window)
        self.assertEqual([(heading, [p.name for p in paths]) for heading, paths in window.sections()],
                         [("Client", ["transcription_viewer.py", "viewparquets.py"])])

    def test_the_viewer_copy_says_the_bundle_it_was_packaged_with(self):
        # D147: in a pull's folder there is no bundle above it to read.
        import importlib.util
        from unittest import mock

        from .. import config

        with mock.patch.object(config, "bundle_id", return_value="ca0fa906"):
            self.write()
        copy = self.out / "utils" / "client" / "viewparquets.py"
        self.assertIn('\nBUNDLE = "ca0fa906"', copy.read_text(encoding="utf-8"))
        self.assertIn('\nBUNDLE = "ca0fa906"', (self.out / "utils.py").read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location("packaged_viewer", copy)
        viewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viewer)
        self.assertEqual(viewer.credit_text(), "Designed and built by Jason Mathias \u00b7 bundle ca0fa906")

    def use_stock(self, listing: str, files: dict[str, str]):
        """A stock folder in the scratch space, with `files` beside it by path."""
        from .. import loaders

        stock = self.work / "stock"
        stock.mkdir(exist_ok=True)
        (stock / "stock.yaml").write_text(listing, encoding="utf-8")
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        original = loaders.STOCK_DIR
        loaders.STOCK_DIR = stock
        self.addCleanup(setattr, loaders, "STOCK_DIR", original)

    def test_what_stock_yaml_lists_is_copied_where_it_says(self):
        # D124: a script added to the list reaches every pull.
        self.use_stock("files:\n  - ../utils/viewparquets.py\n  - file: ../utils/tables.R\n    into: R\n",
                       {"utils/viewparquets.py": "# viewer\n", "utils/tables.R": "# R\n"})
        written = self.write()
        self.assertEqual((self.out / "R" / "tables.R").read_text(encoding="utf-8"), "# R\n")
        self.assertEqual((self.out / "viewparquets.py").read_text(encoding="utf-8"), "# viewer\n")
        self.assertIn(self.out / "R" / "tables.R", written)

    def test_an_entry_that_is_not_there_is_said_and_the_rest_copied(self):
        import contextlib
        import io

        self.use_stock("files: [gone.R, ../utils/viewparquets.py]\n", {"utils/viewparquets.py": "# viewer\n"})
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.write()
        self.assertIn("gone.R", said.getvalue())
        self.assertTrue((self.out / "viewparquets.py").is_file())

    def test_how_to_comes_from_the_stock_file_with_the_pull_filled_in(self):
        from .. import loaders

        stock = self.work / "stock"
        stock.mkdir()
        (stock / "stock.yaml").write_text("files: [HOW_TO.md, viewparquets.py]\n", encoding="utf-8")
        (stock / "viewparquets.py").write_text("# viewer\n", encoding="utf-8")
        (stock / "HOW_TO.md").write_text(
            "<!-- stock HOW_TO.md: a note for the editor -->\n# {project}\n"
            "Files in {parquets}. Braces {like these} stay.\n",
            encoding="utf-8",
        )
        original = loaders.STOCK_DIR
        loaders.STOCK_DIR = stock
        self.addCleanup(setattr, loaders, "STOCK_DIR", original)
        self.write()
        text = (self.out / "HOW_TO.md").read_text(encoding="utf-8")
        self.assertEqual(
            text, f"# IBD_Ancestry\nFiles in {self.out.resolve().as_posix()}. Braces {{like these}} stay.\n"
        )

    def test_the_python_load_script_opens_every_table_by_name(self):
        self.write()
        # An upload as sent, in pull_files/: not a table of the pull (D142).
        sent = self.out / "pull_files" / "split" / "uploads" / "Sent.parquet"
        sent.parent.mkdir(parents=True)
        shutil.copyfile(self.out / "uploads_parquets" / "HospitalICDCodes.parquet", sent)
        done = self.run_script("load_parquets.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Opened 3 table(s): OtherHospitalizations, Patients, HospitalICDCodes", done.stdout)

    def test_the_r_scripts_keep_64_bit_keys_as_integer64(self):
        import shutil
        import subprocess

        rscript = shutil.which("Rscript")
        if not rscript:
            self.skipTest("no Rscript here")
        has_arrow = subprocess.run([rscript, "-e", 'quit(status = !requireNamespace("arrow", quietly = TRUE))'],
                                   capture_output=True, timeout=120)
        if has_arrow.returncode:
            self.skipTest("this R has no arrow package")
        self.write()
        for name, get in (("load_parquets.R", "dplyr::collect(Patients)"),):
            with self.subTest(script=name):
                done = subprocess.run(
                    [rscript, "-e", f'source("{name}"); cat(class({get}$PatientDurableKey))'],
                    capture_output=True, text=True, timeout=180, cwd=str(self.out),
                )
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(done.stdout.endswith("integer64"), done.stdout)

    def test_the_r_scripts_parse(self):
        import shutil
        import subprocess

        rscript = shutil.which("Rscript")
        if not rscript:
            self.skipTest("no Rscript here")
        self.write()
        for name in ("load_parquets.R",):
            with self.subTest(script=name):
                path = (self.out / name).as_posix()
                done = subprocess.run([rscript, "-e", f'invisible(parse("{path}"))'],
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)

