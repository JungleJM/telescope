"""The launcher's controller: commands, the subprocess runner, and status rows."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from .. import launcher
from ..launcher import (
    CommandRunner,
    LauncherError,
    Options,
    Paths,
    Tools,
    child_environment,
    command_dry_run,
    command_execute,
    command_export_split,
    command_validate,
    load_settings,
    locate_tools,
    manifest_rows,
    save_settings,
    try_manifest_rows,
)
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST

TOOLS = Tools(Path("/rt/pullmanager.py"), Path("/rt/scripts/makeYaml.py"))


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class LocateToolsTests(TempDirTestCase):
    def make(self, *relative):
        for rel in relative:
            path = self.tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def test_finds_the_extracted_bundle_layout(self):
        self.make("pullmanager.py", "scripts/makeYaml.py", "pullmanager/__init__.py")
        tools = locate_tools(self.tmp / "pullmanager")
        self.assertEqual(tools.make_yaml, self.tmp / "scripts" / "makeYaml.py")

    def test_finds_the_source_tree_layout(self):
        # makeYaml sits beside pullmanager_src rather than inside it.
        self.make("pullmanager_src/pullmanager.py", "makeYaml.py",
                  "pullmanager_src/pullmanager/__init__.py")
        tools = locate_tools(self.tmp / "pullmanager_src" / "pullmanager")
        self.assertEqual(tools.make_yaml, self.tmp / "makeYaml.py")

    def test_neither_layout_is_an_error(self):
        with self.assertRaises(LauncherError):
            locate_tools(self.tmp / "pullmanager")

    def test_the_real_install_is_found(self):
        tools = locate_tools()
        self.assertTrue(tools.pullmanager.is_file())
        self.assertTrue(tools.make_yaml.is_file())


class CommandTests(unittest.TestCase):
    def test_validate_passes_every_input(self):
        paths = Paths(template="T_transfer.yaml", datadictionary="../data/d.yaml")
        command = command_validate(TOOLS, paths)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(TOOLS.make_yaml))
        self.assertEqual(
            command[2:],
            ["--template", "T_transfer.yaml", "--datadictionary", "../data/d.yaml", "--validate"],
        )

    def test_blank_optional_inputs_fall_back_to_the_bundled_copies(self):
        command = command_validate(TOOLS, Paths(template="T.yaml"))
        self.assertNotIn("--datadictionary", command)

    def test_never_passes_recipes(self):
        # D49: a transfer YAML carries its recipes; none ship to the VM.
        for build in (command_validate, command_export_split):
            with self.subTest(command=build.__name__):
                self.assertNotIn("--recipes", build(TOOLS, Paths(template="T.yaml")))

    def test_a_template_is_required(self):
        for build in (command_validate, command_export_split):
            with self.subTest(command=build.__name__):
                with self.assertRaises(LauncherError):
                    build(TOOLS, Paths(template="  "))

    def test_export_split_writes_to_the_split_folder(self):
        command = command_export_split(TOOLS, Paths(template="T.yaml", split_dir="out"))
        self.assertEqual(command[-3:], ["--export-split", "--out-dir", "out"])

    def test_dry_run_reads_the_manifest_and_writes_sql(self):
        command = command_dry_run(TOOLS, Paths(split_dir="s", sql_dir="q"), Options())
        self.assertEqual(command[1], str(TOOLS.pullmanager))
        self.assertIn("--dry-run", command)
        self.assertIn(str(Path("s") / "pullmanifest.yaml"), command)
        self.assertEqual(command[command.index("--out-dir") + 1], "q")

    def test_execute_carries_retry_failed(self):
        command = command_execute(TOOLS, Paths(split_dir="s"), Options(retry_failed=True))
        self.assertIn("--execute", command)
        self.assertIn("--retry-failed", command)

    def test_retry_is_absent_by_default(self):
        command = command_execute(TOOLS, Paths(split_dir="s"), Options())
        self.assertNotIn("--retry-failed", command)

    def test_repull_is_passed_when_chosen(self):
        self.assertIn("--repull", command_execute(TOOLS, Paths(split_dir="s"), Options(repull=True)))
        self.assertNotIn("--repull", command_execute(TOOLS, Paths(split_dir="s"), Options()))

    def test_the_launcher_cannot_request_a_partial_resume(self):
        # Removed (D52); a finished batch is kept without asking.
        self.assertNotIn("resume_partial", Options.__dataclass_fields__)
        for options in (Options(), Options(retry_failed=True), Options(repull=True)):
            with self.subTest(options=options):
                command = command_execute(TOOLS, Paths(split_dir="s"), options)
                self.assertNotIn("--resume-partial", command)

    def test_execute_passes_a_typed_split_folders_manifest(self):
        # A name finds only runs/<project>/split, so a chosen folder goes by path.
        command = command_execute(TOOLS, Paths(template="T_transfer.yaml", split_dir="s"), Options())
        self.assertEqual(command[command.index("--execute") + 1], str(Path("s") / "pullmanifest.yaml"))

    def test_its_console_is_kept_open_until_exit_is_typed(self):
        paths = Paths(template="T_transfer.yaml")
        self.assertIn("--keep-open", command_execute(TOOLS, paths, Options(), keep_open=True))
        self.assertNotIn("--keep-open", command_execute(TOOLS, paths, Options()))

    def test_child_output_is_unbuffered_utf8(self):
        # Buffered, a long pull prints nothing until it ends; without UTF-8 a
        # Windows code page mangles anything outside ASCII.
        env = child_environment()
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")


class RunFolderTests(unittest.TestCase):
    """Each project's split and SQL in its own folder (D57)."""

    SHARE = "Z:\\Project D139081\\"

    def test_blank_folders_are_the_projects_own(self):
        paths = Paths(template=self.SHARE + "IBD_Ancestry_transfer.yaml")
        split = str(Path("runs") / "IBD_Ancestry" / "split")
        self.assertEqual(command_export_split(TOOLS, paths)[-1], split)
        dry_run = command_dry_run(TOOLS, paths, Options())
        self.assertIn(str(Path(split) / "pullmanifest.yaml"), dry_run)
        self.assertEqual(dry_run[dry_run.index("--out-dir") + 1], str(Path("runs") / "IBD_Ancestry" / "sql"))
        # Execute is given the project's name, which finds that manifest (D66).
        execute = command_execute(TOOLS, paths, Options())
        self.assertEqual(execute[execute.index("--execute") + 1], "IBD_Ancestry")

    def test_the_share_folder_name_plays_no_part(self):
        # The repo sits in "Project D139081"; the project is named by its file.
        paths = Paths(template=self.SHARE + "IBD_Ancestry_transfer.yaml")
        self.assertNotIn("139081", str(paths.manifest()))
        out_dir = command_export_split(TOOLS, paths)[-1]
        self.assertEqual(out_dir, str(Path("runs") / "IBD_Ancestry" / "split"))

    def test_two_projects_never_share_a_manifest(self):
        first = Paths(template="IBD_Ancestry_transfer.yaml").manifest()
        second = Paths(template="IBD_Ancestry_v2_transfer.yaml").manifest()
        self.assertNotEqual(first, second)

    def test_a_typed_folder_still_wins(self):
        paths = Paths(template="IBD_Ancestry_transfer.yaml", split_dir="elsewhere", sql_dir="q")
        self.assertEqual(paths.manifest(), Path("elsewhere") / "pullmanifest.yaml")
        self.assertEqual(paths.sql_folder(), Path("q"))

    def test_no_transfer_yaml_and_no_folder_asks_for_one(self):
        with self.assertRaisesRegex(LauncherError, "transfer YAML"):
            command_dry_run(TOOLS, Paths(), Options())

    def test_the_rule_matches_makeyaml(self):
        # The launcher cannot import makeYaml, so it keeps a copy of the rule.
        import importlib.util

        spec = importlib.util.spec_from_file_location("makeyaml_for_rule", locate_tools().make_yaml)
        make_yaml = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = make_yaml  # its dataclasses look themselves up there
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(make_yaml)
        for name in ("IBD_Ancestry_transfer.yaml", "IBD_Ancestry_intake.yaml", "IBD_Ancestry_temp.yaml", "template.yaml",
                     "My Pull (v2).yaml", "_transfer.yaml"):
            with self.subTest(name=name):
                self.assertEqual(launcher.run_folder_name(name), make_yaml.run_folder_name(name))


class CommandRunnerTests(TempDirTestCase):
    def run_to_end(self, runner, code, timeout=15):
        runner.start([sys.executable, "-c", code], cwd=self.tmp)
        lines = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines.extend(runner.poll())
            if not runner.running:
                return lines
            time.sleep(0.02)
        self.fail("command did not finish")

    def test_streams_output_and_reports_success(self):
        runner = CommandRunner()
        lines = self.run_to_end(runner, "print('one'); print('two')")
        self.assertEqual(lines, ["one", "two"])
        self.assertEqual(runner.returncode, 0)

    def test_reports_failure(self):
        runner = CommandRunner()
        self.run_to_end(runner, "import sys; sys.exit(3)")
        self.assertEqual(runner.returncode, 3)

    def test_merges_stderr_into_the_log(self):
        runner = CommandRunner()
        lines = self.run_to_end(runner, "import sys; print('err', file=sys.stderr)")
        self.assertIn("err", lines)

    def test_non_ascii_survives(self):
        runner = CommandRunner()
        lines = self.run_to_end(runner, "print('Crohn’s – café')")
        self.assertEqual(lines, ["Crohn’s – café"])

    def test_runs_in_the_given_directory(self):
        runner = CommandRunner()
        lines = self.run_to_end(runner, "import os; print(os.getcwd())")
        self.assertEqual(Path(lines[0]).resolve(), self.tmp.resolve())

    def test_stop_terminates_a_long_command(self):
        runner = CommandRunner()
        runner.start([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.tmp)
        self.assertTrue(runner.running)
        runner.stop()
        runner.wait(timeout=10)
        runner.poll()
        self.assertFalse(runner.running)
        self.assertNotEqual(runner.returncode, 0)

    def test_one_command_at_a_time(self):
        runner = CommandRunner()
        runner.start([sys.executable, "-c", "import time; time.sleep(5)"], cwd=self.tmp)
        self.addCleanup(runner.stop)
        with self.assertRaises(LauncherError):
            runner.start([sys.executable, "-c", "pass"], cwd=self.tmp)


class ConsoleRunnerTests(TempDirTestCase):
    """D68: Execute's own window. On the Mac there is none: it runs unseen."""

    def test_runs_the_command_and_reports_its_exit_code(self):
        runner = launcher.ConsoleRunner()
        marker = self.tmp / "ran.txt"
        runner.start([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x'); raise SystemExit(3)"],
                     cwd=self.tmp)
        self.assertEqual(runner.wait(timeout=15), 3)
        self.assertFalse(runner.alive)
        self.assertTrue(marker.is_file())

    def test_stop_ends_it(self):
        runner = launcher.ConsoleRunner()
        runner.start([sys.executable, "-c", "import time; time.sleep(60)"], cwd=self.tmp)
        self.assertTrue(runner.alive)
        runner.stop()
        runner.wait(timeout=10)
        self.assertFalse(runner.alive)

    def test_windows_failures_are_shown_in_hex(self):
        self.assertEqual(launcher.exit_code_words(3221225794), "3221225794 (0xC0000142)")
        self.assertEqual(launcher.exit_code_words(1), "1")


class LogFollowerTests(TempDirTestCase):
    def test_reads_only_what_was_added(self):
        log = self.tmp / "execute-20260925-140300.log"
        log.write_text("one\n", encoding="utf-8")
        follower = launcher.LogFollower()
        self.assertEqual(follower.read(log), (True, "one\n"))
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("two\n")
        self.assertEqual(follower.read(log), (False, "two\n"))
        self.assertEqual(follower.read(log), (False, ""))

    def test_a_character_cut_by_a_read_arrives_whole(self):
        log = self.tmp / "execute.log"
        data = "Crohn’s\n".encode("utf-8")
        cut = data.index("’".encode("utf-8")) + 1
        log.write_bytes(data[:cut])
        follower = launcher.LogFollower()
        _, first = follower.read(log)
        with open(log, "ab") as handle:
            handle.write(data[cut:])
        _, second = follower.read(log)
        self.assertEqual(first + second, "Crohn’s\n")

    def test_a_new_log_starts_over(self):
        first, second = self.tmp / "a.log", self.tmp / "b.log"
        first.write_text("old\n", encoding="utf-8")
        second.write_text("new\n", encoding="utf-8")
        follower = launcher.LogFollower()
        follower.read(first)
        self.assertEqual(follower.read(second), (True, "new\n"))

    def test_the_live_executes_log_is_shown_before_a_newer_one(self):
        from ..lock import LockInfo

        manifest = self.tmp / "runs" / "P" / "split" / "pullmanifest.yaml"
        logs = self.tmp / "runs" / "P" / "logs"
        logs.mkdir(parents=True)
        running = logs / "execute-20260925-140300.log"
        refused = logs / "execute-20260925-150000.log"  # a second Execute, refused
        running.write_text("pulling\n", encoding="utf-8")
        refused.write_text("already executing\n", encoding="utf-8")
        lock = LockInfo(path=manifest.with_suffix(".lock"), pid=1, machine="VM", started=0,
                        heartbeat=time.time(), log=str(running))
        self.assertEqual(launcher.pull_log(manifest, lock), running)
        self.assertEqual(launcher.pull_log(manifest, None), refused)


class StatusRowTests(TempDirTestCase):
    def write_manifest(self, data=None):
        path = self.tmp / "split" / "pullmanifest.yaml"
        dump_yaml(data if data is not None else SAMPLE_MANIFEST, path)
        return path

    def test_one_row_per_session_phase_and_run(self):
        rows = manifest_rows(self.write_manifest())
        kinds = [row.kind for row in rows]
        self.assertEqual(kinds.count("session"), 2)
        self.assertEqual(kinds.count("phase"), 6)
        self.assertEqual(kinds.count("run"), 3)

    def test_runs_are_named_for_their_batch(self):
        rows = manifest_rows(self.write_manifest())
        self.assertIn("LA-Female", [row.name for row in rows if row.kind == "run"])

    def test_failure_detail_is_shown(self):
        import copy

        data = copy.deepcopy(SAMPLE_MANIFEST)
        run = data["sessions"][0]["runs"][0]
        run["status"] = "failed"
        run["error"] = {"message": "OPENQUERY failed", "detail": "Msg 7321"}
        rows = manifest_rows(self.write_manifest(data))
        failed = [row for row in rows if row.status == "failed"]
        self.assertEqual(failed[0].detail, "OPENQUERY failed")

    def test_rows_are_formatted(self):
        import copy

        data = copy.deepcopy(SAMPLE_MANIFEST)
        pk = data["sessions"][0]["phases"]["pk"]
        pk["rows"] = 1234567
        pk["duration"] = {"seconds": 312, "display": "5m 12s"}
        row = next(r for r in manifest_rows(self.write_manifest(data)) if r.name == "pk")
        self.assertEqual(row.rows, "1,234,567")
        self.assertEqual(row.duration, "5m 12s")

    def test_a_missing_manifest_explains_itself(self):
        rows, message = try_manifest_rows(self.tmp / "nope" / "pullmanifest.yaml")
        self.assertEqual(rows, [])
        self.assertIn("Export a split", message)

    def test_an_unreadable_manifest_does_not_raise(self):
        path = self.tmp / "pullmanifest.yaml"
        path.write_text("manifest_version: 99\nsessions: []\n", encoding="utf-8")
        rows, message = try_manifest_rows(path)
        self.assertEqual(rows, [])
        self.assertIn("Could not read", message)


class SettingsTests(TempDirTestCase):
    def test_round_trips(self):
        paths = Paths(template="IBD_transfer.yaml", datadictionary="../data/d.yaml", split_dir="out")
        save_settings(paths, self.tmp)
        self.assertEqual(load_settings(self.tmp), paths)

    def test_live_in_the_working_directory_not_the_bundle(self):
        # The extracted bundle is replaced on update, so remembered choices
        # kept inside it would be lost every time.
        path = save_settings(Paths(template="x"), self.tmp)
        self.assertEqual(path.parent, self.tmp / "runs")  # in the working folder's runs/

    def test_absent_or_corrupt_settings_give_defaults(self):
        self.assertEqual(load_settings(self.tmp), Paths())
        (self.tmp / launcher.SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(load_settings(self.tmp), Paths())

    def test_an_older_launchers_saved_defaults_become_the_projects_own(self):
        # It saved "split" and "sql" whether or not they were chosen (D57).
        (self.tmp / launcher.SETTINGS_FILENAME).write_text(
            '{"template": "IBD_Ancestry_transfer.yaml", "split_dir": "split", "sql_dir": "sql"}',
            encoding="utf-8",
        )
        loaded = load_settings(self.tmp)
        self.assertEqual((loaded.split_dir, loaded.sql_dir), ("", ""))
        self.assertEqual(loaded.manifest(), Path("runs") / "IBD_Ancestry" / "split" / "pullmanifest.yaml")

    def test_settings_from_before_d49_still_load(self):
        # Older launchers remembered a recipes file; that choice no longer exists.
        (self.tmp / launcher.SETTINGS_FILENAME).write_text(
            '{"template": "IBDTest.yaml", "recipes": "../data/recipes.yaml"}', encoding="utf-8"
        )
        self.assertEqual(load_settings(self.tmp), Paths(template="IBDTest.yaml"))

    def test_unknown_keys_are_ignored(self):
        (self.tmp / launcher.SETTINGS_FILENAME).write_text(
            '{"template": "a.yaml", "from_a_later_version": 1}', encoding="utf-8"
        )
        self.assertEqual(load_settings(self.tmp).template, "a.yaml")


class SettingsPlaceTests(unittest.TestCase):
    """Remembered paths live in runs/, not loose in the working folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

    def test_saved_under_runs(self):
        launcher.save_settings(launcher.Paths(template="A_transfer.yaml"), self.work)
        self.assertTrue((self.work / "runs" / ".pullmanager-gui.json").is_file())
        self.assertFalse((self.work / ".pullmanager-gui.json").exists())
        self.assertEqual(launcher.load_settings(self.work).template, "A_transfer.yaml")

    def test_an_old_file_at_the_top_is_moved_and_still_read(self):
        (self.work / ".pullmanager-gui.json").write_text('{"template": "Old_transfer.yaml"}', encoding="utf-8")
        self.assertEqual(launcher.load_settings(self.work).template, "Old_transfer.yaml")
        self.assertFalse((self.work / ".pullmanager-gui.json").exists())
        self.assertTrue((self.work / "runs" / ".pullmanager-gui.json").is_file())

    def test_a_newer_file_in_runs_wins_and_the_old_is_removed(self):
        launcher.save_settings(launcher.Paths(template="New_transfer.yaml"), self.work)
        (self.work / ".pullmanager-gui.json").write_text('{"template": "Old_transfer.yaml"}', encoding="utf-8")
        self.assertEqual(launcher.load_settings(self.work).template, "New_transfer.yaml")
        self.assertFalse((self.work / ".pullmanager-gui.json").exists())


class ConfigTests(unittest.TestCase):
    """D111: the runtime reads datascope.json as makeYaml does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_its_defaults_are_makeyamls(self):
        import importlib.util
        from .. import config

        spec = importlib.util.spec_from_file_location("makeyaml_for_config", locate_tools().make_yaml)
        make_yaml = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = make_yaml
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(make_yaml)
        self.assertEqual(config.CORE_DEFAULTS, make_yaml.CORE_DEFAULTS)
        self.assertEqual(config.RUNS_DEFAULT, str(make_yaml.RUNS_DIR))
        self.assertEqual(config.CONFIG_NAME, make_yaml.CONFIG_NAME)

    def test_pulls_are_found_under_the_runs_folder_it_names(self):
        from .. import pulls

        (self.home / "datascope.json").write_text('{"runs": "cleanup/runs"}', encoding="utf-8")
        manifest = self.home / "cleanup" / "runs" / "IBD" / "split" / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, manifest)
        self.assertEqual(pulls.resolve("IBD", self.home).resolve(), manifest.resolve())
        command, folder = pulls.execute_command(manifest, self.home)
        self.assertEqual((command, folder), ("python pullmanager.py --execute IBD", self.home.resolve()))
        self.assertTrue((launcher.save_settings(launcher.Paths(template="IBD_transfer.yaml"), self.home)
                         ).is_relative_to(self.home / "cleanup" / "runs"))

    def test_without_it_runs_are_in_the_working_folder(self):
        from .. import config

        self.assertEqual(config.runs_dir(self.home), self.home / "runs")
