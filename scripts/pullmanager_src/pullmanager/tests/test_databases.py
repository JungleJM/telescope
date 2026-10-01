"""A pull is given a Projects database of its own before it first runs (D164)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from .. import config, databases
from ..db import Settings
from ..manifest import Manifest
from ..yaml_io import dump_yaml, load_yaml

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"
GB = 1024


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        assert "sys.database_files" in sql, sql

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        pass


def data_file(used_gb: float, cap_gb: float | None):
    return ("ROWS", used_gb * GB, None if cap_gb is None else cap_gb * GB)


class ChoiceTestCase(unittest.TestCase):
    def setUp(self):
        if not FIXTURES.is_dir():
            self.skipTest(f"fixtures not found at {FIXTURES}")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = Path(self._tmp.name) / "runs"
        self.run = self.runs / "Infant_RSV"
        shutil.copytree(FIXTURES, self.run)
        data = load_yaml(self.run / "pullmanifest.yaml")
        data.pop("database_choice")
        data["project"]["project_db"] = "PROJECTD93A5E7"
        dump_yaml(data, self.run / "pullmanifest.yaml")
        self.measured: list[str] = []
        self.said: list[str] = []
        # Free space: D93A5E7 nearly full, D33A929 roomy, D723D95 roomier.
        self.files = {
            "PROJECTD93A5E7": [data_file(19, 20), ("LOG", 1, None)],
            "PROJECTD33A929": [data_file(2, 20)],
            "PROJECTD723D95": [data_file(1, 20)],
        }

    def connect(self, conn_str, **_):
        name = conn_str.split("Database=")[1].split(";")[0]
        self.measured.append(name)
        if name not in self.files:
            raise RuntimeError(f"Cannot open database \"{name}\" requested by the login.")
        return FakeConnection(self.files[name])

    def manifest(self) -> Manifest:
        return Manifest.load(self.run / "pullmanifest.yaml")

    def choose(self, names=None) -> bool:
        home = Path(self._tmp.name)
        (home / config.CONFIG_NAME).write_text(
            json.dumps({"projects_databases": names or list(self.files)}), encoding="utf-8")
        return databases.choose_database(self.manifest(), Settings(projects_server="PROJ"),
                                         self.connect, say=self.said.append, home=home)

    def other_pull(self, name: str, database: str, how: str = "stopped with errors",
                   packaged: bool = False) -> None:
        folder = self.runs / name
        shutil.copytree(FIXTURES, folder)
        data = load_yaml(folder / "pullmanifest.yaml")
        data["project"]["project_db"] = database
        data["last_execute"] = {"started_at": "x", "ended_at": "y", "how": how}
        if how == "finished":
            for session in data["sessions"]:
                for node in [*session["phases"].values(), *session["runs"]]:
                    node["status"] = "done"
        dump_yaml(data, folder / "pullmanifest.yaml")
        if packaged:
            (folder / "contents.md").write_text("# contents\n", encoding="utf-8")


class ChooseTests(ChoiceTestCase):
    def test_a_full_default_is_passed_over_for_the_roomiest(self):
        # Two of three pulls stopped when PROJECTD93A5E7 filled (20 GB).
        self.assertTrue(self.choose())
        manifest = self.manifest()
        self.assertEqual(manifest.project["project_db"], "PROJECTD723D95")
        # Every phase and run document lands its tables there too.
        for session in manifest.sessions:
            for node in session.children:
                self.assertEqual(load_yaml(manifest.resolve(node))["project_db"], "PROJECTD723D95")
        self.assertEqual(manifest.data["database_choice"]["database"], "PROJECTD723D95")
        self.assertIn("PROJECTD93A5E7", manifest.data["database_choice"]["free"])

    def test_the_blueprints_own_is_kept_when_it_has_room_and_no_other_pull(self):
        self.files["PROJECTD93A5E7"] = [data_file(1, 20)]
        self.files["PROJECTD723D95"] = [data_file(0, 20)]
        self.assertTrue(self.choose())
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD93A5E7")

    def test_one_an_unfinished_pull_uses_is_passed_over(self):
        self.other_pull("UC_Visits", "PROJECTD723D95")
        self.assertTrue(self.choose())
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD33A929")

    def test_a_finished_and_packaged_pull_does_not_count(self):
        self.other_pull("Celiac", "PROJECTD723D95", how="finished", packaged=True)
        self.assertTrue(self.choose())
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD723D95")

    def test_with_every_database_in_use_they_stack_on_the_roomiest(self):
        self.other_pull("A", "PROJECTD33A929")
        self.other_pull("B", "PROJECTD723D95")
        self.assertTrue(self.choose(["PROJECTD33A929", "PROJECTD723D95"]))
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD723D95")
        self.assertIn("shares, with B", self.manifest().data["database_choice"]["why"])

    def test_with_none_over_6_gb_nothing_is_chosen_and_each_is_listed(self):
        self.files = {"PROJECTD93A5E7": [data_file(19, 20)], "PROJECTD33A929": [data_file(15, 20)]}
        self.assertFalse(self.choose())
        self.assertNotIn("database_choice", self.manifest().data)
        text = "\n".join(self.said)
        self.assertIn("PROJECTD93A5E7: 1.0 GB free", text)
        self.assertIn("PROJECTD33A929: 5.0 GB free", text)

    def test_one_that_cannot_be_opened_is_skipped_and_said(self):
        self.assertTrue(self.choose(["PROJECTD03DEC", "PROJECTD33A929"]))
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD33A929")
        self.assertIn("PROJECTD03DEC: could not be opened", "\n".join(self.said))

    def test_once_chosen_it_never_changes(self):
        self.assertTrue(self.choose())
        self.measured.clear()
        self.files["PROJECTD723D95"] = [data_file(20, 20)]
        self.assertTrue(self.choose())
        self.assertEqual(self.measured, [])
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD723D95")

    def test_a_pull_that_has_run_is_not_moved(self):
        # Split before D164 and run: its finished batches are where it is (D52).
        manifest = self.manifest()
        manifest.sessions[0].children[0].status = "done"
        manifest.save()
        self.assertTrue(self.choose())
        self.assertEqual(self.measured, [])
        self.assertEqual(self.manifest().project["project_db"], "PROJECTD93A5E7")


class ExecuteTests(ChoiceTestCase):
    def test_execute_builds_nothing_when_no_database_has_room(self):
        import argparse
        import contextlib
        import io
        from unittest import mock

        from .. import cli

        class Anything:
            def cursor(self):
                return self

            def execute(self, sql, params=None):
                return self

            def fetchall(self):
                return []

            def close(self):
                pass

        def connect(conn_str, **kwargs):
            if "Database=PROJECTD" in conn_str:
                return self.connect(conn_str, **kwargs)
            return Anything()  # Cosmos, read for its refresh stamps

        self.files = {name: [data_file(19, 20)] for name in config.DEFAULT_PROJECTS_DATABASES}
        args = argparse.Namespace(env=None, repull=False, retry_failed=False, repull_session=None)
        out = io.StringIO()
        with mock.patch.object(databases, "config_home", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.execute(self.manifest(), args, connect_fn=connect)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("No Projects database has more than 6 GB free", out.getvalue())
        self.assertNotIn("=== Patients ===", out.getvalue())
        self.assertEqual(len(self.measured), 10)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_without_a_list_the_users_ten_with_the_default_first(self):
        names = config.projects_databases(self.home)
        self.assertEqual(len(names), 10)
        self.assertEqual(names[0], "PROJECTD93A5E7")
        self.assertIn("PROJECTD03DEC", names)

    def test_setting_the_backup_keeps_the_list_a_list(self):
        (self.home / config.CONFIG_NAME).write_text(
            json.dumps({"projects_databases": ["PROJECTD1", "PROJECTD2"]}), encoding="utf-8")
        config.set_backup(self.home, self.home / "backup")
        self.assertEqual(config.projects_databases(self.home), ["PROJECTD1", "PROJECTD2"])

    def test_makeyaml_reads_the_same_keys(self):
        import importlib.util

        from ..launcher import locate_tools

        spec = importlib.util.spec_from_file_location("makeyaml_for_keys", locate_tools().make_yaml)
        make_yaml = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = make_yaml  # its dataclasses look themselves up there
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(make_yaml)
        self.assertEqual(tuple(make_yaml.CONFIG_KEYS), tuple(config.CONFIG_KEYS))
