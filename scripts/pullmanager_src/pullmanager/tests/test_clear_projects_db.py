"""utils/clear_projects_db.py: what is left in the database after a drop."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "utils" / "manager" / "clear_projects_db.py"


def load():
    spec = importlib.util.spec_from_file_location("clear_projects_db", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDatabase:
    """Tables and foreign keys; a DROP TABLE a key still points at fails, as SQL Server's does."""

    def __init__(self, tables, keys=(), locked=()):
        self.tables = {name: size for name, size in tables}
        self.keys = {name: (parent, referenced) for name, parent, referenced in keys}
        self.locked = set(locked)

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def execute(self, sql):
        db = self.db
        if "sys.foreign_keys" in sql:
            self.rows = [("dbo", p, k, "dbo", r) for k, (p, r) in db.keys.items()]
        elif "FROM sys.tables" in sql:
            self.rows = [("dbo", t, 1, mb) for t, mb in sorted(db.tables.items(), key=lambda x: -x[1])]
        elif sql.startswith("ALTER TABLE"):
            del db.keys[sql.rsplit("DROP CONSTRAINT ", 1)[1][1:-1]]
        elif sql.startswith("DROP TABLE"):
            table = sql[len("DROP TABLE [dbo]."):][1:-1].replace("]]", "]")
            if table in db.locked:
                raise RuntimeError("[HY000] Lock request time out period exceeded. (1222)")
            if any(r == table for _, r in db.keys.values()):
                raise RuntimeError(f"Could not drop object 'dbo.{table}' because it is referenced "
                                   "by a FOREIGN KEY constraint. (3726)")
            del db.tables[table]
        return self

    def fetchall(self):
        return self.rows


class DropTests(unittest.TestCase):
    def setUp(self):
        self.tool = load()
        self.said = []

    def test_dropping_selected_tables_leaves_the_rest(self):
        db = FakeDatabase([("PKTable", 900), ("EDVisits", 500), ("upload_Meds", 3)])
        dropped, failures = self.tool.drop_tables(db, [("dbo", "EDVisits")], self.said.append)
        self.assertEqual((dropped, failures), (1, []))
        self.assertEqual(set(db.tables), {"PKTable", "upload_Meds"})

    def test_a_key_pointing_at_a_chosen_table_is_dropped_first(self):
        db = FakeDatabase([("PKTable", 900), ("EDVisits", 500)],
                          keys=[("FK_visits_pk", "EDVisits", "PKTable")])
        dropped, failures = self.tool.drop_tables(db, [("dbo", "PKTable")], self.said.append)
        self.assertEqual((dropped, failures), (1, []))
        self.assertEqual(set(db.tables), {"EDVisits"})
        self.assertEqual(db.keys, {})

    def test_a_locked_table_is_reported_as_blocked_and_the_rest_still_drop(self):
        db = FakeDatabase([("PKTable", 900), ("Weird]Name", 5), ("EDVisits", 500)], locked={"EDVisits"})
        tables = [("dbo", t) for t in ("PKTable", "Weird]Name", "EDVisits")]
        dropped, failures = self.tool.drop_tables(db, tables, self.said.append)
        self.assertEqual(dropped, 2)
        self.assertEqual(set(db.tables), {"EDVisits"})
        self.assertEqual(len(failures), 1)
        self.assertIn("blocked", failures[0])
        self.assertIn("SSMS", failures[0])

    def test_only_a_projects_database_is_accepted(self):
        self.assertEqual(self.tool.check_database(" PROJECTD93A5E7 "), "PROJECTD93A5E7")
        with self.assertRaisesRegex(ValueError, "must start with PROJECTD"):
            self.tool.check_database("COSMOS")


class SettingsTests(unittest.TestCase):
    def test_the_environment_wins_over_a_env_and_defaults_fill_the_rest(self):
        tool = load()
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, ".env").write_text(
                "# comment\nPULLMANAGER_PROJECTS_SERVER=FROMFILE\n"
                "PULLMANAGER_PROJECTS_DATABASE='PROJECTD1'\n", encoding="utf-8")
            got = tool.settings(env={"PULLMANAGER_PROJECTS_SERVER": "FROMENV"}, folders=[Path(folder)])
        self.assertEqual(got, {"server": "FROMENV", "database": "PROJECTD1",
                               "driver": tool.DEFAULT_DRIVER})


if __name__ == "__main__":
    unittest.main()
