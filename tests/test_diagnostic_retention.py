import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cleanup import Artifact, Cleaner
from ub_agents.cleanup_control import CleanupControl
from ub_agents.coordination import Coordinator
from ub_agents.diagnostic_retention import RETENTION_SECONDS, prune
from ub_agents.errors import AgentError, CleanupError
from ub_agents.records import iso, timestamp
from ub_agents.startup_cleanup import sweep
from tests.support import FakeGitHub, config, edit_lease, issue


class DiagnosticRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = config(self.root)
        self.github = FakeGitHub(issue())
        self.lines = []
        self.cleaner = Cleaner(self.config, self.github, "operator", output=self.lines.append)
        self.inventory = self.enterContext(patch.object(self.cleaner, "inventory", return_value=[]))
        self.now = timestamp()
        self.old = self.now - RETENTION_SECONDS - 1
        self.runs = self.root / ".ub-agents/runs"
        self.sessions = self.root / ".ub-agents/sessions"
        self.sessions.mkdir(parents=True)

    def file(self, path, text="log", time=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (self.old if time is None else time,) * 2)
        return path

    def make_run(self, name="old", text="log", time=None):
        return self.file(self.runs / name / "process.log", text, time).parent

    def snapshot(self, name, *, time=None, **state):
        return self.file(self.sessions / f"{name}.json", json.dumps({
            "published_at": iso(self.now), "pid": os.getpid(), "host": socket.gethostname(),
            "ended": True, **state}), time)

    def prune(self, **kwargs):
        prune(self.cleaner, self.lines.append, now=self.now, **kwargs)

    def lease(self):
        coordinator = Coordinator(self.github, "operator")
        lease = coordinator.claim(coordinator.plan(issue(), self.config.agents[0], ()))
        edit_lease(self.github, lease, state="released", host=socket.gethostname())
        self.make_run(lease["run"])
        return lease

    def test_newest_nested_file_and_strict_seven_day_boundary(self):
        old = self.make_run("old", "1234")
        # A recently touched directory does not make its files recent.
        os.utime(old, (self.now,) * 2)
        self.make_run("boundary", time=self.now - RETENTION_SECONDS)
        self.make_run("new", time=self.now)
        self.make_run("nested")
        self.file(self.runs / "nested/cleanup/attempt/stopped", "confirmed\n", self.now)
        self.prune()
        self.assertFalse(old.exists())
        for name in ("boundary", "new", "nested"):
            self.assertTrue((self.runs / name).exists())
        self.assertEqual(self.lines, ["pruned 1 run directories and 0 session files older than 7 days (4 B)"])

    def test_no_pruning_prints_nothing(self):
        self.make_run(time=self.now)
        self.snapshot("live", ended=False)
        self.prune()
        self.assertEqual(self.lines, [])

    def test_live_local_and_unconfirmed_leases_protect_old_files(self):
        lease = self.lease()
        for fields in ({"state": "running", "expires": iso(self.now + 60)},
                       {"state": "claiming", "expires": iso(self.now + 60)},
                       {"state": "released", "cleanup": "unconfirmed"}):
            with self.subTest(fields=fields):
                edit_lease(self.github, lease, **fields)
                self.prune()
                self.assertTrue((self.runs / lease["run"]).exists())
        self.assertEqual(self.lines, [])

    def test_expired_local_group_must_be_confirmed_gone(self):
        lease = self.lease()
        edit_lease(self.github, lease, state="running", expires=iso(self.now - 1), process_group=1234)
        for result in ([1234], CleanupError("ps unavailable")):
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=result), patch("ub_agents.cleanup.group_members", **kwargs):
                self.prune()
                self.assertTrue((self.runs / lease["run"]).exists())
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.prune()
        self.assertFalse((self.runs / lease["run"]).exists())

    def test_other_host_live_lease_does_not_keep_local_diagnostics(self):
        lease = self.lease()
        edit_lease(self.github, lease, state="running", host="another-host", expires=iso(self.now + 60))
        self.prune()
        self.assertFalse((self.runs / lease["run"]).exists())

    def test_unconfirmed_or_unreadable_events_preserve_logs_without_a_lease(self):
        for name, events in (("unconfirmed", '{"event":"cleanup-unconfirmed"}\n'),
                             ("invalid", "invalid json"), ("non-object", "[]")):
            self.make_run(name)
            self.file(self.runs / name / "events.jsonl", events)
        self.prune()
        self.assertEqual(len(list(self.runs.iterdir())), 3)
        self.assertEqual(self.lines, [])

    def test_setup_and_hook_process_records_preserve_uncertain_runs(self):
        for folder in ("checkout-setup", "cleanup/attempt"):
            for mode in ("missing", "invalid", "present", "error", "stopped", "gone"):
                with self.subTest(folder=folder, mode=mode):
                    name = folder.replace("/", "-") + "-" + mode
                    self.make_run(name)
                    directory = self.runs / name / folder
                    directory.mkdir(parents=True)
                    if mode == "stopped":
                        self.file(directory / "stopped", "confirmed\n")
                    elif mode != "missing":
                        self.file(directory / "pid", "invalid" if mode == "invalid" else "1234")
                    group_result = [1234] if mode == "present" else []
                    group_error = CleanupError("ps unavailable") if mode == "error" else None
                    with patch("ub_agents.worktree_setup.group_members", return_value=group_result,
                               side_effect=group_error), \
                            patch("ub_agents.hooks.group_members", return_value=group_result,
                                  side_effect=group_error):
                        self.prune()
                    self.assertEqual((self.runs / name).exists(), mode not in {"stopped", "gone"})

    def test_failed_coordination_reads_preserve_old_logs(self):
        lease = self.lease()
        for method in ("repository_comments", "comments"):
            with self.subTest(method=method), patch.object(self.github, method, side_effect=AgentError("read failed")):
                self.prune()
                self.assertTrue((self.runs / lease["run"]).exists())
        self.assertEqual(self.lines, [])

    def test_kept_artifact_or_cleanup_failure_preserves_logs(self):
        directory = self.make_run()
        for kind in ("worktree", "branch"):
            with self.subTest(kind=kind):
                self.inventory.return_value = [Artifact(kind, "kept", "old")]
                self.prune()
                self.assertTrue(directory.exists())
        self.inventory.return_value = []
        for protected in (self.cleaner.kept_runs, self.cleaner.blocked_runs):
            protected.add("old")
            self.prune()
            self.assertTrue(directory.exists())
            protected.clear()
        self.assertEqual(self.lines, [])

    def test_kept_branch_with_different_run_name_protects_lease_diagnostics(self):
        lease = self.lease()
        branch = "ub-agents/worker/1/other-run"
        edit_lease(self.github, lease, branch=branch)
        self.inventory.return_value = [Artifact("branch", branch, "other-run")]
        self.prune()
        self.assertTrue((self.runs / lease["run"]).exists())

    def test_run_links_are_unlinked_and_redirected_contents_are_kept(self):
        target = self.file(self.root / "outside/events.jsonl", '{"event":"cleanup-unconfirmed"}\n').parent
        self.runs.mkdir()
        link = self.runs / "link"
        link.symlink_to(target, target_is_directory=True)
        os.utime(link, (self.old,) * 2, follow_symlinks=False)
        directory = self.make_run("redirected")
        (directory / "events.jsonl").symlink_to(target / "events.jsonl")
        nested = self.make_run("nested-link")
        (nested / "cleanup").symlink_to(target, target_is_directory=True)
        self.prune()
        self.assertFalse(link.is_symlink())
        self.assertTrue((target / "events.jsonl").exists())
        self.assertTrue(directory.exists())
        self.assertTrue(nested.exists())

    def test_live_lease_protects_even_a_symlinked_run(self):
        lease = self.lease()
        path = self.runs / lease["run"]
        (path / "process.log").unlink()
        path.rmdir()
        path.symlink_to(self.root / "outside")
        os.utime(path, (self.old,) * 2, follow_symlinks=False)
        edit_lease(self.github, lease, state="running", expires=iso(self.now + 60))
        self.prune()
        self.assertTrue(path.is_symlink())

    def test_redirected_parent_directories_are_never_followed(self):
        outside = self.file(self.root / "outside/old.json", '{"ended": true}').parent
        self.sessions.rmdir()
        self.sessions.symlink_to(outside)
        self.runs.symlink_to(outside)
        self.prune()
        self.assertTrue((outside / "old.json").exists())
        self.assertEqual(len(self.lines), 2)
        self.assertTrue(all(line.startswith("Cannot prune") for line in self.lines))

    def test_recent_write_during_coordination_read_preserves_run(self):
        directory = self.make_run()
        read = self.cleaner.run_state

        def renewed(*args, **kwargs):
            result = read(*args, **kwargs)
            os.utime(directory / "process.log", (self.now,) * 2)
            return result

        with patch.object(self.cleaner, "run_state", side_effect=renewed):
            self.prune()
        self.assertTrue(directory.exists())
        self.assertEqual(self.lines, [])

    def test_unreadable_regular_file_keeps_run_and_logs_failure_once(self):
        directory = self.make_run()
        real_open = os.open

        def denied(path, *args, **kwargs):
            if path == "process.log":
                raise PermissionError("diagnostics denied")
            return real_open(path, *args, **kwargs)

        with patch("ub_agents.diagnostic_retention.os.open", side_effect=denied):
            self.prune()
        self.assertTrue(directory.exists())
        self.assertEqual(self.lines, ["Cannot prune run directory old: diagnostics denied; continuing to poll"])

    def test_redirected_worktree_inventory_keeps_run_logs(self):
        directory = self.make_run()
        (self.root / ".ub-agents/worktrees").symlink_to(self.root / "outside")
        self.prune()
        self.assertTrue(directory.exists())
        self.assertEqual(self.lines, [])

    def test_ended_stale_and_dead_pid_sessions_prune_with_companion_logs(self):
        self.snapshot("ended")
        self.snapshot("stale", ended=False, host="another-host", published_at=iso(self.now - 31))
        self.snapshot("dead", ended=False)
        for name in ("ended", "stale", "dead"):
            self.file(self.sessions / f"{name}.cleanup.log", time=self.now)
        with patch("ub_agents.observation_worker.os.kill", side_effect=ProcessLookupError):
            self.prune()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("0 run directories and 6 session files", self.lines[0])

    def test_live_current_and_recent_sessions_and_their_files_survive(self):
        self.snapshot("live", ended=False)
        self.snapshot("own")
        self.snapshot("new", time=self.now)
        self.snapshot("boundary", time=self.now - RETENTION_SECONDS)
        for name in ("live", "own"):
            self.file(self.sessions / f"{name}.cleanup.log")
            self.file(self.sessions / f"{name}.tmp")
        self.prune(session="own")
        self.assertEqual(len(list(self.sessions.iterdir())), 8)
        self.assertEqual(self.lines, [])

    def test_old_orphan_files_prune_and_unknown_snapshots_preserve_companions(self):
        self.file(self.sessions / "orphan.tmp")
        self.file(self.sessions / "orphan.cleanup.log")
        self.file(self.sessions / "recent.tmp", time=self.now)
        self.file(self.sessions / "invalid.json", "invalid")
        self.file(self.sessions / "invalid.cleanup.log")
        self.prune()
        self.assertEqual(sorted(p.name for p in self.sessions.iterdir()),
                         ["invalid.cleanup.log", "invalid.json", "recent.tmp"])
        self.assertEqual(self.lines, ["pruned 0 run directories and 2 session files older than 7 days (6 B)"])

    def test_session_links_remove_only_links(self):
        outside = self.file(self.root / "outside.json", "external")
        for name in ("linked.json", "orphan.tmp", "orphan.cleanup.log"):
            path = self.sessions / name
            path.symlink_to(outside)
            os.utime(path, (self.old,) * 2, follow_symlinks=False)
        self.prune()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.assertEqual(outside.read_text(), "external")

    def test_removal_failures_are_logged_once_and_other_entries_still_prune(self):
        self.make_run("bad")
        self.make_run("good", "1234")
        self.snapshot("bad")
        self.file(self.sessions / "bad.cleanup.log")
        self.snapshot("good")
        unlink = os.unlink

        def fail_unlink(name, *args, **kwargs):
            if name == "bad.json":
                raise PermissionError("session denied")
            return unlink(name, *args, **kwargs)

        import shutil
        rmtree = shutil.rmtree

        def fail_rmtree(name, *args, **kwargs):
            if name == "bad":
                raise PermissionError("run denied")
            return rmtree(name, *args, **kwargs)

        with patch("ub_agents.diagnostic_retention.shutil.rmtree", side_effect=fail_rmtree), \
                patch("ub_agents.diagnostic_retention.os.unlink", side_effect=fail_unlink):
            self.prune()
        self.assertTrue((self.runs / "bad").exists())
        self.assertFalse((self.runs / "good").exists())
        self.assertTrue((self.sessions / "bad.json").exists())
        self.assertEqual(sum("session denied" in line for line in self.lines), 1)
        self.assertEqual(sum("run denied" in line for line in self.lines), 1)
        self.assertIn("pruned 1 run directories", self.lines[-1])

    def test_startup_failure_preserves_runs_but_can_still_prune_sessions(self):
        self.make_run()
        self.snapshot("ended")
        with patch("ub_agents.cleanup.Cleaner.clean", side_effect=AgentError("inventory failed")):
            sweep(self.config, self.github, "operator", session="own", control=CleanupControl(threading.Event()))
        self.assertTrue((self.runs / "old").exists())
        self.assertFalse((self.sessions / "ended.json").exists())
        lines = (self.sessions / "own.cleanup.log").read_text().splitlines()
        self.assertIn("Startup cleanup failed: inventory failed; continuing to poll", lines)
        self.assertTrue(any(line.startswith("pruned 0 run directories and 1 session files") for line in lines))
