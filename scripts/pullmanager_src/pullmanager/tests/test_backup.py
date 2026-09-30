"""D149: a pull is backed up before Artifacts replaces it."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from unittest import mock

from .. import cli, config, pulls
from ..backup import backup_pull, mirror
from ..manifest import Manifest
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST
from .test_artifacts import ArtifactTestCase


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name).resolve()
        self.run_dir = self.work / "runs" / "Infant_RSV"
        for rel, text in {
            "pullmanifest.yaml": "manifest",
            "cosmos_parquets/EDVisits.parquet": "old visits",
            "utils/client/viewparquets.py": "viewer",
            "pull_files/split/sessions/a.yaml": "split",
            "pull_files/sql/a.sql": "sql",
            "pullmanifest.lock": "lock",
        }.items():
            path = self.run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def tree(self, folder: Path) -> dict[str, str]:
        return {p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(folder.rglob("*")) if p.is_file()}


class MirrorTests(BackupTestCase):
    def test_everything_but_the_split_sql_and_lock_is_copied(self):
        target = self.work / "drive" / "Infant_RSV"
        self.assertEqual(mirror(self.run_dir, target), (3, 0, 0))
        self.assertEqual(self.tree(target), {
            "cosmos_parquets/EDVisits.parquet": "old visits",
            "pullmanifest.yaml": "manifest",
            "utils/client/viewparquets.py": "viewer",
        })

    def test_a_second_backup_copies_only_what_changed_and_removes_what_went(self):
        target = self.work / "drive" / "Infant_RSV"
        mirror(self.run_dir, target)
        changed = self.run_dir / "pullmanifest.yaml"
        changed.write_text("manifest, longer now", encoding="utf-8")
        os.utime(changed, (time.time() + 10, time.time() + 10))
        (self.run_dir / "cosmos_parquets" / "EDVisits.parquet").unlink()
        self.assertEqual(mirror(self.run_dir, target), (1, 1, 1))
        self.assertEqual(self.tree(target), {
            "pullmanifest.yaml": "manifest, longer now",
            "utils/client/viewparquets.py": "viewer",
        })
        self.assertFalse((target / "cosmos_parquets").exists())


class WhereTests(BackupTestCase):
    def test_to_the_backup_folder_when_it_can_be_reached(self):
        drive = self.work / "drive"
        drive.mkdir()
        config.set_backup(self.work, drive)
        saved = backup_pull(self.run_dir, self.work)
        self.assertEqual((saved.destination, saved.fell_back), (drive / "Infant_RSV", ""))
        self.assertTrue((drive / "Infant_RSV" / "pullmanifest.yaml").is_file())

    def test_to_runs_backup_when_it_cannot_be_reached_or_none_is_set(self):
        local = self.work / "runs" / "backup" / "Infant_RSV"
        config.set_backup(self.work, self.work / "unplugged")
        saved = backup_pull(self.run_dir, self.work)
        self.assertEqual(saved.destination, local)
        self.assertIn("could not be reached", saved.fell_back)
        config.set_backup(self.work, None)
        saved = backup_pull(self.run_dir, self.work)
        self.assertEqual(saved.destination, local)
        self.assertIn("no backup folder is set", saved.fell_back)
        self.assertEqual(self.tree(local)["cosmos_parquets/EDVisits.parquet"], "old visits")


class BackUpAllTests(BackupTestCase):
    def test_every_pull_but_one_executing(self):
        other = self.work / "runs" / "Celiac"
        shutil.copytree(self.run_dir, other)
        (other / "pullmanifest.lock").unlink()
        (self.run_dir / "pullmanifest.lock").write_text(json.dumps(
            {"pid": 4242, "machine": "VM", "started": time.time(), "heartbeat": time.time()}),
            encoding="utf-8")
        for folder in (self.run_dir, other):  # manifests find_pulls can read
            dump_yaml(SAMPLE_MANIFEST, folder / "pullmanifest.yaml")
        drive = self.work / "drive"
        drive.mkdir()
        config.set_backup(self.work, drive)
        out = io.StringIO()
        # Only the test's folder: beside the runtime, on the VM, are real pulls.
        with mock.patch.object(pulls, "home_folders", lambda cwd=None: [Path(cwd or self.work)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.backup_all(self.work)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue((drive / "Celiac" / "cosmos_parquets" / "EDVisits.parquet").is_file())
        self.assertFalse((drive / "Infant_RSV").exists())
        self.assertIn("Infant_RSV: skipped, executing now", out.getvalue())


class ArtifactsBacksUpFirstTests(ArtifactTestCase):
    """The backup holds the packaging before the new one: what a wrong pull
    noticed late needs."""

    def run_artifacts(self, db):
        args = argparse.Namespace(env=None)
        out = io.StringIO()
        with contextlib.chdir(self.work), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = cli.artifacts(Manifest.load(self.split / "pullmanifest.yaml"), args,
                                 connect_fn=lambda *a, **k: db)
        return code, out.getvalue()

    def old_packaging(self):
        old = self.out / "cosmos_parquets" / "Patients.parquet"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"the packaging before")
        return old

    def test_the_old_parquets_are_in_the_backup_and_the_new_in_the_pull(self):
        self.set_status()
        old = self.old_packaging()
        drive = self.work / "drive"
        drive.mkdir()
        config.set_backup(self.work, drive)
        code, out = self.run_artifacts(self.projects())
        self.assertEqual(code, 0, out)
        kept = drive / "IBD_Ancestry" / "cosmos_parquets" / "Patients.parquet"
        self.assertEqual(kept.read_bytes(), b"the packaging before")
        self.assertNotEqual(old.read_bytes(), b"the packaging before")  # replaced by the new
        self.assertNotIn("WARNING", out)

    def test_an_unreachable_backup_goes_to_runs_backup_and_packaging_goes_on(self):
        self.set_status()
        self.old_packaging()
        config.set_backup(self.work, self.work / "unplugged")
        code, out = self.run_artifacts(self.projects())
        self.assertEqual(code, 0, out)
        kept = self.work / "runs" / "backup" / "IBD_Ancestry" / "cosmos_parquets" / "Patients.parquet"
        self.assertEqual(kept.read_bytes(), b"the packaging before")
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("WARNING Backed up to"), out)
        self.assertIn("could not be reached", out)

    def test_a_pull_never_packaged_has_nothing_to_back_up(self):
        self.set_status()
        code, out = self.run_artifacts(self.projects())
        self.assertEqual(code, 0, out)
        self.assertFalse((self.work / "runs" / "backup").exists())
