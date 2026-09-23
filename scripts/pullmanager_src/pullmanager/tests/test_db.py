"""Adapter behaviour, exercised against a fake cursor.

pyodbc is not installed on the development machine and there is no database to
reach, so the DB-API interactions are faked. What is tested is the logic the
old generator got right and that is easy to get wrong: GO splitting, draining
every result set, and keeping server messages on the failure path.
"""

from __future__ import annotations

import unittest

from ..db import (
    DEFAULT_DRIVER,
    DatabaseError,
    ResultSet,
    Settings,
    bulk_insert,
    capture_server_name,
    chunked,
    collect_messages,
    drain,
    execute_script,
    split_batches,
)


class FakeCursor:
    """Enough of the DB-API to exercise the drain loop.

    `statements` is one entry per statement in the current batch: None for a
    statement that returns no rows, or (columns, rows) for one that does.
    """

    def __init__(self, statements=None, messages=(), fail_on=None):
        self._template = statements if statements is not None else [None]
        self._messages = list(messages)
        self._fail_on = fail_on
        self.executed: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.fast_executemany = False
        self._pending: list = []
        self._current = None

    @property
    def messages(self):
        return self._messages

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            raise RuntimeError("simulated SQL failure")
        self._pending = list(self._template)
        self._advance()

    def executemany(self, sql, seq):
        self.executemany_calls.append((sql, list(seq)))

    def _advance(self):
        self._current = self._pending.pop(0) if self._pending else None

    @property
    def description(self):
        if self._current is None:
            return None
        return [(name,) for name in self._current[0]]

    def fetchall(self):
        if self._current is None:
            raise RuntimeError("no result set")
        return list(self._current[1])

    def fetchone(self):
        if self._current is None:
            return None
        rows = self._current[1]
        return rows[0] if rows else None

    def nextset(self):
        if not self._pending:
            return False
        self._advance()
        return True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class SettingsTests(unittest.TestCase):
    def test_connection_string_matches_the_shape_in_use(self):
        settings = Settings(cosmos_server="COSMOS", cosmos_database="COSMOS")
        self.assertEqual(
            settings.cosmos_connection_string(),
            "Driver={ODBC Driver 17 for SQL Server};Server=tcp:COSMOS;"
            "Database=COSMOS;Trusted_Connection=yes;",
        )

    def test_windows_auth_means_no_credentials_appear(self):
        rendered = Settings(projects_server="S", projects_database="D").projects_connection_string()
        self.assertIn("Trusted_Connection=yes", rendered)
        for secret in ("UID=", "PWD=", "Password"):
            self.assertNotIn(secret, rendered)

    def test_reads_environment_with_defaults(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.driver, DEFAULT_DRIVER)
        self.assertEqual(settings.cosmos_server, "COSMOS")

        settings = Settings.from_env({
            "PULLMANAGER_COSMOS_SERVER": "OTHER",
            "PULLMANAGER_UPLOAD_CHUNK": "500",
        })
        self.assertEqual(settings.cosmos_server, "OTHER")
        self.assertEqual(settings.upload_chunk, 500)

    def test_nonsense_numbers_fall_back(self):
        self.assertEqual(Settings.from_env({"PULLMANAGER_UPLOAD_CHUNK": "lots"}).upload_chunk, 20_000)

    def test_missing_server_or_database_is_refused(self):
        with self.assertRaises(DatabaseError):
            Settings(projects_server="", projects_database="D").projects_connection_string()
        with self.assertRaises(DatabaseError):
            Settings(projects_server="S", projects_database="").projects_connection_string()


class BatchSplitTests(unittest.TestCase):
    def test_splits_on_go(self):
        self.assertEqual(split_batches("SELECT 1\nGO\nSELECT 2"), ["SELECT 1", "SELECT 2"])

    def test_go_is_case_insensitive_and_may_be_indented(self):
        self.assertEqual(len(split_batches("SELECT 1\n  go  \nSELECT 2")), 2)

    def test_go_inside_a_statement_is_not_a_separator(self):
        self.assertEqual(len(split_batches("SELECT 'GO' AS x")), 1)

    def test_empty_batches_are_dropped(self):
        self.assertEqual(split_batches("GO\n\nGO\nSELECT 1\nGO\n"), ["SELECT 1"])

    def test_script_without_go_is_one_batch(self):
        self.assertEqual(len(split_batches("SELECT 1\nSELECT 2")), 1)


class DrainTests(unittest.TestCase):
    def test_collects_every_result_set(self):
        cursor = FakeCursor([(["A"], [(1,)]), (["B"], [(2,), (3,)])])
        cursor.execute("x")
        results = drain(cursor)
        self.assertEqual([r.columns for r in results], [["A"], ["B"]])
        self.assertEqual(results[1].rows, [(2,), (3,)])

    def test_skips_statements_that_return_nothing(self):
        # A generated script interleaves DDL and inserts with telemetry SELECTs.
        cursor = FakeCursor([None, (["A"], [(1,)]), None])
        cursor.execute("x")
        results = drain(cursor)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].columns, ["A"])

    def test_result_rows_convert_to_dicts(self):
        self.assertEqual(
            ResultSet(["A", "B"], [(1, 2)]).as_dicts(), [{"A": 1, "B": 2}]
        )


class ExecuteScriptTests(unittest.TestCase):
    def test_runs_every_batch(self):
        cursor = FakeCursor([(["A"], [(1,)])])
        outcome = execute_script(FakeConnection(cursor), "SELECT 1\nGO\nSELECT 2")
        self.assertEqual(len(cursor.executed), 2)
        self.assertEqual(len(outcome.result_sets), 2)

    def test_failure_names_the_batch_and_keeps_server_messages(self):
        # Without the messages, a failed OPENQUERY reports only a generic outer
        # error and the actual cause is lost.
        cursor = FakeCursor(
            messages=[("42000", "OLE DB provider returned message: Login timeout expired")],
            fail_on="BOOM",
        )
        with self.assertRaises(DatabaseError) as caught:
            execute_script(FakeConnection(cursor), "SELECT 1\nGO\nBOOM\nGO\nSELECT 3", label="pk")
        text = str(caught.exception)
        self.assertIn("batch 2/3", text)
        self.assertIn("pk", text)
        self.assertIn("Login timeout expired", text)

    def test_telemetry_is_selected_by_declared_shape(self):
        cursor = FakeCursor([
            (["Unrelated"], [("x",)]),
            (["DestTable", "RowCount"], [("PKTable", 12345)]),
        ])
        outcome = execute_script(FakeConnection(cursor), "SELECT 1")
        self.assertEqual(
            outcome.rows_of("DestTable", "RowCount"),
            [{"DestTable": "PKTable", "RowCount": 12345}],
        )

    def test_messages_are_formatted_as_text(self):
        cursor = FakeCursor(messages=[("01000", "row count"), ("01000", "done")])
        self.assertEqual(collect_messages(cursor), ["01000 | row count", "01000 | done"])


class ServerNameTests(unittest.TestCase):
    def test_captures_the_instance_name(self):
        cursor = FakeCursor([(["CosmosServerName"], [("et4003vpdsql032",)])])
        self.assertEqual(capture_server_name(FakeConnection(cursor)), "et4003vpdsql032")

    def test_absent_name_is_a_hard_error(self):
        # Local SQL cannot build OPENQUERY without it, so a silent fallback
        # would aim the transfer at nothing.
        with self.assertRaises(DatabaseError):
            capture_server_name(FakeConnection(FakeCursor([(["x"], [])])))


class BulkInsertTests(unittest.TestCase):
    def rows(self, count):
        return [(i,) for i in range(count)]

    def test_uses_parameter_binding_not_a_values_list(self):
        # A table value constructor is capped at 1000 rows; parameter arrays
        # are not, because the statement stays single-row.
        cursor = FakeCursor()
        bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(5))
        statement, batch = cursor.executemany_calls[0]
        self.assertEqual(statement, "INSERT INTO ##JVM_X ([Key]) VALUES (?)")
        self.assertEqual(len(batch), 5)

    def test_enables_fast_executemany(self):
        cursor = FakeCursor()
        bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(1))
        self.assertTrue(cursor.fast_executemany)

    def test_chunks_beyond_the_thousand_row_limit(self):
        cursor = FakeCursor()
        inserted = bulk_insert(
            FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(2500), chunk_size=1000
        )
        self.assertEqual(inserted, 2500)
        self.assertEqual([len(b) for _, b in cursor.executemany_calls], [1000, 1000, 500])

    def test_multi_column_placeholders(self):
        cursor = FakeCursor()
        bulk_insert(FakeConnection(cursor), "T", ["A", "B", "C"], [(1, 2, 3)])
        self.assertEqual(cursor.executemany_calls[0][0], "INSERT INTO T ([A], [B], [C]) VALUES (?, ?, ?)")

    def test_no_columns_is_refused(self):
        with self.assertRaises(DatabaseError):
            bulk_insert(FakeConnection(FakeCursor()), "T", [], [])

    def test_empty_input_inserts_nothing(self):
        cursor = FakeCursor()
        self.assertEqual(bulk_insert(FakeConnection(cursor), "T", ["A"], []), 0)
        self.assertEqual(cursor.executemany_calls, [])


class ChunkTests(unittest.TestCase):
    def test_splits_into_even_chunks_with_a_remainder(self):
        self.assertEqual([len(c) for c in chunked(range(7), 3)], [3, 3, 1])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(chunked([], 3)), [])
