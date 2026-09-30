"""D146: the parquet viewer opens a pull's tables by buttons, two at a time."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST

VIEWER = Path(__file__).resolve().parents[2] / "utils" / "client" / "viewparquets.py"


def load_viewer():
    spec = importlib.util.spec_from_file_location("viewparquets", VIEWER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finished_manifest() -> dict:
    data = copy.deepcopy(SAMPLE_MANIFEST)
    for session in data["sessions"]:
        for node in [*session["phases"].values(), *session["runs"]]:
            node["status"] = "done"
    data["last_execute"] = {"started_at": "x", "ended_at": "y", "exit_code": 0, "how": "finished"}
    return data


class ViewerTestCase(unittest.TestCase):
    def setUp(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("needs pyarrow")
        self.viewer = load_viewer()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name).resolve()
        # Only the test's folder: on the VM, beside the runtime are real pulls.
        if str(VIEWER.parents[2]) not in sys.path:
            sys.path.insert(0, str(VIEWER.parents[2]))
        pulls = importlib.import_module("pullmanager.pulls")
        patcher = mock.patch.object(pulls, "home_folders", lambda cwd=None: [Path(cwd or self.work)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_parquet(self, path: Path, rows: int = 5) -> Path:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({
            "PatientDurableKey": pa.array([10_000_000_000 + i for i in range(rows)], pa.int64()),
            "Sex": pa.array([None if i == 1 else ("Female", "Male")[i % 2] for i in range(rows)]),
            "Days": pa.array([rows - i if i != 2 else None for i in range(rows)], pa.int32()),
        }), path)
        return path

    def make_pull(self, name: str, data: dict | None, tables=("A", "B", "C")) -> Path:
        run = self.work / "runs" / name
        run.mkdir(parents=True)
        if data is not None:
            dump_yaml(data, run / "pullmanifest.yaml")
        for table in tables:
            self.write_parquet(run / "cosmos_parquets" / f"{table}.parquet")
        return run


class ColumnsTests(ViewerTestCase):
    def test_one_fills_two_split_and_a_third_replaces_the_older(self):
        columns = self.viewer.Columns()
        self.assertEqual(columns.pick("A"), 0)
        self.assertEqual(columns.pick("B"), 1)
        self.assertEqual(columns.pick("C"), 0)  # A was the older
        self.assertEqual(columns.shown, ["C", "B"])
        self.assertEqual(columns.pick("D"), 1)  # now B is
        self.assertEqual(columns.shown, ["C", "D"])
        self.assertIsNone(columns.pick("C"))  # already shown: nothing moves
        columns.close(0)
        self.assertEqual(columns.shown, ["D"])


class ArrowTableTests(ViewerTestCase):
    def test_only_the_page_asked_for_is_read_into_python(self):
        path = self.write_parquet(self.work / "t.parquet", rows=2500)
        table = self.viewer.load_parquet(str(path))
        self.assertIsInstance(table, self.viewer.ArrowTable)
        self.assertEqual(table.row_count, 2500)
        page = table.page(1000, 1000)
        self.assertEqual(len(page), 1000)
        self.assertEqual(page[0][0], 10_000_001_000)
        self.assertEqual(len(table.page(2000, 1000)), 500)

    def test_sorting_is_whole_table_with_empty_values_last(self):
        path = self.write_parquet(self.work / "t.parquet", rows=5)
        table = self.viewer.load_parquet(str(path))
        up = [row[2] for row in table.sorted(2, descending=False).page(0, 10)]
        down = [row[2] for row in table.sorted(2, descending=True).page(0, 10)]
        self.assertEqual(up, [1, 2, 4, 5, None])
        self.assertEqual(down, [5, 4, 2, 1, None])


class PullListTests(ViewerTestCase):
    def test_pulls_that_have_run_are_listed_as_run_shows_them(self):
        self.make_pull("Finished", finished_manifest())
        self.make_pull("NotRun", copy.deepcopy(SAMPLE_MANIFEST))
        found = self.viewer.runtime_pulls(self.work)
        self.assertEqual([label for label, _ in found], ["Finished  (finished)"])
        self.assertEqual(found[0][1], self.work / "runs" / "Finished")

    def test_a_copy_away_from_the_runtime_has_no_list_and_opens_its_own_pull(self):
        run = self.make_pull("P", finished_manifest())
        copy = run / "utils" / "client" / "viewparquets.py"  # where Artifacts puts it (D148)
        copy.parent.mkdir(parents=True)
        shutil.copyfile(VIEWER, copy)
        spec = importlib.util.spec_from_file_location("copied_viewer", copy)
        copied = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(copied)
        self.assertIsNone(copied.runtime_pulls(self.work))
        self.assertEqual(copied.own_pull(), run)


# The window, opened hidden with its event loop patched out, driven as a click
# would; prints what it shows. A process of its own: two Tk windows in one test
# process can hang on the Mac.
WINDOW_PROBE = """
import os, sys, tkinter as tk
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import pullmanager.pulls  # only the test's folder: beside the runtime, on the VM, are real pulls
pullmanager.pulls.home_folders = lambda cwd=None: [Path(cwd)]
tk.Tk.mainloop = lambda self, n=0: None
opened = tk.Tk.__init__
def hidden(self, *args, **kwargs):
    opened(self, *args, **kwargs)
    self.withdraw()
tk.Tk.__init__ = hidden
import importlib.util
spec = importlib.util.spec_from_file_location("viewer", sys.argv[1])
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)
app = viewer.ParquetViewer(cwd=sys.argv[2])
if app.pulls:
    print("PULLS", "|".join(app.pulls))
    app.choose_pull(next(iter(app.pulls)))
else:
    print("OWN", app.pull_label.cget("text"))
buttons = sorted(app.table_buttons)
print("BUTTONS", "|".join(b.cget("text") for b in app.table_buttons.values()))
for path in buttons:
    app.table_buttons[path].invoke()
    app.update_idletasks()
    print("SHOWN", "|".join(app.shown_names()), "PANES", len(app.pane.panes()))
print("MARKED", "|".join(sorted(b.cget("text") for b in app.table_buttons.values() if b.cget("text").startswith(viewer.SHOWN_MARK))))
app.destroy()
"""


class WindowTests(ViewerTestCase):
    def probe(self, viewer: Path) -> list[str]:
        try:
            import tkinter as tk

            tk.Tk().destroy()
        except Exception as exc:  # noqa: BLE001 - no display, no window to check
            self.skipTest(f"needs a display ({exc})")
        # UTF-8 both ways: on Windows a child's piped output is cp1252, which
        # has no ● for a shown table's button, and the print failed.
        done = subprocess.run([sys.executable, "-c", WINDOW_PROBE, str(viewer), str(self.work),
                               str(VIEWER.parents[2])],
                              capture_output=True, encoding="utf-8", timeout=120,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.splitlines()

    def test_choosing_a_pull_then_three_tables_shows_the_two_latest(self):
        self.make_pull("Infant_RSV", finished_manifest())
        out = self.probe(VIEWER)
        self.assertEqual(out[0], "PULLS Infant_RSV  (finished)")
        self.assertEqual(out[1], "BUTTONS A  5|B  5|C  5")
        self.assertEqual(out[2:5], [
            "SHOWN A.parquet PANES 1",
            "SHOWN A.parquet|B.parquet PANES 2",
            # C takes A's place, the older of the two; B stays where it was.
            "SHOWN C.parquet|B.parquet PANES 2",
        ])
        mark = self.viewer.SHOWN_MARK
        self.assertEqual(out[5], f"MARKED {mark}B  5|{mark}C  5")

    def test_the_copy_in_a_pulls_folder_opens_on_that_pull(self):
        run = self.make_pull("Infant_RSV", finished_manifest(), tables=("EDVisits",))
        copy = run / "utils" / "client" / "viewparquets.py"  # where Artifacts puts it (D148)
        copy.parent.mkdir(parents=True)
        shutil.copyfile(VIEWER, copy)
        out = self.probe(copy)
        self.assertEqual(out[0], "OWN Infant_RSV")
        self.assertEqual(out[1], "BUTTONS EDVisits  5")
        self.assertEqual(out[2], "SHOWN EDVisits.parquet PANES 1")
