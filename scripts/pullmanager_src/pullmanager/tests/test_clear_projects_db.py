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


class SpaceDatabase(FakeDatabase):
    """A database that also answers for its files and its log, or cannot be opened."""

    def __init__(self, tables, files=(("ROWS", 18000, 20000),)):
        super().__init__(tables)
        self.files = files
        self.closed = False

    def cursor(self):
        db = self

        class Cursor(FakeCursor):
            def execute(self, sql):
                if "sys.database_files" in sql:
                    self.rows = [(f"f{i}", kind, used, used, cap) for i, (kind, used, cap) in enumerate(db.files)]
                    return self
                if "sys.databases" in sql:
                    self.rows = [("SIMPLE", "NOTHING")]
                    return self
                return super().execute(sql)

            def fetchone(self):
                return self.rows[0] if self.rows else None

        return Cursor(db)

    def close(self):
        self.closed = True


class EveryDatabaseTests(unittest.TestCase):
    """D185: every listed database with its room, its tables grouped by pull."""

    def setUp(self):
        self.tool = load()

    def test_the_listed_databases_are_pullmanagers_own(self):
        from pullmanager.config import DEFAULT_PROJECTS_DATABASES

        self.assertEqual(self.tool.listed_databases(), list(DEFAULT_PROJECTS_DATABASES))

    def test_tables_group_by_their_pulls_prefix_largest_first(self):
        tables = [("dbo", "ucvis_MedAdminHistory", 51_187_764, 9051), ("dbo", "hatphe_hat_Labs", 10, 1),
                  ("dbo", "ucvis_upload_IBD_Meds", 715, 0), ("dbo", "Scratch", 3, 0)]
        grouped = self.tool.by_pull(tables)
        self.assertEqual([pull for pull, _ in grouped], ["ucvis", "hatphe", "(no prefix)"])
        self.assertEqual([r[1] for r in grouped[0][1]], ["ucvis_MedAdminHistory", "ucvis_upload_IBD_Meds"])

    def test_free_space_is_the_data_files_room_to_their_caps(self):
        db = SpaceDatabase([], files=(("ROWS", 18000, 20000), ("ROWS", 500, 1000), ("LOG", 100, 20000)))
        self.assertEqual(self.tool.free_mb(db), 2500.0)
        self.assertIsNone(self.tool.free_mb(SpaceDatabase([], files=(("ROWS", 1, None),))))

    def test_a_database_that_cannot_be_opened_says_why_and_the_others_load(self):
        catalog = self.tool.Catalog()
        good = SpaceDatabase([("ucvis_EDVisits", 500)])

        def connect(name):
            if name == "PROJECTDBAD":
                raise RuntimeError("Login failed for user (18456)\nmore")
            return good

        for name in ("PROJECTDBAD", "PROJECTDGOOD"):
            catalog.load(name, connect)
        self.assertEqual(catalog.row_text("PROJECTDBAD"), "could not be opened (Login failed for user (18456))")
        self.assertEqual(catalog.row_text("PROJECTDGOOD"), "2.0 GB free, 1 table(s)")
        self.assertTrue(good.closed)

    def test_a_selection_means_its_tables_a_pulls_or_a_whole_databases(self):
        catalog = self.tool.Catalog()
        catalog.tables = {
            "PROJECTDA": [("dbo", "ucvis_A", 1, 1), ("dbo", "ucvis_B", 1, 1), ("dbo", "hat_C", 1, 1)],
            "PROJECTDB": [("dbo", "x_D", 1, 1)],
        }
        t = self.tool.targets
        self.assertEqual(t(["table\x00PROJECTDA\x00dbo\x00hat_C"], catalog), {"PROJECTDA": [("dbo", "hat_C")]})
        self.assertEqual(t(["pull\x00PROJECTDA\x00ucvis"], catalog),
                         {"PROJECTDA": [("dbo", "ucvis_A"), ("dbo", "ucvis_B")]})
        self.assertEqual(t(["db\x00PROJECTDB", "pull\x00PROJECTDA\x00ucvis", "table\x00PROJECTDA\x00dbo\x00ucvis_A"],
                           catalog),
                         {"PROJECTDB": [("dbo", "x_D")], "PROJECTDA": [("dbo", "ucvis_A"), ("dbo", "ucvis_B")]})


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
