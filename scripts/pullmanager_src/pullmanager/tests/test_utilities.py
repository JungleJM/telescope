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
