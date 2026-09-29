"""Finding a pull by its project's name, and listing the pulls (D66)."""

from __future__ import annotations

import contextlib
import copy
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .. import cli, pulls
from ..pulls import PullNotFound, execute_command, find_pulls, listing, resolve
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"


def manifest_with(*statuses: str) -> dict:
    """SAMPLE_MANIFEST with its sessions' phases and runs all set to a status."""
    data = copy.deepcopy(SAMPLE_MANIFEST)
    for session, status in zip(data["sessions"], statuses):
        for node in [*session["phases"].values(), *session["runs"]]:
            node["status"] = status
    return data


class PullsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name).resolve()
        # Nothing beside the runtime: only the working directory is searched.
        patcher = mock.patch.object(pulls, "home_folders", lambda cwd=None: [Path(cwd or self.work)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_pull(self, name: str, data: dict | None = None, home: Path | None = None) -> Path:
        path = (home or self.work) / "runs" / name / "split" / "pullmanifest.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_yaml(data or SAMPLE_MANIFEST, path)
        return path


class ResolveTests(PullsTestCase):
    def test_every_way_of_naming_a_pull_finds_its_manifest(self):
        manifest = self.make_pull("IBD_Ancestry")
        (self.work / "IBD_Ancestry_transfer.yaml").write_text("project_folder: IBD Ancestry\n")
        for name in (
            "IBD_Ancestry",
            "IBD Ancestry",
            "ibd_ancestry",
            "IBD_Ancestry_transfer.yaml",
            str(self.work / "IBD_Ancestry_transfer.yaml"),
            "runs/IBD_Ancestry/split/pullmanifest.yaml",
            "runs/IBD_Ancestry",
        ):
            with self.subTest(name=name):
                self.assertEqual(resolve(name, self.work).resolve(), manifest.resolve())

    def test_a_transfer_yaml_is_not_taken_for_a_manifest(self):
        # It exists and is YAML, but it lists no sessions: its pull is meant.
        manifest = self.make_pull("Test_Run")
        (self.work / "Test_Run_transfer.yaml").write_text("cohorts: []\n")
        self.assertEqual(resolve("Test_Run_transfer.yaml", self.work).resolve(), manifest.resolve())

    def test_a_manifest_anywhere_still_works_by_its_path(self):
        path = self.work / "elsewhere" / "custom.yaml"
        path.parent.mkdir()
        dump_yaml(SAMPLE_MANIFEST, path)
        self.assertEqual(resolve(str(path), self.work), path)

    def test_an_unknown_name_lists_the_pulls_there_are(self):
        self.make_pull("IBD_Ancestry")
        self.make_pull("Test_Run")
        with self.assertRaises(PullNotFound) as caught:
            resolve("IBD_Ancestory", self.work)
        message = str(caught.exception)
        self.assertIn("No pull named 'IBD_Ancestory'", message)
        self.assertIn("IBD_Ancestry, Test_Run", message)

    def test_a_mistyped_path_says_the_manifest_is_missing(self):
        self.make_pull("IBD_Ancestry")
        with self.assertRaises(PullNotFound) as caught:
            resolve("runs/IBD_Ancestory/split/pullmanifest.yaml", self.work)
        self.assertIn("Manifest not found", str(caught.exception))
        self.assertIn("IBD_Ancestry", str(caught.exception))

    def test_no_pulls_at_all_says_to_export_a_split(self):
        with self.assertRaises(PullNotFound) as caught:
            resolve("IBD_Ancestry", self.work)
        self.assertIn("Export the transfer YAML's split first", str(caught.exception))

    def test_the_folder_beside_the_runtime_is_searched_second(self):
        beside = self.work / "share"
        here = self.work / "somewhere"
        here.mkdir()
        manifest = self.make_pull("IBD_Ancestry", home=beside)
        with mock.patch.object(pulls, "home_folders", lambda cwd=None: [here, beside]):
            self.assertEqual(resolve("IBD_Ancestry", here).resolve(), manifest.resolve())


class HomeFolderTests(unittest.TestCase):
    def test_the_working_directory_first_then_the_runtimes_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            homes = pulls.home_folders(Path(tmp))
        self.assertEqual(homes[0], Path(tmp).resolve())
        # pullmanager/pulls.py -> the extracted folder -> the folder it sits in.
        self.assertEqual(homes[-1], Path(pulls.__file__).resolve().parents[2])


class ListingTests(PullsTestCase):
    def test_each_pull_shows_its_state_and_its_command(self):
        self.make_pull("A_Fresh", manifest_with("pending", "pending"))
        self.make_pull("B_Halfway", manifest_with("done", "pending"))
        self.make_pull("C_Failed", manifest_with("done", "failed"))
        self.make_pull("D_Finished", manifest_with("done", "done"))
        states = {p.name: p.state for p in find_pulls(self.work)}
        self.assertEqual(states, {
            "A_Fresh": "not started",
            "B_Halfway": "1 of 2 sessions done",
            "C_Failed": "1 of 2 sessions done, 1 failed",
            "D_Finished": "finished",
        })
        lines = listing(self.work)
        self.assertEqual(lines[0], "Which pull? Name one:")
        row = next(line for line in lines if "B_Halfway" in line)
        self.assertIn("python scope.py --execute B_Halfway", row)

    def test_no_pulls_says_to_export_a_split(self):
        self.assertIn("Export split", listing(self.work)[0])


class ExecuteCommandTests(PullsTestCase):
    def test_a_pull_in_runs_is_named(self):
        manifest = self.make_pull("IBD_Ancestry")
        command, folder = execute_command(manifest, self.work)
        self.assertEqual(command, "python scope.py --execute IBD_Ancestry")
        self.assertEqual(folder, self.work)

    def test_a_manifest_elsewhere_is_given_by_its_path(self):
        path = self.work / "my split" / "pullmanifest.yaml"
        path.parent.mkdir()
        dump_yaml(SAMPLE_MANIFEST, path)
        command, folder = execute_command(path, self.work)
        self.assertEqual(command, f'python scope.py --execute "{Path("my split") / "pullmanifest.yaml"}"')
        self.assertEqual(folder, self.work)


class ExecuteByNameTests(PullsTestCase):
    """The command itself: which manifest `--execute <name>` pulls."""

    def run_cli(self, *argv):
        executed = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "execute", lambda manifest, args: executed.append(manifest.path) or 0), \
                contextlib.chdir(self.work), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, executed, out.getvalue(), err.getvalue()

    def test_a_name_pulls_that_projects_manifest(self):
        manifest = self.make_pull("IBD_Ancestry")
        self.make_pull("Test_Run")
        code, executed, _, _ = self.run_cli("--execute", "IBD Ancestry")
        self.assertEqual(code, 0)
        self.assertEqual([p.resolve() for p in executed], [manifest.resolve()])

    def test_no_name_pulls_nothing_and_lists_the_choices(self):
        self.make_pull("IBD_Ancestry")
        code, executed, out, _ = self.run_cli("--execute")
        self.assertEqual(executed, [])
        self.assertEqual(code, 1)
        self.assertIn("Which pull?", out)
        self.assertIn("python scope.py --execute IBD_Ancestry", out)

    def test_an_unknown_name_pulls_nothing(self):
        self.make_pull("IBD_Ancestry")
        code, executed, _, err = self.run_cli("--execute", "Nope")
        self.assertEqual((code, executed), (1, []))
        self.assertIn("No pull named 'Nope'", err)


class PreviewStatementTests(PullsTestCase):
    """D71: the preview ends with one statement holding everything needed next."""

    def setUp(self):
        super().setUp()
        if not FIXTURES.is_dir():
            self.skipTest(f"fixtures not found at {FIXTURES}")
        self.split = self.work / "runs" / "IBD_Ancestry" / "split"
        shutil.copytree(FIXTURES, self.split)

    def preview(self, *extra):
        out = io.StringIO()
        with contextlib.chdir(self.work), contextlib.redirect_stdout(out):
            code = cli.main(["--dry-run", str(Path("runs/IBD_Ancestry/split/pullmanifest.yaml")), *extra])
        self.assertEqual(code, 0)
        return out.getvalue().rstrip("\n").splitlines()

    def test_the_last_lines_say_what_was_written_and_how_to_pull_it(self):
        lines = self.preview("--out-dir", str(Path("runs/IBD_Ancestry/sql")))
        self.assertRegex(
            lines[-4],
            r"^Preview finished: \d+ unit\(s\), \d+ SQL block\(s\), 0 errors, \d+ note\(s\)\. "
            r"Nothing was pulled\.$",
        )
        sql = Path("runs") / "IBD_Ancestry" / "sql"
        self.assertEqual(lines[-3], f"SQL written to {sql} for reading; Execute does not need it.")
        self.assertEqual(lines[-2], f"To pull it: press Execute, or in a terminal in {self.work} run:")
        self.assertEqual(lines[-1], "    python scope.py --execute IBD_Ancestry")
        self.assertTrue(any((self.work / sql).iterdir()))

    def test_without_an_sql_folder_it_says_none_was_written(self):
        lines = self.preview()
        self.assertIn("No SQL was written; add --out-dir", lines[-3])

    def test_the_named_command_pulls_the_previewed_manifest(self):
        # What the statement says to type must find the same manifest.
        lines = self.preview()
        name = lines[-1].split("--execute ")[1]
        self.assertEqual(resolve(name, self.work).resolve(), (self.split / "pullmanifest.yaml").resolve())
