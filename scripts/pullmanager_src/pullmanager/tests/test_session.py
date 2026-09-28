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
        self.owner.executed_params.append((sql, list(params or [])))
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

    def fetchmany(self, size):
        if self._current is None:
            raise RuntimeError("no rows")
        rows, self._current = self._current[1][:size], (self._current[0], self._current[1][size:])
        return list(rows)

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
                 pk_rows=3, existing_temps=(), transactional=False, upload_columns=None,
                 widths=None, found_values=None):
        self.side = side
        # What `SELECT DISTINCT [column]` finds in the PK copy, for values: all.
        self.found_values = found_values
        # Column -> the widest value each measurement of it reports, in turn.
        self.widths = {column: list(values) for column, values in (widths or {}).items()}
        self.rows = rows
        self.distinct = rows if distinct is None else distinct
        self.landed = landed
        self.failures = failures or {}
        self.fail_once = fail_once if fail_once is not None else {}
        self.tables = tables if tables is not None else {}
        self.created = created
        self.pk_rows = pk_rows
        # What INFORMATION_SCHEMA reports for an upload's Projects copy.
        self.upload_columns = upload_columns or [
            ("DiagnosisCode", "nvarchar", 55, None, None, None),
            ("Description", "nvarchar", 82, None, None, None),
        ]
        # Transactional: changes apply to a working copy that a commit makes
        # durable and a rollback discards. Off by default: the worst case.
        self.transactional = transactional
        self._pending = None
        # Global temps another pull holds on this instance, for D50's check.
        self.existing_temps = {name.lower() for name in existing_temps}
        # pattern -> [matches left before failing, message]
        self.fail_nth = {k: list(v) for k, v in (fail_nth or {}).items()}
        self.executed: list[str] = []
        self.executed_params: list[tuple[str, list]] = []
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
        if match := re.search(r"SELECT \* INTO (PROJECTD\S+) FROM", statement):
            tables[match.group(1)] = Counter()
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
        if match := re.search(r"SELECT OBJECT_ID\(N'(PROJECTD[^']+)', N'U'\)", sql):
            return [(["id"], [(99 if match.group(1) in self._working() else None,)])]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [(["COLUMN_NAME", "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH", "NUMERIC_PRECISION",
                      "NUMERIC_SCALE", "DATETIME_PRECISION"], list(self.upload_columns))]
        if "OBJECT_ID(N'tempdb.." in sql:
            names = re.findall(r"OBJECT_ID\(N'tempdb\.\.([^']+)'\)", sql)
            return [(names, [tuple(
                1234 if name.lower() in self.existing_temps else None for name in names
            )])]
        if "sys.databases" in sql:
            return [(["name", "create_date"],
                     [("Cosmos", self.created), ("Cosmos_SneakPeek", self.created)])]
        if sql.startswith("SELECT DISTINCT [") and self.found_values is not None:
            return [(["value"], [(v,) for v in self.found_values])]
        if "SELECT DISTINCT" in sql:
            return [(["total", "distinct"], [(self.rows, self.distinct)])]
        if sql.startswith("SELECT COUNT_BIG(1) FROM PROJECTD"):
            return [(["count"], [(self.pk_rows,)])]
        if "SELECT * FROM" in sql:
            return [(["PatientDurableKey", "Sex"], [(i, "Female") for i in range(3)])]
        if whole := re.match(r"SELECT ((?:\[[^\]]+\](?:, )?)+) FROM (PROJECTD\S+);$", sql):
            # A whole table read into a parquet (D87): its described columns.
            names = re.findall(r"\[([^\]]+)\]", whole.group(1))
            return [(names, [tuple(f"{name}{i}" for name in names) for i in range(self.pk_rows)])]
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
        measured = re.findall(
            r"'([^']+)' AS \[DestTable\],\s*'([^']+)' AS \[Column\],\s*'([^']+)' AS \[DeclaredType\]",
            sql,
        )
        if measured:
            sets.append((["DestTable", "Column", "DeclaredType", "MaxLength"], [
                (dest, column, declared,
                 self.widths[column].pop(0) if self.widths.get(column) else None)
                for dest, column, declared in measured
            ]))
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
        try:
            import pyarrow  # noqa: F401  the fixture's upload is parquet (D54)
        except ImportError:
            self.skipTest("needs pyarrow")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "split"
        shutil.copytree(FIXTURES, self.root)
        (self.root / "fixtures").mkdir(exist_ok=True)
        (self.root / "fixtures" / "hospital_icd_codes.csv").write_text(
            "DiagnosisCode,Label\nK50.0,Crohn's\nK51.0,UC\n", encoding="utf-8"
        )
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")

    def remove_pk_key(self):
        """The fixture's PK is keyed by its dedup_keys (D69); take them away."""
        path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load_yaml(path)
        for key in ("dedup_keys", "dedup_order_by", "key_column", "key_columns"):
            doc["cohorts"][0].pop(key, None)
        dump_yaml(doc, path)

    def declare_pk_key(self, column="PatientDurableKey"):
        """Give the fixture's PK a key_column as well as its dedup_keys."""
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


class PkParquetTests(SessionTestCase):
    """D87: the whole PK is written to parquet once it lands, before any run."""

    def test_the_pk_is_a_parquet_where_artifacts_puts_it(self):
        import pyarrow.parquet as pq

        with self.runner(projects={"pk_rows": 5}) as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        path = self.root.parent / "parquets" / "Cosmos" / "Patients.parquet"
        self.assertTrue(path.is_file(), report.warnings)
        self.assertEqual(pq.read_metadata(path).num_rows, 5)
        pk = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[2]
        self.assertEqual(pk.outputs["pk_parquet"], {"file": "parquets/Cosmos/Patients.parquet", "rows": 5})

    def test_it_is_written_before_any_run(self):
        from .. import artifacts

        order = []
        original = artifacts.write_whole_table

        def spy(*args):
            order.append("pk parquet")
            return original(*args)

        artifacts.write_whole_table = spy
        self.addCleanup(setattr, artifacts, "write_whole_table", original)
        with self.runner() as runner:
            original_run = runner._run_run

            def run(*args):
                order.append("run")
                return original_run(*args)

            runner._run_run = run
            runner.execute()
        self.assertEqual(order[0], "pk parquet", order)

    def test_a_failure_to_write_it_warns_and_the_pull_goes_on(self):
        from .. import artifacts

        def broken(*_):
            raise artifacts.ArtifactError("this Python lacks pyarrow")

        original = artifacts.write_whole_table
        artifacts.write_whole_table = broken
        self.addCleanup(setattr, artifacts, "write_whole_table", original)
        with self.runner() as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(len(report.completed), 4)
        self.assertTrue(any("PK was not written to parquet" in w for w in report.warnings), report.warnings)


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

    def test_a_pk_without_a_key_warns_instead_of_checking(self):
        # Nothing to order by means chunk stability cannot be verified. The
        # warning names both ways to declare a key (D69).
        self.remove_pk_key()
        with self.runner() as runner:
            report = runner.execute()
        warning = next(w for w in report.warnings if "declares no key" in w)
        self.assertIn("dedup_keys", warning)
        self.assertIn("key_column", warning)

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

        self.remove_pk_key()
        path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load(path)
        doc["cohorts"][0]["key_columns"] = ["PatientDurableKey", "DiagnosisEventKey"]
        dump(doc, path)
        with self.runner() as runner:
            runner.execute()
        sent = "\n".join(self.projects.executed)
        self.assertNotIn("COUNT_BIG(DISTINCT", sent)
        self.assertIn("SELECT DISTINCT [PatientDurableKey], [DiagnosisEventKey]", sent)


class UploadCopyTests(SessionTestCase):
    """D54: uploads land typed in Projects first, and that copy is the source."""

    COPY = "PROJECTD33A929.dbo.upload_HospitalICDCodes"
    TEMP = "##manvalbas_HospitalICDCodes"

    def setUp(self):
        super().setUp()
        self.make_batched()
        self.tables: dict[str, Counter] = {}

    def execute(self, **projects):
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        retry = projects.pop("retry_failed", False)
        with self.runner(projects={"tables": self.tables, **projects}, retry_failed=retry) as runner:
            return runner.execute()

    def test_an_upload_lands_in_projects_then_goes_up_from_the_copy(self):
        report = self.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertIn(self.COPY, self.tables)
        projects = "\n".join(self.projects.executed)
        self.assertIn(f"CREATE TABLE {self.COPY}", projects)
        self.assertIn(f"SELECT * FROM {self.COPY};", projects)
        cosmos = "\n".join(self.cosmos.executed)
        # The temp takes the copy's types, not text for everything.
        self.assertIn(f"CREATE TABLE {self.TEMP}\n(\n    [DiagnosisCode] NVARCHAR(55) NULL", cosmos)
        self.assertTrue(any(self.TEMP in sql for sql, _ in self.cosmos.inserted))

    def start_over_without_repull(self):
        """As a later session does: from scratch, in the same pull."""
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        for child in manifest.sessions[0].children:
            child.reset("a later session")
        manifest.save()

    def test_a_later_session_uses_the_copy_landed_earlier_in_the_pull(self):
        # D61: every session landed the file again; eight sessions, eight copies.
        self.assertTrue(self.execute().ok)
        self.start_over_without_repull()
        second = self.execute()
        self.assertTrue(second.ok, second.failed)
        self.assertNotIn(f"CREATE TABLE {self.COPY}", "\n".join(self.projects.executed))
        # Its Cosmos temp died with the first session's connection: loaded again.
        self.assertIn(f"CREATE TABLE {self.TEMP}", "\n".join(self.cosmos.executed))
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        self.assertEqual(manifest.uploads_landed["HospitalICDCodes"]["table"], self.COPY)
        phase = manifest.sessions[0].phases[1]
        self.assertEqual(phase.outputs["uploads"]["HospitalICDCodes"]["projects"],
                         "landed earlier in this pull")

    def test_a_repull_lands_the_file_again(self):
        self.assertTrue(self.execute().ok)
        manifest = Manifest.load(self.root / "pullmanifest.yaml")
        manifest.reset_all("re-pulled: --repull")
        manifest.save()
        self.assertTrue(self.execute().ok)
        self.assertIn(f"CREATE TABLE {self.COPY}", "\n".join(self.projects.executed))

    def test_a_reused_copy_that_is_gone_is_refused(self):
        self.assertTrue(self.execute().ok)
        self.start_over_without_repull()
        del self.tables[self.COPY]
        report = self.execute()
        message = dict(report.failed)["Patients/upload_cohorts"]
        self.assertIn("landed earlier in this pull", message)
        self.assertIn("--repull", message)

    def test_an_upload_no_cohort_reads_is_landed_but_not_sent_to_cosmos(self):
        path = self.root / "sessions" / "Patients" / "upload_cohorts.yaml"
        doc = load_yaml(path)
        unused = dict(doc["upload_cohorts"][0], name="Unused", dest_table="Unused")
        doc["upload_cohorts"].append(unused)
        dump_yaml(doc, path)
        report = self.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertIn("PROJECTD33A929.dbo.upload_Unused", self.tables)
        self.assertNotIn("CREATE TABLE ##manvalbas_Unused", "\n".join(self.cosmos.executed))
        self.assertFalse([sql for sql, _ in self.cosmos.inserted if "##manvalbas_Unused" in sql])
        phase = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[1]
        self.assertEqual(phase.outputs["uploads"]["Unused"]["cosmos"], "not read in this session")

    def test_a_renamed_and_dropped_upload_lands_so_in_projects(self):
        # D98: the rename happens as the file lands, so what reads the copy sees it.
        path = self.root / "sessions" / "Patients" / "upload_cohorts.yaml"
        doc = load_yaml(path)
        doc["upload_cohorts"][0]["columns"] = [{"name": "ICDCode", "from": "DiagnosisCode"},
                                               {"name": "Description", "drop": True}]
        dump_yaml(doc, path)
        report = self.execute()
        self.assertTrue(report.ok, report.failed)
        projects = "\n".join(self.projects.executed)
        created = projects[projects.index(f"CREATE TABLE {self.COPY}"):]
        created = created[:created.index(");")]
        self.assertIn("[ICDCode]", created)
        self.assertNotIn("[DiagnosisCode]", created)
        self.assertNotIn("[Description]", created)
        # Filled under the new name; the Cosmos temp copies the copy's columns
        # as INFORMATION_SCHEMA reports them, which this fake does not model.
        inserts = [sql for sql, _ in self.projects.inserted if self.COPY in sql]
        self.assertTrue(inserts)
        self.assertTrue(all("[ICDCode]" in sql and "Description" not in sql for sql in inserts))

    def test_a_retry_uses_the_copy_not_the_file(self):
        # The file is gone (or changed) by the retry; the copy is what counts.
        first = self.execute(fail_once={r"WHERE \[_batch\] = 'Male'": "timeout"})
        self.assertFalse(first.ok)
        (self.root / "uploads" / "hospital_icd_codes.parquet").unlink()
        second = self.execute(retry_failed=True)
        self.assertTrue(second.ok, second.failed)
        self.assertNotIn(f"CREATE TABLE {self.COPY}", "\n".join(self.projects.executed))
        self.assertEqual(self.tables[DEST], Counter({"Female": 10, "Male": 10}))

    def test_a_copy_missing_on_resume_points_at_repull(self):
        self.execute(fail_once={r"WHERE \[_batch\] = 'Male'": "timeout"})
        del self.tables[self.COPY]
        report = self.execute(retry_failed=True)
        self.assertTrue(any("--repull" in message for _, message in report.failed), report.failed)


class ControlSampleTests(SessionTestCase):
    """D59: a control's PK keeps row_mult times its case, batch by batch."""

    CASE = "PROJECTD33A929.dbo.CasePatients"

    def make_control(self, matched_to="CasePatients"):
        path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load_yaml(path)
        doc["cohorts"][0]["key_column"] = "PatientDurableKey"
        doc["cohorts"][0]["split_after_build"] = [{
            "multiplier": "Race", "strat": "white", "applies_to": "PKTable",
            "role": "control", "row_mult": 4, "matched_to": matched_to,
        }]
        dump_yaml(doc, path)

    def execute(self, tables, **projects):
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        with self.runner(projects={"tables": tables, "pk_rows": 3, **projects}) as runner:
            return runner.execute()

    def deletes(self):
        return [(sql, params) for sql, params in self.projects.executed_params
                if sql.startswith("WITH [_ranked]")]

    def test_each_batch_keeps_row_mult_times_its_case(self):
        self.make_batched()
        self.make_control()
        report = self.execute({self.CASE: Counter()})
        self.assertTrue(report.ok, report.failed)
        deletes = self.deletes()
        # The case has 3 rows in each batch, so each batch keeps 12 controls.
        self.assertEqual([params for _, params in deletes], [["Female", 12], ["Male", 12]])
        self.assertTrue(all("FROM PROJECTD33A929.dbo.Patients WHERE [Sex] = ?" in sql
                            for sql, _ in deletes))
        self.assertTrue(all("HASHBYTES('SHA2_256', CAST([PatientDurableKey]" in sql
                            for sql, _ in deletes))
        counted = [sql for sql in self.projects.executed if "COUNT_BIG(1) FROM " + self.CASE in sql]
        self.assertEqual(len(counted), 2)
        pk = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[2]
        self.assertEqual(pk.outputs["control_sample"]["per_batch"],
                         {"Female": {"cases": 3, "controls": 3}, "Male": {"cases": 3, "controls": 3}})
        self.assertTrue(any("so all are kept" in w for w in report.warnings), report.warnings)

    def test_the_sample_is_taken_before_any_run_reads_the_copy(self):
        self.make_batched()
        self.make_control()
        self.execute({self.CASE: Counter()})
        sent = self.projects.executed
        first_delete = next(i for i, sql in enumerate(sent) if sql.startswith("WITH [_ranked]"))
        first_batch_read = next(i for i, sql in enumerate(sent)
                                if sql.startswith("SELECT * FROM PROJECTD33A929.dbo.Patients"))
        self.assertLess(first_delete, first_batch_read)

    def test_an_unbatched_control_rebuilds_its_temp_from_the_sample(self):
        # Its Cosmos temp still holds every row the PK query built.
        self.make_control()
        report = self.execute({self.CASE: Counter()})
        self.assertTrue(report.ok, report.failed)
        self.assertEqual([params for _, params in self.deletes()], [[12]])
        self.assertIn("SELECT * FROM PROJECTD33A929.dbo.Patients;", self.projects.executed)
        self.assertTrue(any("##manvalbas_Patients" in sql for sql, _ in self.cosmos.inserted))

    def test_a_missing_case_stops_the_pk_saying_why(self):
        self.make_control()
        report = self.execute({})
        self.assertFalse(report.ok)
        message = dict(report.failed)["Patients/pk"]
        self.assertIn("CasePatients", message)
        self.assertIn("run it first", message)
        self.assertEqual(self.deletes(), [])


class ValuesAllTests(SessionTestCase):
    """D82: a batch without listed values pulls every value the PK has."""

    STATE = {"name": "state", "kind": "column_values", "column": "StateOrProvinceAbbreviation",
             "values": "all", "applies_to": "PKTable"}

    def execute(self, runtime, **projects):
        self.make_batched(runtime=runtime)
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        self.tables: dict[str, Counter] = {}
        with self.runner(projects={"tables": self.tables, **projects}) as runner:
            return runner.execute()

    def narrowed(self):
        return [(sql, params) for sql, params in self.projects.executed_params
                if sql.startswith("SELECT * FROM PROJECTD33A929.dbo.Patients")]

    def test_every_value_found_is_pulled_null_included(self):
        # Refused before: "values: all ... Not yet supported".
        report = self.execute([self.STATE], found_values=["LA", "MS", None])
        self.assertTrue(report.ok, report.failed)
        female = [(sql, params) for sql, params in self.narrowed() if params[:1] == ["Female"]]
        self.assertEqual([params for _, params in female], [["Female", "LA"], ["Female", "MS"], ["Female"]])
        self.assertIn("[StateOrProvinceAbbreviation] IS NULL", female[2][0])
        # Each value lands in the run, under the run's own label.
        self.assertEqual(self.tables[DEST], Counter({"Female": 30, "Male": 30}))
        run = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].runs[0]
        self.assertEqual(run.outputs["values_found"], 3)
        self.assertEqual(run.outputs["value"], "v3of3 (NULL)")
        self.assertFalse(any("did not carry everything" in w for w in report.warnings), report.warnings)

    def test_values_are_looked_for_within_the_batch(self):
        self.execute([self.STATE], found_values=["LA"])
        found = [(sql, params) for sql, params in self.projects.executed_params
                 if sql.startswith("SELECT DISTINCT [StateOrProvinceAbbreviation]")]
        self.assertEqual([params for _, params in found], [["Female"], ["Male"]])
        self.assertIn("WHERE [Sex] = ?", found[0][0])

    def test_each_value_is_chunked_in_turn(self):
        self.declare_pk_key()
        report = self.execute([self.STATE, ChunkTests.CHUNK], found_values=["LA", "MS"], pk_rows=4500)
        self.assertTrue(report.ok, report.failed)
        windows = [
            (int(o), int(s)) for sql in self.projects.executed
            for o, s in re.findall(r"OFFSET (\d+) ROWS FETCH NEXT (\d+) ROWS ONLY", sql)
        ]
        # Two values, three chunks each, for each of the two runs.
        self.assertEqual(windows, [(0, 2000), (2000, 2000), (4000, 2000)] * 4)
        self.assertEqual(self.tables[DEST], Counter({"Female": 60, "Male": 60}))

    def test_no_values_warns_and_pulls_nothing(self):
        report = self.execute([self.STATE], found_values=[])
        self.assertTrue(report.ok, report.failed)
        self.assertTrue(any("no values of StateOrProvinceAbbreviation" in w for w in report.warnings))
        self.assertEqual(self.tables.get(DEST, Counter()), Counter())


class ReadoutTests(SessionTestCase):
    """D70: widths once per session, the widest across its batches, as notes."""

    def execute(self, widths, **projects):
        self.make_batched()
        tables: dict[str, Counter] = {}

        def connect(conn_str, **_):
            if "PROJECTD" in conn_str:
                return FakeConnection("projects", tables=tables, widths=widths, **projects)
            return FakeConnection("cosmos")

        args = argparse.Namespace(env=None, repull=False, retry_failed=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.execute(Manifest.load(self.root / "pullmanifest.yaml"), args, connect_fn=connect)
        return code, out.getvalue()

    def test_one_line_per_column_holding_the_widest_of_every_batch(self):
        # Female measures 6, Male 9. Each batch printed its own before.
        code, out = self.execute({"EncounterType": [6, 9], "Sex": [6]})
        self.assertEqual(code, 0, out)
        rows = [line for line in out.splitlines() if "EncounterType" in line]
        self.assertEqual(len(rows), 1, out)
        self.assertRegex(rows[0], r"OtherHospitalizations\s+EncounterType\s+NVARCHAR\(300\)\s+9$")
        self.assertEqual(len([line for line in out.splitlines() if " Sex " in line]), 1)
        self.assertFalse([line for line in out.splitlines() if line.startswith("  warning")], out)

    def test_they_are_notes_after_the_warnings(self):
        # Projects reports fewer rows than Cosmos: a warning that needs a look.
        code, out = self.execute({"EncounterType": [6, 9]}, landed=5)
        lines = out.splitlines()
        note = next(i for i, line in enumerate(lines) if line.startswith("  note     Column widths"))
        warnings = [i for i, line in enumerate(lines) if line.startswith("  warning")]
        self.assertTrue(warnings, out)
        self.assertLess(max(warnings), note)
        self.assertFalse(any("EncounterType" in line for line in lines if "warning" in line))


class PkKeyTests(SessionTestCase):
    """D69: a PK keyed only by dedup_keys, as the fixture's is, is still keyed.

    The IBD Ancestry PK named its key only in dedup_keys, so every session
    warned "PK declares no key_column" and its uniqueness was never checked.
    """

    CHUNK = ChunkTests.CHUNK

    def execute(self, tables=None, **projects):
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
        settings = {"tables": {} if tables is None else tables, **projects}
        with self.runner(projects=settings) as runner:
            return runner.execute()

    def test_a_duplicated_key_stops_the_pk(self):
        # Unchecked, duplicates would have gone on to every run.
        report = self.execute(rows=10, distinct=9)
        self.assertIn("Patients/pk", dict(report.failed))
        self.assertIn("distinct PatientDurableKey", dict(report.failed)["Patients/pk"])

    def test_a_unique_key_passes_without_a_warning(self):
        report = self.execute()
        self.assertTrue(report.ok, report.failed)
        self.assertFalse(any("declares no key" in w for w in report.warnings), report.warnings)

    def test_every_chunk_is_pulled_on_such_a_pk(self):
        # It failed its runs before: "Row chunking needs the PK key columns".
        self.make_batched(runtime=[self.CHUNK])
        tables: dict[str, Counter] = {}
        report = self.execute(tables, pk_rows=4500)
        self.assertTrue(report.ok, report.failed)
        self.assertEqual(tables[DEST], Counter({"Female": 30, "Male": 30}))


class UploadedPkTests(SessionTestCase):
    """D54: an uploaded PK's checks and batches read its Projects copy."""

    def setUp(self):
        super().setUp()
        import pyarrow
        import pyarrow.parquet

        self.make_batched()
        pyarrow.parquet.write_table(
            pyarrow.table({"PatientDurableKey": pyarrow.array([1, 2], pyarrow.int64()),
                           "Sex": ["Female", "Male"]}),
            str(self.root / "uploads" / "pks.parquet"),
        )
        source = {"kind": "uploaded_cohort", "upload_name": "ClientPK", "table": "ClientPK",
                  "key_columns": ["PatientDurableKey"]}
        uploads_path = self.root / "sessions" / "Patients" / "upload_cohorts.yaml"
        doc = load_yaml(uploads_path)
        doc["upload_cohorts"].append({
            "name": "ClientPK", "dest_table": "ClientPK", "type": "pk", "file_type": "parquet",
            "file_loc": "uploads/pks.parquet", "key_columns": ["PatientDurableKey"],
        })
        dump_yaml(doc, uploads_path)
        pk_path = self.root / "sessions" / "Patients" / "pk.yaml"
        doc = load_yaml(pk_path)
        doc["cohorts"] = []
        doc["pull_context"]["pk_source"] = source
        dump_yaml(doc, pk_path)
        data = load_yaml(self.root / "pullmanifest.yaml")
        data["sessions"][0]["pk_table"] = "ClientPK"
        data["sessions"][0]["phases"]["pk"]["pk_source"] = source
        dump_yaml(data, self.root / "pullmanifest.yaml")
        self.manifest = Manifest.load(self.root / "pullmanifest.yaml")

    def test_its_uniqueness_and_batches_read_the_copy(self):
        copy = "PROJECTD33A929.dbo.upload_ClientPK"
        pk_columns = [("PatientDurableKey", "bigint", None, 19, 0, None),
                      ("Sex", "nvarchar", 56, None, None, None)]
        with self.runner(projects={"upload_columns": pk_columns}) as runner:
            report = runner.execute()
        self.assertTrue(report.ok, report.failed)
        projects = "\n".join(self.projects.executed)
        self.assertIn(f"SELECT DISTINCT [PatientDurableKey] FROM {copy}", projects)
        self.assertIn(f"SELECT * FROM {copy}\nWHERE [Sex] = ?", projects)
        self.assertNotIn("dbo.ClientPK", projects.replace("upload_ClientPK", ""))
        # The PK temp each batch refills takes the copy's types.
        self.assertIn(
            "CREATE TABLE ##manvalbas_ClientPK\n(\n    [PatientDurableKey] BIGINT NULL",
            "\n".join(self.cosmos.executed),
        )
