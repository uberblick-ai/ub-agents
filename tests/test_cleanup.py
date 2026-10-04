from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ub_agents.cleanup import Cleaner
from ub_agents.config import CleanupHook
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError, CleanupError
from ub_agents.execution import git, stop_group
from ub_agents.github import GitHub
from ub_agents.records import iso, timestamp
from tests.support import FakeGitHub, config, edit_lease, issue, pr


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git(self.root, "init", "-b", "main")
        (self.root / ".gitignore").write_text(".ub-agents/\nignored\n")
        (self.root / "file").write_text("base")
        git(self.root, "add", ".")
        self.commit(self.root)
        self.head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "remote", "add", "origin", str(self.root))
        self.github = FakeGitHub(issue())
        self.config = config(self.root)
        self.coordinator = Coordinator(self.github, "operator")
        self.lease = self.coordinator.claim(self.coordinator.plan(issue(), self.config.agents[0], ()))
        self.branch = f"ub-agents/worker/1/{self.lease['run']}"
        self.path = self.root / ".ub-agents" / "worktrees" / self.lease["run"]
        git(self.root, "worktree", "add", "-b", self.branch, str(self.path), "HEAD")
        self.coordinator.update(self.lease, state="running", started=True, branch=self.branch,
                                host=socket.gethostname())
        self.coordinator.release(self.lease, "retry", "fixture")
        self.cleaner = Cleaner(self.config, self.github, "operator", output=lambda *_: None)

    def commit(self, path):
        git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "fixture")

    def test_cleanup_scans_repository_comments_without_startup_window(self):
        github = GitHub("org/project")
        cleaner = Cleaner(self.config, github, "operator", output=lambda *_: None)
        with patch.object(github, "request", return_value=[]) as request:
            cleaner.clean()
        scans = [parse_qs(urlsplit(c.args[0]).query) for c in request.call_args_list
                 if urlsplit(c.args[0]).path.endswith("/issues/comments")]
        self.assertTrue(scans)
        self.assertNotIn("since", scans[0])

    def actions(self, apply=False):
        return {row["kind"]: row for row in self.cleaner.clean(apply)}

    def update(self, **fields):
        # Cleanup observes already ended/crashed runs, including expired leases.
        # Fixture edits do not act as a still-owning supervisor.
        edit_lease(self.github, self.lease, **fields)

    def hook(self, script, timeout=3):
        self.cleaner.config = replace(self.config, cleanup=CleanupHook((sys.executable, "-c", script), timeout))

    def test_preview_predicts_both_removals_without_running_hook_or_deleting(self):
        self.hook("raise AssertionError('preview must not run hook')")
        rows = self.actions()
        self.assertEqual([rows[k]["action"] for k in ("worktree", "branch")], ["would remove"] * 2)
        self.assertTrue(self.path.exists())
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.head)
        self.assertFalse((self.root / ".ub-agents" / "runs").exists())

    def test_apply_removes_worktree_and_local_branch_keeps_remote_and_logs(self):
        git(self.root, "update-ref", f"refs/heads/remote-copy", self.head)
        run_dir = self.root / ".ub-agents" / "runs" / self.lease["run"]
        run_dir.mkdir(parents=True)
        (run_dir / "keep.log").write_text("retain")
        rows = self.actions(True)
        self.assertEqual([rows[k]["action"] for k in ("worktree", "branch")], ["removed"] * 2)
        self.assertFalse(self.path.exists())
        self.assertEqual(git(self.root, "rev-parse", "refs/heads/remote-copy"), self.head)
        self.assertTrue((run_dir / "keep.log").exists())

    def test_locked_tracked_and_untracked_dirty_trees_keep_their_branches(self):
        for mode in ("locked", "tracked", "untracked"):
            with self.subTest(mode=mode):
                if mode == "locked":
                    git(self.root, "worktree", "lock", str(self.path))
                elif mode == "tracked":
                    (self.path / "file").write_text("dirty")
                else:
                    (self.path / "untracked").write_text("dirty")
                rows = self.actions(True)
                self.assertEqual(rows["worktree"]["action"], "kept")
                self.assertIn("locked" if mode == "locked" else "dirty", rows["worktree"]["reason"])
                self.assertEqual(rows["branch"]["action"], "kept")
                if mode == "locked":
                    git(self.root, "worktree", "unlock", str(self.path))
                elif mode == "tracked":
                    git(self.path, "restore", "file")
                else:
                    (self.path / "untracked").unlink()

    def test_ignored_files_do_not_make_tree_dirty(self):
        (self.path / "ignored").write_text("ignored")
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_trusted_other_account_on_this_host_is_eligible(self):
        self.github.roles["other"] = "write"
        self.github.store[1][0]["user"]["login"] = "other"
        self.assertEqual(self.actions()["worktree"]["action"], "would remove")
        self.assertEqual(self.actions(True)["branch"]["action"], "removed")

    def test_released_and_expired_leases_require_this_host(self):
        for state in ("released", "running"):
            for host in ("other-host", None):
                with self.subTest(state=state, host=host):
                    self.update(state=state, host=host, expires=iso(timestamp() - 60), process_group=1234)
                    with patch("ub_agents.cleanup.group_members", return_value=[]) as groups:
                        rows = self.actions(True)
                    self.assertTrue(all(row["action"] == "kept" for row in rows.values()))
                    self.assertIn("another host or has no recorded host", rows["worktree"]["reason"])
                    groups.assert_not_called()
        self.update(host=socket.gethostname())
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(self.actions()["worktree"]["action"], "would remove")

    def test_unlisted_writer_and_demoted_account_artifacts_are_kept(self):
        self.github.roles["other"] = "write"
        self.github.store[1][0]["user"]["login"] = "other"
        self.cleaner = Cleaner(replace(self.config, launchers=("operator",)), self.github, "operator", output=lambda _: None)
        self.assertTrue(all(row["action"] == "kept" for row in self.actions(True).values()))
        self.cleaner = Cleaner(self.config, self.github, "operator", output=lambda _: None)
        self.github.roles["other"] = "read"
        self.assertTrue(all(row["action"] == "kept" for row in self.actions(True).values()))

    def test_live_other_actor_missing_and_unconfirmed_leases_are_kept(self):
        for mode in ("live", "actor", "missing", "unconfirmed"):
            with self.subTest(mode=mode):
                original = deepcopy(self.github.store)
                original_lease = self.lease.copy()
                if mode == "live":
                    self.update(state="running", expires=iso(timestamp() + 60))
                elif mode == "actor":
                    self.github.login = "other"
                    self.github.store[1][0]["user"]["login"] = "other"
                elif mode == "missing":
                    self.github.store = {}
                else:
                    self.update(cleanup="unconfirmed")
                self.assertTrue(all(r["action"] == "kept" for r in self.cleaner.clean(True)))
                self.github.store = original
                self.github.login = "operator"
                self.lease.clear()
                self.lease.update(original_lease)

    def test_unreadable_github_keeps_artifacts(self):
        self.github.unreadable = True
        self.assertTrue(all(r["action"] == "kept" for r in self.cleaner.clean(True)))

    def test_expired_group_requires_record_and_confirmed_absence(self):
        self.update(state="running", expires=iso(timestamp() - 1))
        self.assertIn("no recorded process", self.actions()["worktree"]["reason"])
        self.update(process_group=123456)
        with patch("ub_agents.cleanup.group_members", return_value=[123456]):
            self.assertIn("still present", self.actions(True)["worktree"]["reason"])
        with patch("ub_agents.cleanup.group_members", side_effect=CleanupError("cannot inspect")):
            self.assertIn("cannot be confirmed", self.actions(True)["worktree"]["reason"])
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_expired_outcome_is_kept_until_recovery(self):
        self.update(state="running", expires=iso(timestamp() + 60))
        self.coordinator.report(self.lease, "retry", "reported")
        self.update(expires=iso(timestamp() - 1), process_group=123456)
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertIn("awaiting recovery", self.actions(True)["worktree"]["reason"])

    def hook_record(self, pid=None):
        directory = self.root / ".ub-agents" / "runs" / self.lease["run"] / "cleanup" / "crashed"
        directory.mkdir(parents=True)
        if pid is not None:
            (directory / "pid").write_text(str(pid))
        return directory

    def test_orphaned_real_hook_keeps_expired_run_until_group_is_gone(self):
        self.update(state="running", expires=iso(timestamp() - 1), process_group=123456)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True)
        self.addCleanup(stop_group, process)
        directory = self.hook_record(process.pid)
        self.hook("pass")
        with patch("ub_agents.cleanup.group_members", return_value=[]), \
                patch("ub_agents.cleanup.run_hook") as hook:
            for apply in (False, True):
                rows = self.actions(apply)
                self.assertTrue(all(r["action"] == "kept" for r in rows.values()))
                self.assertIn("hook process group", rows["worktree"]["reason"])
            hook.assert_not_called()
        self.assertTrue(self.path.exists())
        self.assertEqual(len(list(directory.parent.iterdir())), 1)
        stop_group(process)
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_released_branch_without_tree_still_checks_unfinished_hook(self):
        git(self.root, "worktree", "remove", str(self.path))
        self.hook_record(123456)
        with patch("ub_agents.hooks.group_members", return_value=[123456]):
            self.assertIn("hook process group", self.actions(True)["branch"]["reason"])

    def test_missing_malformed_and_redirected_hook_records_keep_artifacts(self):
        directory = self.hook_record()
        for value in (None, "not-a-pid", "0", "-1"):
            with self.subTest(value=value):
                if value is not None:
                    (directory / "pid").write_text(value)
                self.assertIn("cannot be confirmed", self.actions(True)["worktree"]["reason"])
        (directory / "pid").unlink()
        (self.root / "outside-pid").write_text("123456")
        (directory / "pid").symlink_to(self.root / "outside-pid")
        self.assertIn("redirected", self.actions(True)["worktree"]["reason"])
        (directory / "pid").unlink()
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        self.assertIn("uncertain hook", self.actions(True)["worktree"]["reason"])

    def test_unfinished_hook_process_inspection_failure_keeps_artifacts(self):
        self.hook_record(123456)
        with patch("ub_agents.hooks.group_members", side_effect=CleanupError("cannot inspect group")):
            self.assertIn("cannot inspect group", self.actions(True)["worktree"]["reason"])

    def test_confirmed_stop_marker_allows_retry_without_checking_reused_pid(self):
        directory = self.hook_record(123456)
        (directory / "stopped").write_text("confirmed\n")
        with patch("ub_agents.hooks.group_members", side_effect=AssertionError("already confirmed stopped")):
            self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_unreadable_stop_marker_keeps_artifacts(self):
        directory = self.hook_record(123456)
        (directory / "stopped").write_text("partial")
        self.assertIn("unreadable hook stop", self.actions(True)["worktree"]["reason"])

    def test_unregistered_and_redirected_paths_are_never_removed(self):
        extra = self.path.parent / "unknown"
        extra.mkdir()
        rows = self.cleaner.clean(True)
        self.assertTrue(extra.exists())
        self.assertIn("Unregistered", next(r["reason"] for r in rows if r["name"] == str(extra)))
        extra.rmdir()
        extra.symlink_to(self.root, target_is_directory=True)
        self.assertIn("redirects", next(r["reason"] for r in self.cleaner.clean(True) if r["name"] == str(extra)))

    def test_unknown_remote_tip_keeps_branch_after_worktree_removal(self):
        (self.path / "file").write_text("private commit")
        git(self.path, "add", "file")
        self.commit(self.path)
        # A separate remote excludes the local private branch.
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.root, "remote", "set-url", "origin", str(remote))
        git(self.root, "push", "origin", "main")
        rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertIn("not known remotely", rows["branch"]["reason"])

    def test_open_pr_keeps_branch_and_closed_pr_head_proves_squashed_tip(self):
        candidate = replace(pr(head=self.head), branch=self.branch)
        self.github.items[2] = candidate
        rows = self.actions(True)
        self.assertIn("open PR", rows["branch"]["reason"])
        git(self.root, "checkout", self.branch)
        (self.root / "file").write_text("checkpoint")
        git(self.root, "add", "file")
        self.commit(self.root)
        tip = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "main")
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.root, "remote", "set-url", "origin", str(remote))
        git(self.root, "push", "origin", "main", f"{tip}:refs/pull/2/head")
        self.github.change(2, state="closed", head=tip)
        self.assertEqual(self.actions(True)["branch"]["action"], "removed")

    def test_apply_rechecks_lease_and_worktree_after_preview_and_hook(self):
        original = self.cleaner.check
        calls = 0
        def race(artifact):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.update(state="running", expires=iso(timestamp() + 60))
            return original(artifact)
        with patch.object(self.cleaner, "check", side_effect=race):
            self.assertEqual(self.actions(True)["worktree"]["action"], "kept")
        self.update(state="released")
        self.hook("import os; from pathlib import Path; (Path(os.environ['UB_AGENTS_WORKTREE'])/'dirty').write_text('x')")
        self.assertIn("dirty", self.actions(True)["worktree"]["reason"])

    def test_successful_hook_receives_context_from_operator_checkout(self):
        output = self.root / ".ub-agents" / "context-copy.json"
        self.hook("import json,os; from pathlib import Path; "
                  "p=Path(os.environ['UB_AGENTS_CLEANUP_CONTEXT']); "
                  "v=json.loads(p.read_text()); v['cwd']=os.getcwd(); "
                  f"Path({str(output)!r}).write_text(json.dumps(v))")
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")
        context = json.loads(output.read_text())
        self.assertEqual(context["cwd"], str(self.root))
        self.assertEqual(context["worktree"], str(self.path))
        self.assertEqual(context["status"], "retry")
        self.assertEqual(context["branch"], self.branch)

    def test_failed_and_timed_out_hook_preserve_both_and_can_retry(self):
        for script, timeout, reason in (("import sys; sys.exit(3)", 3, "exited 3"),
                                        ("import time; time.sleep(30)", .05, "timed out")):
            with self.subTest(reason=reason):
                self.hook(script, timeout)
                rows = self.actions(True)
                self.assertIn(reason, rows["worktree"]["reason"])
                self.assertEqual(rows["branch"]["action"], "kept")
                self.assertTrue(self.path.exists())
                self.cleaner.blocked_runs.clear()
        self.hook("pass")
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_hook_unconfirmed_stop_preserves_artifacts_and_stops_command(self):
        self.hook("pass")
        with patch("ub_agents.hooks.supervise", side_effect=CleanupError("cannot confirm stop")):
            with self.assertRaises(CleanupError):
                self.actions(True)
        self.assertTrue(self.path.exists())
        self.assertIn("unconfirmed", self.actions()["worktree"]["reason"])

    def test_other_tools_and_unowned_branch_names_are_out_of_scope(self):
        legacy = self.root / ".claude" / "worktrees" / "legacy"
        git(self.root, "worktree", "add", "-b", "feature/legacy", str(legacy))
        self.assertFalse(any("legacy" in r["name"] for r in self.cleaner.clean(True)))
        self.assertTrue(legacy.exists())

    def test_later_live_lease_on_the_branch_protects_it_without_attached_tree(self):
        git(self.root, "worktree", "remove", str(self.path))
        current = self.lease.copy()
        current.pop("id")
        current.pop("url")
        current.update(run="continued", state="running", expires=iso(timestamp() + 60))
        from ub_agents.records import body
        self.github.create_comment(1, body(current))
        self.assertIn("live", self.actions(True)["branch"]["reason"])
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.head)

    def test_branch_checked_out_outside_private_tree_is_kept(self):
        git(self.root, "worktree", "remove", str(self.path))
        outside = self.root / "manual-worktree"
        git(self.root, "worktree", "add", str(outside), self.branch)
        self.assertIn("checked out", self.actions(True)["branch"]["reason"])
        self.assertTrue(outside.exists())

    def test_branch_owner_name_and_number_must_match_run_lease(self):
        git(self.root, "worktree", "remove", str(self.path))
        self.update(agent="another")
        self.assertIn("does not match", self.actions(True)["branch"]["reason"])

    def test_fetch_failure_keeps_branch(self):
        read = git
        def fail(root, *args):
            if args[:2] == ("fetch", "--prune"):
                raise AgentError("origin unavailable")
            return read(root, *args)
        with patch("ub_agents.cleanup.git", side_effect=fail):
            rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertIn("origin unavailable", rows["branch"]["reason"])

    def test_live_lease_and_new_open_pr_during_fetch_keep_branch(self):
        git(self.root, "worktree", "remove", str(self.path))
        read = git
        for change in ("lease", "pr"):
            with self.subTest(change=change):
                def race(root, *args):
                    result = read(root, *args)
                    if args[:2] == ("fetch", "--prune"):
                        if change == "lease":
                            self.update(state="running", expires=iso(timestamp() + 60))
                        else:
                            self.github.items[2] = replace(pr(), branch=self.branch)
                    return result
                with patch("ub_agents.cleanup.git", side_effect=race):
                    self.assertEqual(self.actions(True)["branch"]["action"], "kept")
                self.update(state="released")

    def test_atomic_branch_delete_refuses_a_new_tip(self):
        git(self.root, "worktree", "remove", str(self.path))
        read = git
        # Create a distinct commit without moving any checked-out branch.
        tree = read(self.root, "rev-parse", "HEAD^{tree}")
        import os
        import subprocess
        env = os.environ.copy() | {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                                   "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        tip = subprocess.run(["git", "-C", str(self.root), "-c", "commit.gpgsign=false",
                              "commit-tree", tree, "-p", self.head, "-m", "racing commit"],
                             env=env, check=True, text=True, capture_output=True).stdout.strip()
        def race(root, *args):
            if args[:2] == ("update-ref", "-d"):
                read(root, "update-ref", f"refs/heads/{self.branch}", tip)
            return read(root, *args)
        with patch("ub_agents.cleanup.git", side_effect=race):
            self.assertEqual(self.actions(True)["branch"]["action"], "kept")
        self.assertEqual(read(self.root, "rev-parse", self.branch), tip)

    def test_unreadable_and_deleted_fresh_records_do_not_use_discovery_snapshot(self):
        index, _ = self.cleaner.coordinator.repository_history()
        self.github.store = {}
        with patch.object(self.cleaner.coordinator, "repository_history", return_value=(index, set())):
            self.assertTrue(all(row["action"] == "kept" for row in self.cleaner.clean(True)))

    def test_cli_preview_and_apply_dispatch(self):
        from ub_agents.cli import main
        path = self.root / "ub-agents.yaml"
        path.write_text("repository: org/project\nagents:\n  worker:\n    command: [echo]\n    trigger: ready\n"
                        "    outcomes: {done: {}}\n")
        with patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cleanup.Cleaner.clean") as clean:
            self.assertEqual(main(["--config", str(path), "cleanup"]), 0)
            clean.assert_called_once_with(apply=False)
            clean.reset_mock()
            self.assertEqual(main(["--config", str(path), "cleanup", "--apply"]), 0)
            clean.assert_called_once_with(apply=True)

    def test_recovered_outcome_still_requires_original_process_group_absence(self):
        from ub_agents.records import body, payload
        self.update(state="running", expires=iso(timestamp() + 60))
        self.coordinator.report(self.lease, "retry", "reported before crash")
        self.update(expires=iso(timestamp() - 1), process_group=123456)
        recovery = payload(self.lease) | {"run": "recovered", "branch": None,
                                         "state": "released", "result": "retry",
                                         "recovered_lease_id": self.lease["id"]}
        self.github.create_comment(1, body(recovery))
        with patch("ub_agents.cleanup.group_members", return_value=[123456]):
            self.assertIn("still present", self.actions(True)["worktree"]["reason"])
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(self.actions(True)["worktree"]["action"], "removed")
