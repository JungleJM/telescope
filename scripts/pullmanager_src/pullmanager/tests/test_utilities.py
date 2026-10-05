"""D124: the utilities window offers each script in utils/, run on its own."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

UTILITIES = Path(__file__).resolve().parents[2] / "utilities.py"


def load_utilities():
    spec = importlib.util.spec_from_file_location("utilities", UTILITIES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UtilitiesTests(unittest.TestCase):
    def setUp(self):
        self.utilities = load_utilities()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_each_script_is_offered_by_name_and_underscored_ones_are_not(self):
        for name in ("viewparquets.py", "Transcription_viewer.py", "_helper.py", "notes.txt"):
            (self.folder / name).write_text("", encoding="utf-8")
        self.assertEqual([p.name for p in self.utilities.scripts(self.folder)],
                         ["Transcription_viewer.py", "viewparquets.py"])

    def test_a_script_runs_with_this_python_in_its_own_process(self):
        script = self.folder / "viewparquets.py"
        with mock.patch.object(self.utilities.subprocess, "Popen") as popen:
            self.utilities.start(script, cwd=self.folder)
        popen.assert_called_once_with([sys.executable, str(script)], cwd=str(self.folder))

    def test_the_shipped_utilities_include_the_viewer(self):
        self.assertIn("viewparquets.py", [p.name for p in self.utilities.scripts()])


if __name__ == "__main__":
    unittest.main()


# Opens a window with its event loop patched out, then prints whether a label
# on it reads the credit. Run in a process of its own: two Tk windows in one
# test process can hang on the Mac.
CREDIT_PROBE = """
import sys, tkinter as tk
seen = []
def shown(root):
    stack = [root]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        try:
            text = str(widget.cget("text")) if widget.winfo_class() == "TLabel" else ""
            if text.startswith("Designed and built by Jason Mathias") and widget.winfo_manager() == "pack":
                return text
        except tk.TclError:
            pass
    return None
def fake_loop(self, n=0):
    self.update_idletasks()
    seen.append(shown(self))
    self.destroy()
tk.Tk.mainloop = fake_loop
tk.Misc.mainloop = fake_loop
opened = tk.Tk.__init__
def hidden(self, *args, **kwargs):  # never shown: the checks run on the VM too
    opened(self, *args, **kwargs)
    self.withdraw()
tk.Tk.__init__ = hidden
sys.path.insert(0, sys.argv[1])
exec(sys.argv[2])
print(seen[-1] if seen and seen[-1] else "NO CREDIT")
"""


class GroupTests(unittest.TestCase):
    """D148: utilities sorted for whom they are; a script at the top still shows."""

    def test_client_and_manager_show_under_their_headings(self):
        utilities = load_utilities()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for rel in ("mine.py", "client/viewparquets.py", "manager/clear_projects_db.py",
                        "client/_private.py"):
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_text("", encoding="utf-8")
            self.assertEqual([(h, [p.name for p in ps]) for h, ps in utilities.sections(folder)], [
                ("", ["mine.py"]),
                ("Client", ["viewparquets.py"]),
                ("Manager", ["clear_projects_db.py"]),
            ])

    def test_the_shipped_ones_are_sorted(self):
        utilities = load_utilities()
        shipped = {h: [p.name for p in ps] for h, ps in utilities.sections()}
        self.assertEqual(shipped.get("Client"), ["transcription_viewer.py", "viewparquets.py"])
        self.assertEqual(shipped.get("Manager"), ["clear_projects_db.py"])


# Opens the app with its event loop patched out, then prints its tabs and the
# buttons on its Utils tab, one per line (D194).
UTILS_TAB_PROBE = """
import sys, tkinter as tk
from tkinter import ttk
seen = []
def fake_loop(self, n=0):
    self.update_idletasks()
    book = next(w for w in self.winfo_children() if isinstance(w, ttk.Notebook))
    seen.append("TABS " + ",".join(book.tab(t, "text") for t in book.tabs()))
    utils = self.nametowidget(book.tabs()[-1])
    stack = list(utils.winfo_children())
    while stack:
        widget = stack.pop(0)
        stack.extend(widget.winfo_children())
        if isinstance(widget, ttk.Button):
            seen.append("BUTTON " + str(widget.cget("text")))
    self.destroy()
tk.Tk.mainloop = fake_loop
tk.Misc.mainloop = fake_loop
opened = tk.Tk.__init__
def hidden(self, *args, **kwargs):
    opened(self, *args, **kwargs)
    self.withdraw()
tk.Tk.__init__ = hidden
sys.path.insert(0, sys.argv[1])
from pullmanager import app
app.main()
print("\\n".join(seen))
"""


class UtilsTabTests(unittest.TestCase):
    """D194: the app's Utils tab, after Run, has the utilities window's buttons."""

    SRC = Path(__file__).resolve().parents[2]

    def test_the_tab_is_after_run_with_a_button_per_utility(self):
        import subprocess

        try:
            import tkinter as tk

            tk.Tk().destroy()
        except Exception as exc:  # noqa: BLE001 - no display, no window to check
            self.skipTest(f"needs a display ({exc})")
        done = subprocess.run([sys.executable, "-c", UTILS_TAB_PROBE, str(self.SRC)],
                              capture_output=True, encoding="utf-8", timeout=120,
                              cwd=tempfile.gettempdir(), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        lines = done.stdout.strip().splitlines()
        self.assertIn("TABS Author,Run,Utils", lines, done.stderr[-2000:])
        buttons = [line[len("BUTTON "):] for line in lines if line.startswith("BUTTON ")]
        self.assertEqual(buttons, [p.stem for p in load_utilities().scripts()])

    def test_the_window_and_the_tab_draw_with_one_function(self):
        utilities = load_utilities()
        with mock.patch.object(utilities, "fill") as fill, \
                mock.patch("tkinter.Tk") as tk_root, mock.patch.object(utilities, "add_credit"):
            utilities.window(self.SRC / "utils")
        fill.assert_called_once()
        self.assertEqual(fill.call_args.args[1], self.SRC / "utils")
        tk_root.return_value.mainloop.assert_called_once()


class CreditTests(unittest.TestCase):
    """D145: every window says who made it."""

    SRC = Path(__file__).resolve().parents[2]

    def probe(self, folder, code):
        import subprocess

        try:
            import tkinter as tk

            tk.Tk().destroy()
        except Exception as exc:  # noqa: BLE001 - no display, no window to check
            self.skipTest(f"needs a display ({exc})")
        # UTF-8 both ways: on Windows a child's piped output is cp1252 (D147's ·).
        done = subprocess.run([sys.executable, "-c", CREDIT_PROBE, str(folder), code],
                              capture_output=True, encoding="utf-8", timeout=120,
                              cwd=tempfile.gettempdir(), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return done.stdout.strip().splitlines()[-1:] or [done.stderr[-2000:]]

    def test_each_window_says_who_made_it(self):
        windows = {
            "the utilities window": (self.SRC, "import utilities; utilities.window()"),
            "the parquet viewer": (self.SRC / "utils" / "client", "import viewparquets; viewparquets.main()"),
            "clear_projects_db": (self.SRC / "utils" / "manager", "import clear_projects_db; clear_projects_db.main([])"),
            "the app": (self.SRC, "from pullmanager import app; app.main()"),
        }
        try:
            import PIL  # noqa: F401  the transcription viewer draws with Pillow
            windows["the transcription viewer"] = (
                self.SRC / "utils" / "client", "import transcription_viewer; transcription_viewer.main([])")
        except ImportError:
            pass
        # D147: beside it, the bundle each window runs from; none from source.
        from ..config import bundle_id

        found = bundle_id()
        expected = "Designed and built by Jason Mathias" + (f" \u00b7 bundle {found}" if found else "")
        for name, (folder, code) in windows.items():
            with self.subTest(window=name):
                self.assertEqual(self.probe(folder, code), [expected])

