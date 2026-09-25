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
        return lambda *args, **kwargs: mock.MagicMock(name=name)


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
        self.work = Path(self._tmp.name)

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
