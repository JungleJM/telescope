"""Adapter behaviour, exercised against a fake cursor.

pyodbc is not installed on the development machine and there is no database to
reach, so the DB-API interactions are faked. What is tested is the logic the
old generator got right and that is easy to get wrong: GO splitting, draining
every result set, and keeping server messages on the failure path.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ..db import (
    DEFAULT_DRIVER,
    find_env_file,
    load_env_file,
    parse_env_file,
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


class LateFailureCursor(FakeCursor):
    """An exception among the statements is raised when nextset() reaches it."""

    def nextset(self):
        if not self._pending:
            return False
        if isinstance(self._pending[0], Exception):
            raise self._pending.pop(0)
        self._advance()
        return True


class PerStatementMessages:
    """Each statement carries its own messages, as pyodbc's cursor does."""

    def __init__(self, statements):
        self._statements = statements
        self._index = 0

    def execute(self, sql, params=None):
        self._index = 0

    @property
    def messages(self):
        return [("01000", text) for text in self._statements[self._index][1]]

    @property
    def description(self):
        return None

    def nextset(self):
        if self._index + 1 >= len(self._statements):
            return False
        self._index += 1
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
        # Both hosts are DNS aliases, so an empty .env still connects.
        settings = Settings.from_env({})
        self.assertEqual(settings.driver, DEFAULT_DRIVER)
        self.assertEqual(settings.cosmos_server, "COSMOS")
        self.assertEqual(settings.projects_server, "PROJECTS")

    def test_no_configuration_at_all_still_builds_both_strings(self):
        settings = Settings.from_env({})
        self.assertIn("Server=tcp:COSMOS;", settings.cosmos_connection_string())
        self.assertIn(
            "Server=tcp:PROJECTS;", settings.projects_connection_string("PROJECTD33A929")
        )

        settings = Settings.from_env({
            "PULLMANAGER_COSMOS_SERVER": "OTHER",
            "PULLMANAGER_UPLOAD_CHUNK": "500",
        })
        self.assertEqual(settings.cosmos_server, "OTHER")
        self.assertEqual(settings.upload_chunk, 500)

    def test_nonsense_numbers_fall_back(self):
        self.assertEqual(Settings.from_env({"PULLMANAGER_UPLOAD_CHUNK": "lots"}).upload_chunk, 20_000)

    def test_an_explicitly_blank_server_or_database_is_refused(self):
        with self.assertRaises(DatabaseError):
            Settings(projects_server="", projects_database="D").projects_connection_string()
        with self.assertRaises(DatabaseError):
            Settings(projects_server="S", projects_database="").projects_connection_string()


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))

    def write(self, text):
        path = self.dir / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_the_usual_shapes(self):
        parsed = parse_env_file(
            "# a comment\n"
            "PULLMANAGER_COSMOS_SERVER=COSMOS\n"
            "export PULLMANAGER_PROJECTS_SERVER=\"PROJ SRV\"\n"
            "PULLMANAGER_UPLOAD_CHUNK = 5000\n"
            "\n"
            "EMPTY=\n"
            "not-an-assignment\n"
        )
        self.assertEqual(parsed["PULLMANAGER_COSMOS_SERVER"], "COSMOS")
        self.assertEqual(parsed["PULLMANAGER_PROJECTS_SERVER"], "PROJ SRV")
        self.assertEqual(parsed["PULLMANAGER_UPLOAD_CHUNK"], "5000")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("not-an-assignment", parsed)

    def test_loads_into_the_environment(self):
        import os

        os.environ.pop("PULLMANAGER_COSMOS_SERVER", None)
        path = self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n")
        load_env_file(path)
        self.assertEqual(Settings.from_env().cosmos_server, "FROMFILE")

    def test_a_real_environment_variable_wins(self):
        import os

        os.environ["PULLMANAGER_COSMOS_SERVER"] = "FROMENV"
        load_env_file(self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n"))
        self.assertEqual(os.environ["PULLMANAGER_COSMOS_SERVER"], "FROMENV")

    def test_override_lets_the_file_win(self):
        import os

        os.environ["PULLMANAGER_COSMOS_SERVER"] = "FROMENV"
        load_env_file(self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n"), override=True)
        self.assertEqual(os.environ["PULLMANAGER_COSMOS_SERVER"], "FROMFILE")

    def test_a_named_file_that_is_missing_is_an_error(self):
        # Silently ignoring it would surface later as "No server configured".
        with self.assertRaises(DatabaseError):
            find_env_file(self.dir / "nope.env")

    def test_no_env_file_anywhere_is_not_an_error(self):
        self.assertEqual(load_env_file(None), {}) if find_env_file() is None else None


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

    def test_a_later_statement_that_fails_fails_the_batch(self):
        # pyodbc raises a later statement's error at nextset(); drain used to
        # take it for the end of the results, and the batch passed (D151).
        cursor = LateFailureCursor([(["a"], [(1,)]), RuntimeError("Divide by zero"), (["c"], [(2,)])])
        cursor.execute("SELECT 1 AS a; SELECT 1/0 AS b; SELECT 2 AS c;")
        with self.assertRaises(RuntimeError):
            drain(cursor)

    def test_through_execute_script_it_is_a_database_error(self):
        cursor = LateFailureCursor([(["a"], [(1,)]), RuntimeError("Divide by zero")])
        with self.assertRaises(DatabaseError) as caught:
            execute_script(FakeConnection(cursor), "SELECT 1 AS a; SELECT 1/0 AS b;", label="probe")
        self.assertIn("Divide by zero", str(caught.exception))

    def test_every_statements_messages_are_kept(self):
        cursor = PerStatementMessages([(None, ["first"]), (None, ["second"])])
        outcome = execute_script(FakeConnection(cursor), "A; B;")
        self.assertEqual(outcome.messages, ["01000 | first", "01000 | second"])

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


class ConnectFailureTests(unittest.TestCase):
    """A refused connection is a readable DatabaseError, not a traceback."""

    def fake_pyodbc(self, message):
        import sys
        import types

        module = types.ModuleType("pyodbc")

        class Error(Exception):
            pass

        def connect(*_, **__):
            raise Error("28000", message)

        module.Error = Error
        module.connect = connect
        previous = sys.modules.get("pyodbc")
        sys.modules["pyodbc"] = module
        self.addCleanup(lambda: sys.modules.pop("pyodbc") if previous is None
                        else sys.modules.__setitem__("pyodbc", previous))

    def test_a_database_that_cannot_be_opened_names_itself_and_project_db(self):
        from ..db import DatabaseError, Settings, connect

        self.fake_pyodbc('Cannot open database "PROJECTD93A57" requested by the login.')
        string = Settings().projects_connection_string("PROJECTD93A57")
        with self.assertRaises(DatabaseError) as caught:
            connect(string)
        message = str(caught.exception)
        self.assertIn("PROJECTS, database PROJECTD93A57", message)
        self.assertIn("project_db", message)
