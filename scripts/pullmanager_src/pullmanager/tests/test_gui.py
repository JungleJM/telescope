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
        self.assertEqual(
            set(self.app.vars),
            {"template", "datadictionary", "split_dir", "sql_dir"},
        )

    def test_starts_from_the_defaults(self):
        # Blank: the project's own runs/<project>/ folders (D57).
        self.assertEqual(self.app.paths().split_dir, "")
        self.assertEqual(self.app.paths().sql_dir, "")

    def test_restores_remembered_choices(self):
        from ..launcher import Paths, save_settings

        save_settings(Paths(template="IBD_transfer.yaml", datadictionary="../data/d.yaml"), self.work)
        from ..launcher import locate_tools
        app = self.gui.LauncherApp(mock.MagicMock(), locate_tools(), self.work)
        self.assertEqual(app.vars["template"].get(), "IBD_transfer.yaml")
        self.assertEqual(app.vars["datadictionary"].get(), "../data/d.yaml")


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
            ["Validate", "Export split", "Preview SQL", "Execute", "Artifacts"],
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
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "split" / "pullmanifest.yaml"
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

    def test_a_live_pull_greys_export_split_and_execute_only(self):
        self.lock()
        self.app.watch_pull()
        self.assertEqual(self.states(), {
            "Validate": "normal", "Export split": "disabled",
            "Preview SQL": "normal", "Execute": "disabled", "Artifacts": "disabled",
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
        other = self.work / "runs" / "Celiac" / "split" / "pullmanifest.yaml"
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
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "split" / "pullmanifest.yaml"
        dump_yaml(SAMPLE_MANIFEST, self.manifest)
        self.logs = self.manifest.parent.parent / "logs"
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
        self.assertIn("python pullmanager.py --execute IBD_Ancestry", message)
        self.assertIn(str(self.work), message)
        self.assertIn("python pullmanager.py --execute IBD_Ancestry", self.pull_log())
        self.assertEqual(self.app.buttons["Execute"].configure.call_args.kwargs["state"], "normal")

    def test_a_window_that_could_not_open_says_so_the_same_way(self):
        def refuse(command, cwd=None):
            raise OSError("Access is denied")

        self.console.start = refuse
        self.app.on_execute()
        message = self.messagebox.showerror.call_args.args[1]
        self.assertIn("Access is denied", message)
        self.assertIn("python pullmanager.py --execute IBD_Ancestry", message)

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
    """D63, D93: `python pullmanager.py` with nothing after it opens the app."""

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
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "split" / "pullmanifest.yaml"

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

    def test_a_missing_manifest_says_what_to_do(self):
        self.app.refresh_status()
        message = self.app.status_message.configure.call_args.kwargs["text"]
        self.assertIn("Export a split", message)

    def test_no_transfer_yaml_yet_says_so_instead_of_failing(self):
        self.app.vars["template"].set("")
        self.app.refresh_status()
        message = self.app.status_message.configure.call_args.kwargs["text"]
        self.assertIn("transfer YAML", message)


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
