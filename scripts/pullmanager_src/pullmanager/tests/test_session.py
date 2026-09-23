"""Session execution, against scripted fake connections.

There is no database reachable from the development machine, so the
connections are faked. What this proves is the orchestration: ordering, what a
failure blocks, what the manifest records, and that the epoch and instance name
are captured per connection.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from ..db import Settings
from ..manifest import Manifest
from ..session import LARGE_ROW_WARNING, SessionError, SessionRunner

FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
INSTANCE = "et4003vpdsql032"


class ScriptedCursor:
    """Answers by matching the SQL, so one fake serves the whole flow."""

    def __init__(self, owner):
        self.owner = owner
        self._sets: list = []
        self._current = None

    def execute(self, sql, params=None):
        self.owner.executed.append(sql)
        for pattern, action in self.owner.failures.items():
            if re.search(pattern, sql, re.I):
                raise RuntimeError(action)
        self._sets = list(self.owner.results_for(sql))
        self._advance()

    def executemany(self, sql, seq):
        self.owner.inserted.append((sql, list(seq)))

    def _advance(self):
        self._current = self._sets.pop(0) if self._sets else None

    @property
    def messages(self):
        return []

    @property
    def description(self):
        return None if self._current is None else [(c,) for c in self._current[0]]

    def fetchall(self):
        if self._current is None:
            raise RuntimeError("no rows")
        return list(self._current[1])

    def fetchone(self):
        if self._current is None:
            return None
        rows = self._current[1]
        return rows[0] if rows else None

    def nextset(self):
        if not self._sets:
            return False
        self._advance()
        return True

    fast_executemany = False


class FakeConnection:
    def __init__(self, side, *, rows=10, distinct=None, landed=None, failures=None):
        self.side = side
        self.rows = rows
        self.distinct = rows if distinct is None else distinct
        self.landed = rows if landed is None else landed
        self.failures = failures or {}
        self.executed: list[str] = []
        self.inserted: list = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return ScriptedCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    def results_for(self, sql):
        if "@@SERVERNAME" in sql:
            return [(["CosmosServerName"], [(INSTANCE,)])]
        if "COUNT_BIG(DISTINCT" in sql:
            return [(["total", "distinct"], [(self.rows, self.distinct)])]
        if "SELECT * FROM" in sql:
            return [(["PatientDurableKey", "Sex"], [(i, "Female") for i in range(3)])]
        sets = []
        for match in re.finditer(r"'([^']+)' AS \[DestTable\]", sql):
            dest = match.group(1)
            if "'cosmos' AS [Side]" in sql:
                sets.append((["DestTable", "Side", "RowCount"],
                             [(dest, "cosmos", self.rows)]))
                sets.append((["DestTable", "Side", "RowCount"],
                             [(dest, "projects", self.landed)]))
            else:
                sets.append((["CohortName", "DestTable", "RowCount"],
                             [(dest, dest, self.rows)]))
        return sets


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        if not FIXTURES.is_dir():
            self.skipTest(f"fixtures not found at {FIXTURES}")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "split"
        shutil.copytree(FIXTURES, self.root)
        (self.root / "fixtures").mkdir(exist_ok=True)
        (self.root / "fixtures" / "hospital_icd_codes.csv").write_text(
            "DiagnosisCode,Label\nK50.0,Crohn's\nK51.0,UC\n", encoding="utf-8"
        )
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")

    def declare_pk_key(self, column="PatientDurableKey"):
        """The fixture's PK declares no key_column; some checks need one."""
        from ..yaml_io import dump_yaml, load_yaml

        path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load_yaml(path)
        doc["cohorts"][0]["key_column"] = column
        dump_yaml(doc, path)

    def runner(self, **kwargs):
        cosmos = FakeConnection("cosmos", **kwargs.pop("cosmos", {}))
        projects = FakeConnection("projects", **kwargs.pop("projects", {}))
        self.cosmos, self.projects = cosmos, projects
        order = [cosmos, projects]

        def connect_fn(conn_str, **_):
            return order.pop(0)

        return SessionRunner(
            self.manifest,
            self.manifest.sessions[0],
            Settings(projects_server="PROJ", projects_database="PROJECTD33A929"),
            connect_fn=connect_fn,
            upload_root=self.root,
            **kwargs,
        )


class HappyPathTests(SessionTestCase):
    def test_runs_every_unit_and_records_it(self):
        with self.runner() as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(len(report.completed), 4)

    def test_captures_the_instance_name_per_connection(self):
        # It changes every connection, so it is never cached.
        with self.runner() as runner:
            runner.execute()
            self.assertEqual(runner.session.runtime["linked_server"], INSTANCE)
            self.assertTrue(runner.session.epoch)

    def test_manifest_is_saved_as_it_goes(self):
        with self.runner() as runner:
            runner.execute()
        reloaded = Manifest.load(self.root / "pullmanifest.yaml")
        session = reloaded.sessions[0]
        self.assertEqual(session.status, "done")
        self.assertTrue(all(p.status == "done" for p in session.phases))
        self.assertTrue(all(p.epoch for p in session.phases))

    def test_rows_are_recorded(self):
        with self.runner(cosmos={"rows": 4242}) as runner:
            runner.execute()
        pk = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[2]
        self.assertEqual(pk.rows, 4242)

    def test_csv_upload_is_bound_not_interpolated(self):
        with self.runner() as runner:
            runner.execute()
        statements = [sql for sql, _ in self.cosmos.inserted]
        self.assertTrue(any("VALUES (?, ?)" in s for s in statements))

    def test_connections_are_closed(self):
        runner = self.runner()
        with runner:
            runner.execute()
        self.assertTrue(self.cosmos.closed)
        self.assertTrue(self.projects.closed)


class FailureTests(SessionTestCase):
    def test_a_failed_phase_blocks_what_follows(self):
        # setup, uploads and PK are prerequisites.
        with self.runner(projects={"failures": {r"CREATE TABLE PROJECTD": "disk full"}}) as runner:
            report = runner.execute()
        self.assertFalse(report.ok)
        session = Manifest.load(self.root / "pullmanifest.yaml").sessions[0]
        self.assertEqual(session.phases[0].status, "failed")
        self.assertEqual(session.phases[1].status, "blocked")
        self.assertEqual(session.runs[0].status, "blocked")

    def test_failure_detail_is_kept(self):
        with self.runner(projects={"failures": {r"CREATE TABLE PROJECTD": "disk full"}}) as runner:
            runner.execute()
        phase = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[0]
        self.assertIn("disk full", phase.error["message"])

    def test_a_pk_without_a_key_column_warns_instead_of_checking(self):
        # Nothing to order by means chunk stability cannot be verified.
        with self.runner() as runner:
            report = runner.execute()
        self.assertTrue(any("key_column" in w for w in report.warnings))

    def test_a_non_unique_pk_is_refused(self):
        # Chunking orders by the key; duplicates make a chunk mean different
        # rows each run.
        self.declare_pk_key()
        with self.runner(projects={"rows": 100, "distinct": 90}) as runner:
            report = runner.execute()
        self.assertFalse(report.ok)
        self.assertTrue(any("distinct" in message for _, message in report.failed))

    def test_row_count_mismatch_warns(self):
        with self.runner(cosmos={"rows": 1000}, projects={"rows": 1000, "landed": 998}) as runner:
            report = runner.execute()
        self.assertTrue(any("did not carry everything" in w for w in report.warnings))

    def test_very_large_pull_warns(self):
        big = LARGE_ROW_WARNING + 1
        with self.runner(cosmos={"rows": big}, projects={"rows": big, "landed": big}) as runner:
            report = runner.execute()
        self.assertTrue(any("warning threshold" in w for w in report.warnings))


class ResumeTests(SessionTestCase):
    def test_a_second_run_replays_stale_server_work(self):
        with self.runner() as runner:
            runner.execute()
        first_epoch = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].epoch

        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        with self.runner() as runner:
            report = runner.execute()
        # A new connection means the global temps are gone, so the phases run
        # again even though their status said done.
        self.assertIn("Patients/setup", report.completed)
        self.assertIn("Patients/pk", report.completed)
        self.assertNotEqual(self.manifest.sessions[0].epoch, first_epoch)

    def test_partial_resume_keeps_completed_runs(self):
        with self.runner() as runner:
            runner.execute()
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        with self.runner(mode="partial") as runner:
            report = runner.execute()
        # The run's rows are in a Projects table and survive the lost connection.
        self.assertTrue(any("survive" in s for s in report.skipped))
