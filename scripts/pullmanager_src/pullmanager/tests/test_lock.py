"""The lock a running Execute holds on its manifest (D67)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from .. import cli, pulls
from ..db import DatabaseError
from ..launcher import locate_tools
from ..lock import STALE_SECONDS, LockHeld, PullLock, live_lock, lock_path, read_lock
from ..manifest import Manifest
from ..yaml_io import dump_yaml
from .support import SAMPLE_MANIFEST


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name).resolve()
        self.manifest = self.work / "runs" / "IBD_Ancestry" / "split" / "pullmanifest.yaml"
        self.manifest.parent.mkdir(parents=True)
        dump_yaml(SAMPLE_MANIFEST, self.manifest)

    def write_lock(self, heartbeat_age: float, **extra):
        now = time.time()
        data = {"pid": 4242, "machine": "VM", "started": now - 600,
                "heartbeat": now - heartbeat_age, "token": "theirs", **extra}
        lock_path(self.manifest).write_text(json.dumps(data), encoding="utf-8")


class PullLockTests(LockTestCase):
    def test_a_second_execute_is_refused_while_the_first_holds_it(self):
        with PullLock(self.manifest):
            self.assertIsNotNone(live_lock(self.manifest))
            with self.assertRaises(LockHeld) as caught:
                PullLock(self.manifest).acquire()
        message = str(caught.exception)
        self.assertIn("IBD_Ancestry is already executing", message)
        self.assertIn("process", message)
        self.assertIsNone(read_lock(self.manifest), "the lock is removed when the pull ends")
        with PullLock(self.manifest):
            pass

    def test_a_stale_lock_is_taken_over(self):
        # Its process stopped without cleaning up: no heartbeat for 2 minutes.
        self.write_lock(STALE_SECONDS + 1)
        with PullLock(self.manifest) as held:
            self.assertEqual(held.replaced.pid, 4242)
            self.assertEqual(read_lock(self.manifest).token, held.token)

    def test_a_live_lock_of_another_process_is_not_taken(self):
        self.write_lock(20)
        with self.assertRaises(LockHeld):
            PullLock(self.manifest).acquire()
        self.assertEqual(read_lock(self.manifest).token, "theirs")

    def test_the_heartbeat_keeps_it_live_through_a_long_query(self):
        with PullLock(self.manifest, interval=0.05) as held:
            first = read_lock(self.manifest).heartbeat
            time.sleep(0.3)  # a query that blocks the main thread
            self.assertGreater(read_lock(self.manifest).heartbeat, first)
            self.assertEqual(read_lock(self.manifest).started, held.started)

    def test_release_leaves_a_lock_that_is_not_its_own(self):
        held = PullLock(self.manifest)
        held.acquire()
        self.write_lock(0)  # replaced meanwhile, say by a takeover
        held.release()
        self.assertEqual(read_lock(self.manifest).token, "theirs")

    def test_a_lock_caught_mid_write_counts_as_live(self):
        lock_path(self.manifest).write_text("", encoding="utf-8")
        self.assertIsNotNone(live_lock(self.manifest))


class ExecuteLockTests(LockTestCase):
    """The command itself: what `--execute` does with the lock."""

    def execute(self, connect_fn):
        args = argparse.Namespace(env=None, repull=False, retry_failed=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.execute(Manifest.load(self.manifest), args, connect_fn=connect_fn)
        return code, out.getvalue()

    def test_a_pull_already_executing_is_not_touched(self):
        self.write_lock(20)
        connected = []
        before = self.manifest.read_text(encoding="utf-8")
        code, out = self.execute(lambda *a, **k: connected.append(a))
        self.assertEqual(code, 1)
        self.assertEqual(connected, [], "it must not reach a database")
        self.assertIn("already executing", out)
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), before)

    def test_the_lock_is_held_while_it_runs_and_gone_after_a_failure(self):
        seen = []

        def connect(*args, **kwargs):
            seen.append(live_lock(self.manifest))
            raise DatabaseError("login failed")

        code, _ = self.execute(connect)
        self.assertEqual(code, 1)
        self.assertIsNotNone(seen[0])
        self.assertIsNone(read_lock(self.manifest))

    def test_a_stale_lock_is_reported_and_taken_over(self):
        self.write_lock(STALE_SECONDS + 1)

        def connect(*args, **kwargs):
            raise DatabaseError("login failed")

        _, out = self.execute(connect)
        self.assertIn("Took over a stale lock: process 4242 on VM", out)


class RunningListTests(LockTestCase):
    def test_running_says_which_pulls_are_executing(self):
        other = self.work / "runs" / "Test_Run" / "split" / "pullmanifest.yaml"
        other.parent.mkdir(parents=True)
        dump_yaml(SAMPLE_MANIFEST, other)
        self.write_lock(20)
        out = io.StringIO()
        with mock.patch.object(pulls, "home_folders", lambda cwd=None: [self.work]), \
                contextlib.chdir(self.work), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--running"]), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("Pulls under runs"))
        ibd = next(line for line in lines if "IBD_Ancestry" in line)
        test_run = next(line for line in lines if "Test_Run" in line)
        self.assertIn("executing since", ibd)
        self.assertNotIn("executing", test_run)

    def test_a_manifest_left_running_without_a_lock_is_stopped(self):
        data = json.loads(json.dumps(SAMPLE_MANIFEST))
        data["sessions"][0]["phases"]["setup"]["status"] = "running"
        dump_yaml(data, self.manifest)
        with mock.patch.object(pulls, "home_folders", lambda cwd=None: [self.work]):
            state = pulls.find_pulls(self.work)[0].state
        self.assertTrue(state.startswith("stopped mid-run"), state)


class MakeYamlAgreesTests(LockTestCase):
    """makeYaml keeps its own copy of the rule, since it cannot import this."""

    def make_yaml(self):
        try:
            path = locate_tools().make_yaml
        except Exception as exc:  # pragma: no cover - depends on the layout
            self.skipTest(f"makeYaml not found: {exc}")
        spec = importlib.util.spec_from_file_location("makeyaml_for_lock", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # its dataclasses look themselves up there
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def test_both_see_the_same_lock_live_or_stale(self):
        make_yaml = self.make_yaml()
        self.assertEqual(make_yaml.PULL_LOCK_FILENAME, lock_path(self.manifest).name)
        self.assertEqual(make_yaml.PULL_LOCK_STALE_SECONDS, STALE_SECONDS)
        with PullLock(self.manifest):
            self.assertIsNotNone(make_yaml.executing_pull(self.manifest.parent))
        self.assertIsNone(make_yaml.executing_pull(self.manifest.parent))
        for age, live in ((20, True), (STALE_SECONDS + 1, False)):
            with self.subTest(age=age):
                self.write_lock(age)
                self.assertEqual(make_yaml.executing_pull(self.manifest.parent) is not None, live)
                self.assertEqual(live_lock(self.manifest) is not None, live)


class ExecuteLogTests(LockTestCase):
    """D68: whatever starts it, Execute writes what it prints to a log."""

    def execute(self, connect_fn):
        args = argparse.Namespace(env=None, repull=False, retry_failed=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.execute(Manifest.load(self.manifest), args, connect_fn=connect_fn)
        return code, out.getvalue()

    def logs(self):
        return sorted((self.work / "runs" / "IBD_Ancestry" / "logs").glob("execute-*.log"))

    def test_the_log_holds_what_the_terminal_showed(self):
        recorded = []

        def connect(*args, **kwargs):
            recorded.append(read_lock(self.manifest).log)
            raise DatabaseError("login failed for PROJECTS")

        code, out = self.execute(connect)
        [log] = self.logs()
        text = log.read_text(encoding="utf-8")
        self.assertIn("login failed for PROJECTS", out)
        self.assertEqual(text, out)
        self.assertTrue(text.startswith("Execute IBD_Ancestry: "))
        # The lock names it, whole, for the launcher to follow.
        self.assertEqual(recorded, [str(log.resolve())])

    def test_a_refused_execute_says_why_in_its_log(self):
        self.write_lock(20)
        code, _ = self.execute(lambda *a, **k: None)
        self.assertEqual(code, 1)
        [log] = self.logs()
        self.assertIn("already executing", log.read_text(encoding="utf-8"))

    def test_ctrl_c_stops_it_releasing_the_lock(self):
        def connect(*args, **kwargs):
            raise KeyboardInterrupt

        code, out = self.execute(connect)
        self.assertEqual(code, 130)
        self.assertIn("Stopped (Ctrl+C)", out)
        self.assertIsNone(read_lock(self.manifest))


class KeepOpenTests(LockTestCase):
    """D68: the console stays until exit is typed; Enter alone does nothing."""

    def test_only_exit_closes_it(self):
        answers = iter(["", "close", "  EXIT  ", "never asked"])
        asked = []

        def answer(prompt):
            asked.append(prompt)
            return next(answers)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.keep_open(0, answer)
        self.assertEqual(len(asked), 3)
        self.assertIn(
            "Safe to close: the pull has finished (exit code 0). "
            "Type exit and press Enter to close this window.",
            out.getvalue(),
        )

    def test_the_command_waits_after_a_failure_too(self):
        # An unknown name ends at once; its window must still stay to be read.
        answers = iter(["exit"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(pulls, "home_folders", lambda cwd=None: [self.work]), \
                contextlib.chdir(self.work), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--execute", "Nope", "--keep-open"], input_fn=lambda _: next(answers))
        self.assertEqual(code, 1)
        self.assertIn("No pull named 'Nope'", err.getvalue())
        self.assertIn("exit code 1", out.getvalue())


class ClearLockTests(LockTestCase):
    def test_stop_clears_only_the_lock_of_the_process_it_ended(self):
        from ..lock import clear_lock_of

        self.write_lock(20)
        self.assertFalse(clear_lock_of(self.manifest, 1111))
        self.assertIsNotNone(read_lock(self.manifest))
        self.assertTrue(clear_lock_of(self.manifest, 4242))
        self.assertIsNone(read_lock(self.manifest))

