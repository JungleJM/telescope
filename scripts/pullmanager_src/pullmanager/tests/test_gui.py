"""The launcher window, built against a fake tkinter.

There is no display on the development machine, and tests must never open a
real window anyway, so tkinter is replaced with stand-ins: variables behave
like variables, widgets accept anything. That exercises the view's own wiring
-- handlers, settings, the run-and-poll loop -- but not Tk itself. Option names
and layout only a real Tk can check, which means the VM.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST


class FakeVar:
    def __init__(self, master=None, value=None):
        self._value = "" if value is None else value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FreshWidgets(types.ModuleType):
    """Every widget class yields a new mock, as real widgets are distinct.

    A plain MagicMock class returns the same object from every call, which
    would make every button one button.
    """

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        # `options` keeps what the widget was made with (its text, say).
        return lambda *args, **kwargs: mock.MagicMock(name=name, options=kwargs)


def fake_tkinter():
    tk = types.ModuleType("tkinter")
    tk.Tk = mock.MagicMock
    tk.StringVar = FakeVar
    tk.BooleanVar = FakeVar
    modules = {"tkinter": tk}
    for name in ("ttk", "scrolledtext"):
        sub = FreshWidgets(f"tkinter.{name}")
        setattr(tk, name, sub)
        modules[f"tkinter.{name}"] = sub
    for name in ("filedialog", "messagebox"):
        sub = mock.MagicMock(name=f"tkinter.{name}")
        setattr(tk, name, sub)
        modules[f"tkinter.{name}"] = sub
    return modules


class GuiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved: Windows can name a temporary folder short (SHDW_0~1) or in full.
        self.work = Path(self._tmp.name).resolve()

        # Swap in only the tkinter entries. patch.dict would restore the whole
        # module table on cleanup, dropping anything first imported during the
        # test while its parent package kept a stale attribute -- leaving two
        # copies of a module, and two exception classes that cannot catch each
        # other.
        self.modules = fake_tkinter()
        saved = {name: sys.modules.get(name) for name in self.modules}
        sys.modules.update(self.modules)

        def restore():
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)
        package = __name__.rsplit(".", 2)[0]
        sys.modules.pop(f"{package}.gui", None)
        self.addCleanup(sys.modules.pop, f"{package}.gui", None)
        self.gui = importlib.import_module(f"{package}.gui")
        self.messagebox = self.modules["tkinter.messagebox"]

        from ..launcher import locate_tools
        self.app = self.gui.LauncherApp(mock.MagicMock(), locate_tools(), self.work)

    def finish(self, timeout=30):
        deadline = time.monotonic() + timeout
        while self.app.runner.running and time.monotonic() < deadline:
            self.app.poll()
            time.sleep(0.02)
        self.app.poll()
        self.assertFalse(self.app.runner.running, "command did not finish")

    def written(self):
        return "".join(call.args[1] for call in self.app.output.insert.call_args_list)


class ConstructionTests(GuiTestCase):
    def test_builds_a_field_for_every_input(self):
        # No split or SQL folder: always runs/<project>/split and /sql (D126).
        # No data dictionary either: the bundle's, always (D150).
        self.assertEqual(set(self.app.vars), {"template"})

    def test_starts_from_the_defaults(self):
        # Blank: the project's own runs/<project>/ folders (D57).
        self.assertEqual(self.app.paths().split_dir, "")
        self.assertEqual(self.app.paths().sql_dir, "")

    def test_restores_remembered_choices(self):
        from ..launcher import Paths, save_settings

        save_settings(Paths(template="IBD_transfer.yaml"), self.work)
        from ..launcher import locate_tools
        app = self.gui.LauncherApp(mock.MagicMock(), locate_tools(), self.work)
        self.assertEqual(app.vars["template"].get(), "IBD_transfer.yaml")


class ActionTests(GuiTestCase):
    def test_validate_without_a_template_warns_instead_of_running(self):
        self.app.on_validate()
        self.messagebox.showwarning.assert_called_once()
        self.assertFalse(self.app.runner.running)

    def test_validate_runs_the_real_command_and_streams_its_output(self):
        # A template that does not exist still exercises the whole path:
        # build the command, start it, stream the output, notice the end.
        self.app.vars["template"].set("no_such_template.yaml")
        self.app.on_validate()
        self.finish()
        output = self.written()
        self.assertIn("=== Validate ===", output)
        self.assertIn("template_not_found", output)
        self.assertIn("exit code 1", output)

    def test_buttons_are_disabled_while_running_and_restored_after(self):
        self.app.vars["template"].set("no_such_template.yaml")
        self.app.on_validate()
        self.app.action_buttons[0].configure.assert_any_call(state="disabled")
        self.finish()
        self.app.action_buttons[0].configure.assert_called_with(state="normal")

    def test_running_saves_the_choices(self):
        from ..launcher import load_settings

        self.app.vars["template"].set("IBDTest.yaml")
        self.app.on_validate()
        self.finish()
        self.assertEqual(load_settings(self.work).template, "IBDTest.yaml")

    def test_execute_asks_before_touching_the_databases(self):
        self.messagebox.askokcancel.return_value = False
        self.app.vars["template"].set("x.yaml")
        self.app.on_execute()
        self.assertFalse(self.app.runner.running)
        self.messagebox.askokcancel.assert_called_once()


class NameTests(GuiTestCase):
    """D71: the tab and the button say what they are for."""

    def test_the_buttons(self):
        self.assertEqual(
            [button.options["text"] for button in self.app.action_buttons],
            ["Validate", "Export split", "Preview SQL", "Execute", "Artifacts", "Scan runs",
             "Audit dictionary"],
        )

    def test_the_tabs(self):
        tabs = [call.kwargs["text"] for call in self.app.notebook.add.call_args_list]
        self.assertEqual(tabs[0], "Validation Output")
        self.assertIn("Status", tabs)

    def test_artifacts_packages_the_loaded_pull_by_name(self):
        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        with mock.patch.object(self.app.runner, "start") as start:
            self.app.on_artifacts()
        command = start.call_args.args[0]
        self.assertEqual(command[command.index("--artifacts") + 1], "IBD_Ancestry")

    def test_the_preview_runs_the_dry_run(self):
        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        with mock.patch.object(self.app.runner, "start") as start:
            self.app.on_dry_run()
        self.assertIn("--dry-run", start.call_args.args[0])
        self.assertIn("=== Preview SQL ===", self.written())


class RunningPullTests(GuiTestCase):
    """D67: while the loaded pull executes, what would overwrite it is grey."""

    def setUp(self):
        super().setUp()
        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        # Execute must not open a real window: on Windows it holds the folder open.
        self.app.console = FakeConsole()

    def lock(self, manifest=None, heartbeat_age=20):
        import json

        from ..lock import lock_path

        now = time.time()
        lock_path(manifest or self.manifest).write_text(json.dumps(
            {"pid": 4242, "machine": "VM", "started": now - 600, "heartbeat": now - heartbeat_age}
        ), encoding="utf-8")

    def states(self):
        return {text: button.configure.call_args.kwargs["state"]
                for text, button in self.app.buttons.items()}

    def test_start_run_lists_projects_and_leaves_out_the_running_one(self):
        # D126: two dropdowns; a pull executing is under Running pulls only.
        for name in ("IBD_Ancestry_transfer.yaml", "Celiac_transfer.yaml"):
            (self.work / name).write_text("cohorts: []\n", encoding="utf-8")
        self.assertEqual(self.app.running_choices(), {})
        start = self.app.start_choices()
        self.assertEqual(list(start), ["Celiac  (not run yet)", "IBD_Ancestry  (not started)"])
        self.lock()
        running = self.app.running_choices()
        self.assertEqual(len(running), 1)
        label = next(iter(running))
        self.assertTrue(label.startswith("IBD_Ancestry: executing since"), label)
        self.assertEqual(list(self.app.start_choices()), ["Celiac  (not run yet)"])
        self.app.vars["template"].set("")
        self.app.choose_running(label)
        self.assertEqual(Path(self.app.vars["template"].get()), self.work / "IBD_Ancestry_transfer.yaml")
        self.app.choose_start("Celiac  (not run yet)")
        self.assertEqual(Path(self.app.vars["template"].get()), self.work / "Celiac_transfer.yaml")

    def test_a_pull_that_has_run_is_under_finished_and_stopped_only(self):
        # D140: three dropdowns; a pull that has run leaves Start run.
        import copy

        for name in ("IBD_Ancestry_transfer.yaml", "Celiac_transfer.yaml"):
            (self.work / name).write_text("cohorts: []\n", encoding="utf-8")
        data = copy.deepcopy(SAMPLE_MANIFEST)
        for session in data["sessions"]:
            for node in [*session["phases"].values(), *session["runs"]]:
                node["status"] = "done"
        data["last_execute"] = {"started_at": "x", "ended_at": "y", "exit_code": 0, "how": "finished"}
        dump_yaml(data, self.manifest)
        self.assertEqual(list(self.app.ended_choices()), ["IBD_Ancestry  (finished)"])
        self.assertEqual(list(self.app.start_choices()), ["Celiac  (not run yet)"])
        self.app.vars["template"].set("")
        self.app.choose_ended("IBD_Ancestry  (finished)")
        self.assertEqual(Path(self.app.vars["template"].get()), self.work / "IBD_Ancestry_transfer.yaml")
        # Executing, it is under Running pulls instead.
        self.lock()
        self.assertEqual(self.app.ended_choices(), {})

    def test_stop_records_that_the_user_stopped_it(self):
        from ..manifest import Manifest

        data = dict(SAMPLE_MANIFEST, last_execute={"started_at": "x", "ended_at": None})
        dump_yaml(data, self.manifest)
        self.lock()
        self.app.console.start(["python", "scope.py", "--execute", "IBD_Ancestry"])
        self.messagebox.askyesno.return_value = True
        self.app.on_stop()
        self.assertTrue(self.app.console.stopped)
        self.assertEqual(Manifest.load(self.manifest).last_execute["how"], "stopped by user")

    def test_the_backup_line_says_where_backups_go(self):
        # D149: set in Run, kept in datascope.json, runs/backup when unreachable.
        import json

        from .. import config

        self.assertIn("none set", self.app.backup_found())
        self.assertIn(str(self.work / "runs" / "backup"), self.app.backup_found())
        drive = self.work / "other_drive"
        self.app.set_backup(drive)
        self.assertEqual(json.loads((self.work / "datascope.json").read_text())["backup"], str(drive))
        self.assertIn("NOT FOUND", self.app.backup_found())
        drive.mkdir()
        self.assertEqual(self.app.backup_found(), str(drive))
        self.assertEqual(config.backup_dir(self.work), drive)
        self.app.set_backup(None)
        self.assertNotIn("backup", json.loads((self.work / "datascope.json").read_text()))

    def test_back_up_all_runs_the_backup_command(self):
        with mock.patch.object(self.app.runner, "start") as start:
            self.app.on_backup_all()
        self.assertIn("--backup", start.call_args.args[0])

    def test_a_live_pull_greys_export_split_and_execute_only(self):
        self.lock()
        self.app.watch_pull()
        self.assertEqual(self.states(), {
            "Validate": "normal", "Export split": "disabled",
            "Preview SQL": "normal", "Execute": "disabled", "Artifacts": "disabled",
            "Scan runs": "normal", "Audit dictionary": "normal",
        })
        message = self.app.status_message.configure.call_args.kwargs["text"]
        self.assertIn("Executing since", message)
        self.assertIn("last heartbeat", message)

    def test_they_come_back_when_it_ends(self):
        self.lock()
        self.app.watch_pull()
        self.lock(heartbeat_age=10_000)  # stopped without cleaning up
        self.app.watch_pull()
        self.assertEqual(set(self.states().values()), {"normal"})

    def test_another_projects_pull_greys_nothing_here(self):
        other = self.work / "runs" / "Celiac" / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, other)
        self.lock(other)
        self.app.watch_pull()
        self.assertEqual(set(self.states().values()), {"normal"})

    def test_execute_greys_them_before_its_lock_appears(self):
        self.messagebox.askokcancel.return_value = True
        with mock.patch.object(self.app, "run"):
            self.app.on_execute()
        self.app.check_pull()  # no lock yet
        self.assertEqual(self.states()["Execute"], "disabled")
        self.assertEqual(self.states()["Export split"], "disabled")
        self.assertEqual(self.states()["Validate"], "normal")


class FakeConsole:
    """Execute's own window, standing in for the process."""

    def __init__(self, returncode=None):
        self.command = None
        self.started = 0.0
        self.returncode = returncode
        self.pid = 4242
        self.stopped = False

    @property
    def alive(self):
        return self.command is not None and self.returncode is None

    def start(self, command, cwd=None):
        self.command, self.cwd, self.started = command, cwd, time.time()

    def stop(self):
        self.stopped = True
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


class ConsoleTests(GuiTestCase):
    """D68: Execute opens its own window; the Pull Log tab follows its log."""

    def setUp(self):
        super().setUp()
        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        self.logs = self.manifest.parent
        self.app.console = self.console = FakeConsole()
        self.messagebox.askokcancel.return_value = True
        self.messagebox.askyesno.return_value = True

    def pull_log(self):
        return "".join(call.args[1] for call in self.app.pull_output.insert.call_args_list)

    def write_log(self, text, name="execute-20260925-140300.log"):
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / name, "a", encoding="utf-8") as handle:
            handle.write(text)
        return self.logs / name

    def lock(self, log):
        import json

        from ..lock import lock_path

        now = time.time()
        lock_path(self.manifest).write_text(json.dumps(
            {"pid": 4242, "machine": "VM", "started": now, "heartbeat": now, "log": str(log)}
        ), encoding="utf-8")

    def test_execute_opens_its_window_on_the_projects_name(self):
        from .. import launcher

        with mock.patch.object(launcher, "CAN_OPEN_CONSOLE", True):
            self.app.on_execute()
        command = self.console.command
        self.assertEqual(command[command.index("--execute") + 1], "IBD_Ancestry")
        self.assertIn("--keep-open", command)
        self.assertEqual(self.console.cwd, self.work)
        self.assertFalse(self.app.runner.running, "it does not run inside the window")
        self.app.notebook.select.assert_called_with(self.app.pull_tab)

    def test_the_pull_log_follows_the_live_executes_log(self):
        self.app.on_execute()
        log = self.write_log("Execute IBD_Ancestry: started\n")
        self.lock(log)
        self.app.watch_once()
        self.assertIn("Execute IBD_Ancestry: started", self.pull_log())
        self.write_log("=== CrohnsblackPatients ===\n")
        self.app.watch_once()
        self.assertEqual(self.pull_log().count("Execute IBD_Ancestry: started"), 1)
        self.assertIn("=== CrohnsblackPatients ===", self.pull_log())

    def test_a_window_that_ends_before_its_log_gives_the_terminal_command(self):
        # What the VM did from the launcher: ended at once, printing nothing.
        self.app.on_execute()
        self.console.returncode = 3221225794
        self.app.watch_once()
        message = self.messagebox.showerror.call_args.args[1]
        self.assertIn("0xC0000142", message)
        self.assertIn("python scope.py --execute IBD_Ancestry", message)
        self.assertIn(str(self.work), message)
        self.assertIn("python scope.py --execute IBD_Ancestry", self.pull_log())
        self.assertEqual(self.app.buttons["Execute"].configure.call_args.kwargs["state"], "normal")

    def test_a_window_that_could_not_open_says_so_the_same_way(self):
        def refuse(command, cwd=None):
            raise OSError("Access is denied")

        self.console.start = refuse
        self.app.on_execute()
        message = self.messagebox.showerror.call_args.args[1]
        self.assertIn("Access is denied", message)
        self.assertIn("python scope.py --execute IBD_Ancestry", message)

    def test_a_pull_that_ran_ends_without_an_error(self):
        self.app.on_execute()
        self.write_log("Execute IBD_Ancestry: started\n")
        self.app.watch_once()
        self.console.returncode = 0
        self.app.watch_once()
        self.messagebox.showerror.assert_not_called()
        self.assertIn("exit code 0", self.pull_log())

    def test_a_pull_that_ends_mid_way_says_so(self):
        # Killed (Windows gives exit code 1) or stopped by an error: no summary.
        self.app.on_execute()
        self.write_log("=== CrohnsPatients ===\n")
        self.app.watch_once()
        self.console.returncode = 1
        self.app.watch_once()
        self.messagebox.showerror.assert_not_called()
        self.assertIn("before the pull finished", self.pull_log())
        self.assertIn("stays 'running'", self.pull_log())

    def test_a_pull_that_finished_with_failures_is_not_called_unfinished(self):
        self.app.on_execute()
        self.write_log("1/2 session(s) run completed; 0 had nothing to pull.\n")
        self.app.watch_once()
        self.console.returncode = 1
        self.app.watch_once()
        self.assertNotIn("before the pull finished", self.pull_log())

    def test_stop_ends_the_pull_and_frees_its_lock(self):
        from ..lock import read_lock

        self.app.on_execute()
        self.lock(self.write_log("started\n"))
        self.app.on_stop()
        self.assertTrue(self.console.stopped)
        self.assertIsNone(read_lock(self.manifest))


class DefaultTests(GuiTestCase):
    """D63, D93: `python scope.py` with nothing after it opens the app."""

    def setUp(self):
        super().setUp()
        package = __name__.rsplit(".", 2)[0]
        sys.modules.pop(f"{package}.app", None)
        self.addCleanup(sys.modules.pop, f"{package}.app", None)
        self.app_module = importlib.import_module(f"{package}.app")

    def test_no_arguments_opens_the_app(self):
        from .. import cli

        with mock.patch.object(self.app_module, "main", return_value=0) as opened:
            self.assertEqual(cli.main([]), 0)
        opened.assert_called_once_with()

    def test_a_command_still_runs_the_command(self):
        import contextlib
        import io

        from .. import cli

        path = self.work / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, path)
        out = io.StringIO()
        with mock.patch.object(self.app_module, "main", return_value=0) as opened, \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([str(path)]), 0)
        opened.assert_not_called()
        self.assertIn("Sessions: 2", out.getvalue())


class StatusTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "pullmanifest.yaml"

    def test_shows_one_row_per_session_phase_and_run(self):
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        self.app.tree.insert.reset_mock()
        self.app.refresh_status()
        self.assertEqual(self.app.tree.insert.call_count, 11)

    def test_runs_nest_under_their_session(self):
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        self.app.tree.insert.reset_mock()
        self.app.refresh_status()
        parents = [call.args[0] for call in self.app.tree.insert.call_args_list]
        self.assertEqual(parents.count(""), 2)

    def test_tables_nest_under_their_run(self):
        import copy
        import itertools

        data = copy.deepcopy(SAMPLE_MANIFEST)
        data["sessions"][0]["runs"][0].setdefault("outputs", {})["table_rows"] = {"A": 7}
        dump_yaml(data, self.manifest)
        ids = itertools.count()
        self.app.tree.insert.reset_mock()
        self.app.tree.insert.side_effect = lambda *a, **k: f"item{next(ids)}"
        self.app.refresh_status()
        calls = self.app.tree.insert.call_args_list
        table = next(i for i, c in enumerate(calls) if c.kwargs["values"][0] == "table")
        self.assertEqual(calls[table - 1].kwargs["values"][0], "run")
        self.assertEqual(calls[table].args[0], f"item{table - 1}")

    def test_double_clicking_a_failed_run_shows_its_error_in_the_manifest_tab(self):
        # D144: the tab shows the manifest, the error red, and a Status row's
        # double-click brings that run's own error into view.
        import copy

        from .. import launcher

        data = copy.deepcopy(SAMPLE_MANIFEST)
        run = data["sessions"][0]["runs"][1]
        run["status"] = "failed"
        run["error"] = {"message": "OPENQUERY failed", "detail": "ProgrammingError"}
        dump_yaml(data, self.manifest)
        import itertools

        ids = itertools.count()  # a real Treeview gives each row its own id
        self.app.tree.insert.side_effect = lambda *a, **k: f"item{next(ids)}"
        self.app.refresh_status()
        text = self.manifest.read_text(encoding="utf-8")
        view = self.app.manifest_text
        shown = "".join(call.args[1] for call in view.insert.call_args_list)
        self.assertIn("OPENQUERY failed", shown)
        self.assertTrue(shown.lstrip().startswith("1  "), "numbered lines")
        red = [call.args for call in view.tag_add.call_args_list if call.args[0] == "error"]
        self.assertTrue(red)
        row = next(r for r in self.app._status_rows.values() if r.has_error)
        view.tag_add.reset_mock()
        self.app.show_in_manifest(row)
        self.app.notebook.select.assert_called_with(self.app.manifest_tab)
        [found] = [call.args for call in view.tag_add.call_args_list if call.args[0] == "found"]
        first = int(found[1].split(".")[0]) - 1
        self.assertEqual(text.splitlines()[first].strip(), "error:")
        self.assertEqual((first, int(found[2].split(".")[0]) - 1), launcher.manifest_span(text, row))

    def count_reads(self):
        """How often Status opens the manifest (D154)."""
        from unittest import mock

        from .. import gui, yaml_io

        reads = []

        def counted(path):
            reads.append(path)
            return yaml_io.read_shared(path)

        patcher = mock.patch.object(gui, "read_shared", side_effect=counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reads

    def test_a_refresh_reads_the_manifest_once_for_both_tabs(self):
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        reads = self.count_reads()
        self.app.refresh_status()
        self.assertEqual(len(reads), 1)
        shown = "".join(call.args[1] for call in self.app.manifest_text.insert.call_args_list)
        self.assertIn("sessions", shown)

    def test_an_unchanged_manifest_is_not_read_again(self):
        # Each read of an executing pull's manifest could block its save.
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        reads = self.count_reads()
        self.app.refresh_status()
        self.app.refresh_status()
        self.app.refresh_status()
        self.assertEqual(len(reads), 1)

    def test_a_changed_manifest_or_refresh_reads_it_again(self):
        import copy
        import os

        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        reads = self.count_reads()
        self.app.refresh_status()
        data = copy.deepcopy(SAMPLE_MANIFEST)
        data["sessions"][0]["runs"][0]["status"] = "done"
        dump_yaml(data, self.manifest)
        info = os.stat(self.manifest)
        os.utime(self.manifest, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000))
        self.app.refresh_status()
        self.app.refresh_status(force=True)
        self.assertEqual(len(reads), 3)

    def test_a_missing_manifest_says_what_to_do(self):
        self.app.refresh_status()
        message = self.app.status_message.configure.call_args.kwargs["text"]
        self.assertIn("Export a split", message)

    def test_no_transfer_yaml_yet_says_so_instead_of_failing(self):
        self.app.vars["template"].set("")
        self.app.refresh_status()
        message = self.app.status_message.configure.call_args.kwargs["text"]
        self.assertIn("transfer YAML", message)


class RepullSessionTests(GuiTestCase):
    """D158: tick Re-pull sessions, add finished sessions, and Execute passes them."""

    def setUp(self):
        super().setUp()
        import copy

        self.app.vars["template"].set("IBD_Ancestry_transfer.yaml")
        data = copy.deepcopy(SAMPLE_MANIFEST)
        session = data["sessions"][0]
        for node in [*session["phases"].values(), *session["runs"]]:
            node["status"] = "done"
        session["status"] = "done"
        dump_yaml(data, self.work / "runs" / "IBD_Ancestry" / "pullmanifest.yaml")

    def test_the_choices_are_all_then_the_finished_sessions(self):
        self.assertEqual(self.app.repull_choices(), ["all", "UCblackPatients"])

    def test_added_sessions_reach_the_options_only_while_ticked(self):
        self.app.repull_pick.set("UCblackPatients")
        self.app.add_repull_session()
        self.app.add_repull_session()  # once is enough
        self.assertEqual(self.app.options().repull_sessions, ())
        self.app.repull_some.set(True)
        self.assertEqual(self.app.options().repull_sessions, ("UCblackPatients",))
        self.app.remove_repull_session()
        self.assertEqual(self.app.options().repull_sessions, ())


class AppTests(GuiTestCase):
    """D93: Author and Run in one window; Author's transfer goes to Run (D94)."""

    def setUp(self):
        super().setUp()
        package = __name__.rsplit(".", 2)[0]
        sys.modules.pop(f"{package}.app", None)
        self.addCleanup(sys.modules.pop, f"{package}.app", None)
        self.app_module = importlib.import_module(f"{package}.app")
        from ..launcher import locate_tools
        self.tools = locate_tools()

    def test_launcher_builds_inside_the_run_tab_and_leaves_the_window_alone(self):
        root = mock.MagicMock()
        frame = mock.MagicMock()
        run = self.gui.LauncherApp(root, self.tools, self.work, parent=frame)
        self.assertFalse(run.standalone)
        root.title.assert_not_called()
        root.protocol.assert_not_called()

    def test_a_transfer_from_author_is_loaded_into_run_and_shown(self):
        with mock.patch.object(self.app_module, "load_author", side_effect=ImportError("no author here")):
            app = self.app_module.App(mock.MagicMock(), self.tools, self.work)
        transfer = self.work / "IBD_Ancestry_transfer.yaml"
        app.take_transfer(transfer)
        self.assertEqual(app.run.vars["template"].get(), str(transfer))
        app.halves.select.assert_called_with(app.run_frame)

    def test_run_still_opens_when_author_cannot(self):
        with mock.patch.object(self.app_module, "load_author", side_effect=ImportError("no yamlmanager_tk")):
            app = self.app_module.App(mock.MagicMock(), self.tools, self.work)
        self.assertIsNone(app.author)
        self.assertIsNotNone(app.run)
