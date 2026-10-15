from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cleanup import Cleaner
from ub_agents.cleanup_control import CleanupControl
from ub_agents.coordination import Coordinator
from ub_agents.diagnostic_retention import RETENTION_SECONDS
from ub_agents.execution import Workspace, git
from ub_agents.records import timestamp
from ub_agents.startup_cleanup import StartupCleanup, sweep
from tests.support import FakeGitHub, agent, config, issue, pr


class StartupCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name).resolve()
        self.root = parent / "checkout"
        self.root.mkdir()
        self.origin = parent / "origin.git"
        git(parent, "init", "--bare", str(self.origin))
        git(self.root, "init", "-b", "main")
        (self.root / "file").write_text("base")
        git(self.root, "add", "file")
        self.base = self.commit("base")
        git(self.root, "remote", "add", "origin", str(self.origin))
        git(self.root, "push", "origin", "main")
        self.tip = self.commit("unpublished")
        self.branch = "ub-agents/worker/99/old"
        git(self.root, "branch", self.branch, self.tip)
        git(self.root, "reset", "--hard", self.base)
        self.config = config(self.root, agent(self.root, worktree=True))
        self.github = FakeGitHub(issue())
        self.stop = threading.Event()
        self.control = CleanupControl(self.stop)
        self.sessions = self.root / ".ub-agents/sessions"
        (self.root / ".ub-agents").mkdir()
        self.log = self.sessions / "own.cleanup.log"

    def commit(self, message):
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def sweep(self, session="own"):
        sweep(self.config, self.github, "operator", session=session, control=self.control)

    def start(self):
        worker = StartupCleanup(self.config, self.github, "operator", self.stop, session="own")
        worker.start()
        return worker

    def join(self, thread):
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_recovery_sha_is_flushed_before_ref_deletion_even_without_result_line(self):
        deleted = []

        def remove(root, *args):
            if args[:3] == ("update-ref", "-d", f"refs/heads/{self.branch}"):
                self.assertIn(f"unpushed tip {self.tip}", self.log.read_text())
                self.assertNotIn("removed branch", self.log.read_text())
                git(root, *args)
                deleted.append(args)
                raise RuntimeError("interrupted after deletion")
            return git(root, *args)

        with patch("ub_agents.cleanup.git", side_effect=remove), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.sweep()
        self.assertEqual(len(deleted), 1)
        self.assertEqual(git(self.root, "for-each-ref", f"refs/heads/{self.branch}"), "")
        self.assertIn(f"unpushed tip {self.tip}", self.log.read_text())
        self.assertIn("interrupted after deletion", self.log.read_text())
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_log_creation_failure_removes_and_prunes_nothing(self):
        with patch.object(Path, "open", side_effect=OSError("log unavailable")), \
                patch.object(Cleaner, "clean") as clean, patch("ub_agents.startup_cleanup.prune") as prune, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.sweep()
        clean.assert_not_called()
        prune.assert_not_called()
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.tip)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_log_write_or_flush_failure_aborts_before_deletion(self):
        for fail in ("write", "flush", "recovery"):
            with self.subTest(fail=fail):
                stream = io.StringIO()
                if fail == "write":
                    stream.write = lambda _: (_ for _ in ()).throw(OSError("disk full"))
                elif fail == "flush":
                    stream.flush = lambda: (_ for _ in ()).throw(OSError("disk full"))
                else:
                    write = stream.write

                    def fail_recovery(line):
                        if "removing branch" in line:
                            raise OSError("disk full")
                        return write(line)

                    stream.write = fail_recovery
                with patch.object(Path, "open", return_value=stream), \
                        patch("ub_agents.startup_cleanup.prune") as prune, \
                        redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                    self.sweep()
                prune.assert_not_called()
                self.assertEqual(git(self.root, "rev-parse", self.branch), self.tip)
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_no_session_and_empty_sweeps_always_create_unique_logs(self):
        with patch.object(Cleaner, "inventory", return_value=[]):
            self.sweep(None)
            self.sweep(None)
        logs = list(self.sessions.glob("*.cleanup.log"))
        self.assertEqual(len(logs), 2)
        self.assertTrue(all("Startup cleanup finished" in log.read_text() for log in logs))
        old = timestamp() - RETENTION_SECONDS - 1
        for log in logs:
            os.utime(log, (old, old))
        with patch.object(Cleaner, "inventory", return_value=[]):
            self.sweep()
        self.assertFalse(any(log.exists() for log in logs))
        self.assertIn("pruned 0 run directories and 2 session files", self.log.read_text())

    def test_claim_and_worktree_setup_survive_a_concurrent_inventory(self):
        entered, scan = threading.Event(), threading.Event()
        inventory = Cleaner.inventory

        def inventory_during_setup(cleaner):
            entered.set()
            if not scan.wait(5):
                raise RuntimeError("setup did not reach inventory")
            return inventory(cleaner)

        worker = None

        def prepare(root, *args):
            result = git(root, *args)
            if args[:2] == ("worktree", "add"):
                # The tree and branch exist, but setup has not returned and the
                # claiming lease still has no host or process-group fields.
                scan.set()
                self.join(worker.thread)
            return result

        with patch.object(Cleaner, "inventory", inventory_during_setup):
            worker = self.start()
            try:
                self.assertTrue(entered.wait(5))
                coordinator = Coordinator(self.github, "operator")
                coordinator.cleanup_control = worker.control
                role = self.config.agents[0]
                lease = coordinator.claim(coordinator.plan(issue(), role, ()))
                workspace = Workspace(self.config, role, issue(), lease, self.github)
                with patch("ub_agents.execution.git", side_effect=prepare):
                    path = workspace.prepare()
                self.assertTrue(path.is_dir())
                self.assertEqual(git(path, "rev-parse", "HEAD"), self.base)
                self.assertEqual(git(self.root, "rev-parse", lease["branch"]), self.base)
                self.assertIn("claimed by this launcher", self.log.read_text())
            finally:
                scan.set()
                worker.close()
                self.join(worker.thread)

    def test_concurrent_cleanup_fetch_cannot_change_issue_or_pr_checkout(self):
        merged = replace(pr(2, head=self.tip), branch=self.branch, state="closed", merged=True)
        candidate = replace(pr(3, head=self.base), branch="candidate")
        self.github.items.update({2: merged, 3: candidate})
        git(self.root, "push", "origin", f"{self.tip}:refs/pull/2/head")
        git(self.origin, "update-ref", "refs/pull/3/head", self.base)
        for item in (issue(), candidate):
            with self.subTest(kind=item.kind):
                fetched, swept = threading.Event(), threading.Event()
                errors, known = [], []

                def cleanup_fetch():
                    try:
                        if not fetched.wait(5):
                            raise RuntimeError("workspace did not fetch")
                        cleaner = Cleaner(self.config, self.github, "operator")
                        known.append(cleaner.known_tip(self.branch, self.tip))
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        swept.set()

                def fetch(root, *args):
                    result = git(root, *args)
                    if args[0] == "fetch" and threading.current_thread() is threading.main_thread():
                        fetched.set()
                        self.assertTrue(swept.wait(5))
                    return result

                thread = threading.Thread(target=cleanup_fetch)
                thread.start()
                try:
                    lease = {"run": f"new-{item.kind}"}
                    workspace = Workspace(self.config, self.config.agents[0], item, lease, self.github)
                    with patch("ub_agents.execution.git", side_effect=fetch):
                        path = workspace.prepare()
                    self.assertEqual(git(path, "rev-parse", "HEAD"), self.base)
                    self.assertEqual(known, [True])
                    self.assertEqual(errors, [])
                    self.assertEqual(git(self.root, "for-each-ref", "refs/ub-agents/fetch/"), "")
                finally:
                    fetched.set()
                    self.join(thread)

    def test_quit_does_not_wait_for_reads_or_start_mutations_after_they_finish(self):
        entered, release = threading.Event(), threading.Event()

        def inventory(cleaner):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("inventory was not released")
            return original(cleaner)

        original = Cleaner.inventory
        with patch.object(Cleaner, "inventory", inventory), patch("ub_agents.cleanup.run_hook") as hook:
            worker = self.start()
            try:
                self.assertTrue(entered.wait(5))
                worker.close()
                self.assertTrue(worker.thread.is_alive())
                self.assertTrue(worker.thread.daemon)
            finally:
                release.set()
                self.join(worker.thread)
        hook.assert_not_called()
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.tip)

    def check_quit_during_artifact(self, stage):
        path = self.root / ".ub-agents/worktrees/old"
        git(self.root, "worktree", "add", str(path), self.branch)
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        calls = []
        original = Cleaner.remove_worktree

        def pause(*args):
            calls.append(stage)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("artifact was not released")
            if stage == "removal":
                original(*args)

        def close():
            worker.close()
            closing.set()

        target = "ub_agents.cleanup.run_hook" if stage == "hook" else "ub_agents.cleanup.Cleaner.remove_worktree"
        with patch(target, autospec=True, side_effect=pause), patch("ub_agents.startup_cleanup.prune") as prune:
            worker = self.start()
            closer = threading.Thread(target=close)
            try:
                self.assertTrue(entered.wait(5))
                self.stop.set()
                closer.start()
                self.assertFalse(closing.wait(0.05))
            finally:
                release.set()
                self.join(worker.thread)
                if closer.ident is not None:
                    self.join(closer)
        self.assertTrue(closing.is_set())
        self.assertEqual(calls, [stage])
        prune.assert_not_called()
        self.assertEqual(path.exists(), stage == "hook")
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.tip)

    def test_quit_waits_for_current_hook_and_starts_no_removal(self):
        self.check_quit_during_artifact("hook")

    def test_quit_waits_for_current_removal_and_starts_no_next_artifact(self):
        self.check_quit_during_artifact("removal")

    def test_worker_start_failure_is_logged_and_never_runs_cleanup(self):
        worker = StartupCleanup(self.config, self.github, "operator", self.stop, session="own")
        with patch.object(worker.thread, "start", side_effect=RuntimeError("no threads")), \
                patch.object(Cleaner, "clean") as clean:
            worker.start()
        clean.assert_not_called()
        self.assertIn("Startup cleanup failed: no threads", self.log.read_text())

    def old_session_files(self):
        self.sessions.mkdir()
        old = timestamp() - RETENTION_SECONDS - 1
        paths = [self.sessions / name for name in ("a.tmp", "b.tmp")]
        for path in paths:
            path.write_text("old session")
            os.utime(path, (old, old))
        return paths

    def test_pruning_failures_and_summary_are_only_in_cleanup_log(self):
        paths = self.old_session_files()
        unlink = os.unlink

        def fail(name, **kwargs):
            if name == "a.tmp":
                raise OSError("pruning denied")
            return unlink(name, **kwargs)

        with patch.object(Cleaner, "inventory", return_value=[]), \
                patch("ub_agents.diagnostic_retention.os.unlink", side_effect=fail), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.sweep()
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())
        log = self.log.read_text()
        self.assertEqual(log.count("Cannot prune session file a.tmp: pruning denied"), 1)
        self.assertIn("pruned 0 run directories and 1 session files", log)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_quit_during_pruning_starts_no_next_prune(self):
        paths = self.old_session_files()
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        unlink = os.unlink

        def remove(name, **kwargs):
            if name == "a.tmp":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("prune was not released")
            return unlink(name, **kwargs)

        def close():
            worker.close()
            closing.set()

        with patch.object(Cleaner, "inventory", return_value=[]), \
                patch("ub_agents.diagnostic_retention.os.unlink", side_effect=remove):
            worker = self.start()
            closer = threading.Thread(target=close)
            try:
                self.assertTrue(entered.wait(5))
                self.stop.set()
                closer.start()
                self.assertFalse(closing.wait(0.05))
            finally:
                release.set()
                self.join(worker.thread)
                if closer.ident is not None:
                    self.join(closer)
        self.assertTrue(closing.is_set())
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
