"""Session execution, against scripted fake connections.

There is no database reachable from the development machine, so the
connections are faked. What this proves is the orchestration: ordering, what a
failure blocks, what the manifest records, and that the epoch and instance name
are captured per connection.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from .. import cli
from ..db import Settings
from ..manifest import Manifest
from ..session import LARGE_ROW_WARNING, SessionRunner
from ..yaml_io import dump_yaml, load_yaml

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"
INSTANCE = "et4003vpdsql032"
LAST_REFRESH = datetime(2026, 9, 17, 19, 34, 56, 450000)
NEXT_REFRESH = datetime(2026, 10, 15, 19, 30, 2, 100000)
DEST = "PROJECTD33A929.dbo.OtherHospitalizations"
PK_DEST = "PROJECTD33A929.dbo.Patients"


class ScriptedCursor:
    """Answers by matching the SQL, so one fake serves the whole flow."""

    def __init__(self, owner):
        self.owner = owner
        self._sets: list = []
        self._current = None

    def execute(self, sql, params=None):
        self.owner.executed.append(sql)
        self.owner.apply(sql)
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
    """A connection whose Projects side remembers what landed where.

    `tables` models the durable Projects database: each destination's rows,
    counted per `_batch` label. It is shared between the connections of
    successive executions, the way the real database outlives them, so a test
    can check the outcome of a failure and a retry rather than the SQL sent.
    Statements apply in order and a failure stops at the one it matches, so a
    run can fail after landing some rows. Nothing is undone by a rollback:
    the worst case, rows that survived a failed run.
    """

    def __init__(self, side, *, rows=10, distinct=None, landed=None, failures=None,
                 fail_once=None, fail_nth=None, tables=None, created=LAST_REFRESH,
                 pk_rows=3, existing_temps=(), transactional=False):
        self.side = side
        self.rows = rows
        self.distinct = rows if distinct is None else distinct
        self.landed = landed
        self.failures = failures or {}
        self.fail_once = fail_once if fail_once is not None else {}
        self.tables = tables if tables is not None else {}
        self.created = created
        self.pk_rows = pk_rows
        # Transactional: changes apply to a working copy that a commit makes
        # durable and a rollback discards. Off by default: the worst case.
        self.transactional = transactional
        self._pending = None
        # Global temps another pull holds on this instance, for D50's check.
        self.existing_temps = {name.lower() for name in existing_temps}
        # pattern -> [matches left before failing, message]
        self.fail_nth = {k: list(v) for k, v in (fail_nth or {}).items()}
        self.executed: list[str] = []
        self.inserted: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return ScriptedCursor(self)

    def commit(self):
        self.commits += 1
        if self._pending is not None:
            self.tables.clear()
            self.tables.update(self._pending)
            self._pending = None

    def rollback(self):
        self.rollbacks += 1
        self._pending = None

    def _working(self):
        if not self.transactional:
            return self.tables
        if self._pending is None:
            self._pending = {name: Counter(rows) for name, rows in self.tables.items()}
        return self._pending

    def close(self):
        self.closed = True

    def apply(self, sql):
        for statement in sql.split(";"):
            for pattern, action in self.failures.items():
                if re.search(pattern, statement, re.I):
                    raise RuntimeError(action)
            for pattern in list(self.fail_once):
                if re.search(pattern, statement, re.I):
                    raise RuntimeError(self.fail_once.pop(pattern))
            for pattern, state in list(self.fail_nth.items()):
                if re.search(pattern, statement, re.I):
                    state[0] -= 1
                    if state[0] == 0:
                        del self.fail_nth[pattern]
                        raise RuntimeError(state[1])
            if self.side == "projects":
                self._model(statement)

    def _model(self, statement):
        tables = self._working()
        if match := re.search(r"DROP TABLE IF EXISTS (PROJECTD\S+)", statement):
            tables.pop(match.group(1), None)
        if match := re.search(r"CREATE TABLE (PROJECTD\S+)", statement):
            if "IF OBJECT_ID" not in statement or match.group(1) not in tables:
                tables[match.group(1)] = Counter()
        if match := re.search(r"INSERT INTO (PROJECTD\S+) \(", statement):
            label = re.search(r", '([^']*)' FROM #", statement)
            tables[match.group(1)][label.group(1) if label else "-"] += self.rows
        if match := re.search(r"DELETE FROM (PROJECTD\S+) WHERE \[_batch\] = '([^']*)'", statement):
            tables[match.group(1)].pop(match.group(2), None)

    def results_for(self, sql):
        if "@@SERVERNAME" in sql:
            return [(["CosmosServerName"], [(INSTANCE,)])]
        if "OBJECT_ID(N'tempdb.." in sql:
            names = re.findall(r"OBJECT_ID\(N'tempdb\.\.([^']+)'\)", sql)
            return [(names, [tuple(
                1234 if name.lower() in self.existing_temps else None for name in names
            )])]
        if "sys.databases" in sql:
            return [(["name", "create_date"],
                     [("Cosmos", self.created), ("Cosmos_SneakPeek", self.created)])]
        if "SELECT DISTINCT" in sql:
            return [(["total", "distinct"], [(self.rows, self.distinct)])]
        if sql.startswith("SELECT COUNT_BIG(1) FROM PROJECTD"):
            return [(["count"], [(self.pk_rows,)])]
        if "SELECT * FROM" in sql:
            return [(["PatientDurableKey", "Sex"], [(i, "Female") for i in range(3)])]
        sets = []
        for match in re.finditer(r"'([^']+)' AS \[DestTable\]", sql):
            dest = match.group(1)
            if "'cosmos' AS [Side]" in sql:
                sets.append((["DestTable", "Side", "RowCount"],
                             [(dest, "cosmos", self.rows)]))
                sets.append((["DestTable", "Side", "RowCount"],
                             [(dest, "projects", self._landed(sql))]))
            else:
                sets.append((["CohortName", "DestTable", "RowCount"],
                             [(dest, dest, self.rows)]))
        return sets

    def _landed(self, sql):
        if self.landed is not None:
            return self.landed
        match = re.search(
            r"\[RowCount\]\s+FROM (PROJECTD\S+?)(?:\s+WHERE \[_batch\] = '([^']*)')?;", sql
        )
        if not match:
            return self.rows
        rows = self._working().get(match.group(1), Counter())
        return rows.get(match.group(2), 0) if match.group(2) else sum(rows.values())


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

    def make_batched(self, runtime=None):
        """Give the fixture's session two batches, Female and Male."""
        runs_dir = self.root / "sessions" / "Patients" / "runs"
        doc = load_yaml(runs_dir / "run.yaml")
        runs = []
        for value in ("Female", "Male"):
            batch = {
                "name": value,
                "dimensions": [
                    {"name": "sex", "kind": "column_values", "column": "Sex", "value": value}
                ],
                "runtime": list(runtime or []),
            }
            doc["pull_context"]["batch"] = batch
            doc["pull_context"]["run_id"] = f"Patients__{value}"
            dump_yaml(doc, runs_dir / f"{value}.yaml")
            runs.append({
                "run_id": f"Patients__{value}",
                "yaml": f"sessions/Patients/runs/{value}.yaml",
                "status": "pending",
                "batch": batch,
            })
        data = load_yaml(self.root / "pullmanifest.yaml")
        data["sessions"][0]["runs"] = runs
        dump_yaml(data, self.root / "pullmanifest.yaml")
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")

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
    """D52: a retry pulls only what failed, and every batch lands exactly once."""

    def setUp(self):
        super().setUp()
        self.make_batched()
        self.tables: dict[str, Counter] = {}

    def execute(self, **projects):
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        retry = projects.pop("retry_failed", False)
        with self.runner(projects={"tables": self.tables, **projects}, retry_failed=retry) as runner:
            return runner.execute()

    def test_a_failed_batch_is_retried_alone_and_lands_once(self):
        # Male fails after its rows have landed: the worst case.
        first = self.execute(
            fail_once={r"\[RowCount\]\s+FROM PROJECTD\S+\s+WHERE \[_batch\] = 'Male'": "timeout"}
        )
        self.assertEqual([label for label, _ in first.failed], ["Patients__Male"])
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))

        second = self.execute(retry_failed=True)
        self.assertTrue(second.ok, second.failed)
        # The outcome: each batch's rows present exactly once.
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))
        # Only the failed batch was pulled again; the PK query was not rerun.
        self.assertIn("Patients__Male", second.completed)
        self.assertNotIn("Patients__Female", second.completed)
        self.assertNotIn("Patients/pk", second.completed)
        self.assertIn(PK_DEST, self.tables, "the PK's Projects copy must be kept")

    def test_an_interrupted_batch_is_cleared_before_it_is_pulled_again(self):
        self.execute()
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        manifest.sessions[0].runs[1].data["status"] = "running"  # a crash mid-run
        manifest.save()
        self.execute()
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))

    def test_a_finished_session_pulls_nothing(self):
        self.execute()
        again = self.execute()
        self.assertEqual(again.completed, [])
        self.assertTrue(any("nothing left" in label for label in again.skipped))

    def test_starting_over_replaces_every_batch(self):
        self.execute()
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        manifest.reset_all("re-pulled: --repull")
        manifest.save()
        again = self.execute()
        self.assertIn("Patients/pk", again.completed)
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))

    def test_the_count_check_counts_only_this_batch(self):
        # The destination holds every earlier batch too; comparing against all
        # of it warned falsely from the second batch on.
        report = self.execute()
        self.assertFalse(any("did not carry everything" in w for w in report.warnings), report.warnings)

    def test_a_failed_unit_is_rolled_back(self):
        self.execute(fail_once={r"WHERE \[_batch\] = 'Male'": "boom"})
        self.assertGreaterEqual(self.projects.rollbacks, 1)


class RefreshTests(SessionTestCase):
    """D51: a Cosmos refresh starts everything over; finished work is skipped."""

    def setUp(self):
        super().setUp()
        self.make_batched()
        self.tables: dict[str, Counter] = {}
        self.stamps = [LAST_REFRESH]
        self.opened: list[str] = []

    def connect(self, conn_str, **_):
        self.opened.append(conn_str)
        if "PROJECTD" in conn_str:
            return FakeConnection("projects", tables=self.tables)
        return FakeConnection("cosmos", created=self.stamps.pop(0) if len(self.stamps) > 1 else self.stamps[0])

    def execute(self, **flags):
        args = argparse.Namespace(**{"env": None, "repull": False, "retry_failed": False, **flags})
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.execute(manifest, args, connect_fn=self.connect)
        return code, out.getvalue(), Manifest.load(self.root / "pullmanifest.yaml")

    def test_records_create_date(self):
        code, _, manifest = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(manifest.cosmos_refresh, {"Cosmos": "2026-09-17T19:34:56.450"})
        self.assertEqual(
            manifest.sessions[0].runtime["cosmos_created"], {"Cosmos": "2026-09-17T19:34:56.450"}
        )

    def test_a_finished_pull_opens_no_session(self):
        self.execute()
        self.opened.clear()
        code, out, _ = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.opened), 1, "only the refresh check should connect")
        self.assertIn("nothing left to pull", out)

    def test_a_refresh_starts_everything_over(self):
        self.execute()
        self.stamps = [NEXT_REFRESH]
        self.opened.clear()
        code, out, manifest = self.execute()
        self.assertEqual(code, 0)
        self.assertIn("was refreshed", out)
        self.assertGreater(len(self.opened), 1, "the finished session must be pulled again")
        self.assertEqual(manifest.cosmos_refresh, {"Cosmos": "2026-10-15T19:30:02.100"})
        self.assertTrue(all(run.status == "done" for run in manifest.sessions[0].runs))
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))

    def test_a_refresh_during_the_run_stops_the_session(self):
        # Checked before sessions open with one value, then Cosmos comes back
        # rebuilt before the session connects.
        self.stamps = [LAST_REFRESH, NEXT_REFRESH]
        code, out, manifest = self.execute()
        self.assertEqual(code, 1)
        self.assertIn("refreshed while this pull was running", out)
        self.assertNotIn(DEST, self.tables)

    def test_repull_starts_a_finished_pull_over(self):
        self.execute()
        self.opened.clear()
        code, out, _ = self.execute(repull=True)
        self.assertEqual(code, 0)
        self.assertGreater(len(self.opened), 1)
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))


class ChunkTests(SessionTestCase):
    """D53: every chunk of a batch is pulled, inside its run, exactly once."""

    CHUNK = {"name": "chunk", "kind": "row_chunk", "rows_per_batch": 2000, "applies_to": "PKTable"}

    def setUp(self):
        super().setUp()
        self.declare_pk_key()
        self.make_batched(runtime=[self.CHUNK])
        self.tables: dict[str, Counter] = {}

    def execute(self, retry_failed=False, **projects):
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        settings = {"tables": self.tables, "pk_rows": 4500, **projects}
        with self.runner(projects=settings, retry_failed=retry_failed) as runner:
            return runner.execute()

    def windows(self):
        return [
            (int(offset), int(size))
            for sql in self.projects.executed
            for offset, size in re.findall(r"OFFSET (\d+) ROWS FETCH NEXT (\d+) ROWS ONLY", sql)
        ]

    def test_every_chunk_is_pulled(self):
        # 4,500 PK rows in chunks of 2,000: rows 0-2000, 2000-4000, 4000-4500,
        # for each batch. Before, only the first chunk was pulled, silently.
        report = self.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(self.windows(), [(0, 2000), (2000, 2000), (4000, 2000)] * 2)
        self.assertEqual(self.tables[DEST], Counter({"Female": 30, "Male": 30}))
        self.assertFalse(any("did not carry everything" in w for w in report.warnings), report.warnings)

    def test_progress_is_recorded_on_the_run(self):
        self.execute()
        run = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].runs[0]
        self.assertEqual(run.outputs["chunk"], "c3of3")
        self.assertEqual(run.outputs["batch_pk_rows_total"], 4500)

    def test_a_batch_failing_mid_chunks_lands_once_after_a_retry(self):
        # Male's first chunk lands, its second fails.
        first = self.execute(fail_nth={r", 'Male' FROM #Local_": (2, "timeout")})
        self.assertEqual([label for label, _ in first.failed], ["Patients__Male"])
        self.assertEqual(self.tables[DEST], Counter({"Female": 30, "Male": 10}))
        second = self.execute(retry_failed=True)
        self.assertTrue(second.ok, second.failed)
        self.assertEqual(self.tables[DEST], Counter({"Female": 30, "Male": 30}))
        self.assertNotIn("Patients__Female", second.completed)

    def test_an_empty_batch_still_runs_once(self):
        report = self.execute(pk_rows=0)
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(self.windows(), [(0, 2000)] * 2)


class TempClashTests(SessionTestCase):
    """D50: a temp another pull holds is never dropped; this session renumbers."""

    def cosmos_sql(self):
        """Everything sent to Cosmos except the probe that asks which names are taken."""
        sent = self.cosmos.executed + [sql for sql, _ in self.cosmos.inserted]
        return "\n".join(sql for sql in sent if "OBJECT_ID(N'tempdb" not in sql)

    def test_no_clash_uses_the_planned_prefix(self):
        with self.runner() as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(runner.session.runtime["temp_prefix"], "manvalbas")
        self.assertIn("##manvalbas_Patients", self.cosmos_sql())

    def test_a_held_temp_moves_this_session_to_a_numbered_prefix(self):
        held = {"##manvalbas_OtherHospitalizations"}
        with self.runner(cosmos={"existing_temps": held}) as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(runner.session.runtime["temp_prefix"], "manvalbas2")
        self.assertTrue(any("##manvalbas2_" in w for w in report.warnings), report.warnings)
        # The outcome: nothing this session sent names the other pull's temps.
        self.assertNotIn("##manvalbas_", self.cosmos_sql())
        self.assertNotIn("##manvalbas_", "\n".join(self.projects.executed))
        self.assertIn("##manvalbas2_OtherHospitalizations", "\n".join(self.projects.executed))
        self.assertIn("##manvalbas2_HospitalICDCodes", self.cosmos_sql())

    def test_numbering_continues_past_a_second_clash(self):
        held = {"##manvalbas_Patients", "##manvalbas2_Patients"}
        with self.runner(cosmos={"existing_temps": held}) as runner:
            runner.execute()
        self.assertEqual(runner.session.runtime["temp_prefix"], "manvalbas3")


class CommitTests(SessionTestCase):
    """D55: each cohort is saved in Projects before the next is pulled."""

    def add_second_cohort(self):
        path = self.root / "sessions" / "Patients" / "runs" / "run.yaml"
        doc = load_yaml(path)
        second = dict(doc["cohorts"][0])
        second["name"] = second["dest_table"] = "SecondHosp"
        doc["cohorts"].append(second)
        dump_yaml(doc, path)

    def test_a_cohort_is_saved_before_the_next_is_built(self):
        # The second cohort's build fails. The first must already be in
        # Projects, committed: before, every cohort was built before any landed.
        self.add_second_cohort()
        tables: dict[str, Counter] = {}
        with self.runner(
            cosmos={"failures": {r"CREATE TABLE ##manvalbas_SecondHosp": "tempdb full"}},
            projects={"tables": tables, "transactional": True},
        ) as runner:
            report = runner.execute()
        self.assertEqual([label for label, _ in report.failed], ["Patients__run"])
        self.assertEqual(tables[DEST], Counter({"all": 10}))
        self.assertEqual(tables["PROJECTD33A929.dbo.SecondHosp"], Counter())

    def test_a_multi_column_key_is_counted_with_valid_sql(self):
        # SQL Server has no COUNT(DISTINCT a, b).
        from ..yaml_io import dump_yaml as dump, load_yaml as load

        path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load(path)
        doc["cohorts"][0]["key_columns"] = ["PatientDurableKey", "DiagnosisEventKey"]
        dump(doc, path)
        with self.runner() as runner:
            runner.execute()
        sent = "\n".join(self.projects.executed)
        self.assertNotIn("COUNT_BIG(DISTINCT", sent)
        self.assertIn("SELECT DISTINCT [PatientDurableKey], [DiagnosisEventKey]", sent)
