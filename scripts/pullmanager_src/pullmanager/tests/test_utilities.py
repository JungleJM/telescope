"""D124: the utilities window offers each script in utils/, run on its own."""

from __future__ import annotations

import importlib.util
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
            if widget.winfo_class() == "TLabel" and widget.cget("text") == "Designed and built by Jason Mathias":
                return widget.winfo_manager() == "pack"
        except tk.TclError:
            pass
    return False
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
print("CREDIT" if seen and all(seen) else "NO CREDIT")
"""


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
        done = subprocess.run([sys.executable, "-c", CREDIT_PROBE, str(folder), code],
                              capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
        return done.stdout.strip().splitlines()[-1:] or [done.stderr[-2000:]]

    def test_each_window_says_who_made_it(self):
        windows = {
            "the utilities window": (self.SRC, "import utilities; utilities.window()"),
            "the parquet viewer": (self.SRC / "utils", "import viewparquets; viewparquets.main()"),
            "clear_projects_db": (self.SRC / "utils", "import clear_projects_db; clear_projects_db.main([])"),
            "the app": (self.SRC, "from pullmanager import app; app.main()"),
        }
        try:
            import PIL  # noqa: F401  the transcription viewer draws with Pillow
            windows["the transcription viewer"] = (
                self.SRC / "utils", "import transcription_viewer; transcription_viewer.main([])")
        except ImportError:
            pass
        for name, (folder, code) in windows.items():
            with self.subTest(window=name):
                self.assertEqual(self.probe(folder, code), ["CREDIT"])

