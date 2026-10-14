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
        self.assertNotIn("unpushed tip", rows["branch"]["reason"])

    def test_locked_tracked_and_untracked_dirty_trees_are_removed(self):
        for mode in ("locked", "tracked", "untracked"):
            with self.subTest(mode=mode):
                if not self.path.exists():
                    git(self.root, "worktree", "add", "-b", self.branch, str(self.path), "HEAD")
                if mode == "locked":
                    git(self.root, "worktree", "lock", str(self.path))
                elif mode == "tracked":
                    (self.path / "file").write_text("dirty")
                else:
                    (self.path / "untracked").write_text("dirty")
                rows = self.actions(True)
                self.assertTrue(all(row["action"] == "removed" for row in rows.values()))
                self.assert_removed()

    def test_ignored_files_do_not_make_tree_dirty(self):
        (self.path / "ignored").write_text("ignored")
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_trusted_other_account_on_this_host_is_eligible(self):
        self.github.roles["other"] = "write"
        self.github.store[1][0]["user"]["login"] = "other"
        self.assertEqual(self.actions()["worktree"]["action"], "would remove")
        self.assertEqual(self.actions(True)["branch"]["action"], "removed")

    def test_other_or_missing_host_leases_do_not_keep_artifacts(self):
        for state in ("released", "running", "claiming"):
            for host in ("other-host", None):
                with self.subTest(state=state, host=host):
                    self.update(state=state, host=host, expires=iso(timestamp() + 60), process_group=1234)
                    with patch("ub_agents.cleanup.group_members", return_value=[]) as groups:
                        rows = self.actions()
                    self.assertTrue(all(row["action"] == "would remove" for row in rows.values()))
                    groups.assert_not_called()
        self.assertTrue(all(row["action"] == "removed" for row in self.actions(True).values()))

    def test_untrusted_leases_do_not_keep_artifacts(self):
        self.github.roles["other"] = "write"
        self.github.store[1][0]["user"]["login"] = "other"
        self.cleaner = Cleaner(replace(self.config, launchers=("operator",)), self.github, "operator", output=lambda _: None)
        self.assertTrue(all(row["action"] == "would remove" for row in self.actions().values()))
        self.cleaner = Cleaner(self.config, self.github, "operator", output=lambda _: None)
        self.github.roles["other"] = "read"
        self.assertTrue(all(row["action"] == "removed" for row in self.actions(True).values()))

    def test_live_local_and_unconfirmed_leases_are_kept(self):
        for mode in ("live", "unconfirmed"):
            with self.subTest(mode=mode):
                original = deepcopy(self.github.store)
                original_lease = self.lease.copy()
                if mode == "live":
                    self.update(state="running", expires=iso(timestamp() + 60))
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

    def test_missing_older_and_unreadable_leases_are_removed_for_every_role(self):
        from ub_agents.records import MARKER
        original = deepcopy(self.github.store)
        for role in ("implementer", "issue-preparer", "custom"):
            for mode in ("missing", "v2", "unreadable"):
                with self.subTest(role=role, mode=mode):
                    branch = f"ub-agents/{role}/1/{self.lease['run']}"
                    git(self.path, "branch", "-m", branch)
                    self.branch = branch
                    self.github.store = deepcopy(original)
                    if mode == "missing":
                        self.github.store = {}
                    elif mode == "v2":
                        self.github.store[1][0]["body"] = original[1][0]["body"].replace(MARKER, "<!-- ub-agents:v2 -->")
                    else:
                        self.github.store[1][0]["body"] = MARKER + "\nunreadable"
                    rows = self.actions(True)
                    self.assertTrue(all(row["action"] == "removed" for row in rows.values()))
                    self.assert_removed()
                    git(self.root, "worktree", "add", "-b", self.branch, str(self.path), "HEAD")

    def test_legacy_branch_prefix_is_removed_on_launch(self):
        branch = f"ub-agent/worker/1/{self.lease['run']}"
        git(self.path, "branch", "-m", branch)
        self.branch = branch
        self.github.store = {}
        lines = self.launch_leftovers(self.assert_removed)
        self.assertEqual(lines[1], f"removed branch {branch}: No live run or open PR")

    def test_live_trusted_other_actor_on_this_host_keeps_artifacts(self):
        self.github.roles["other"] = "write"
        self.github.store[1][0]["user"]["login"] = "other"
        self.update(state="running", expires=iso(timestamp() + 60))
        self.assertTrue(all("lease is live" in row["reason"] for row in self.cleaner.clean(True)))

    def test_invalid_record_does_not_hide_live_lease_in_same_or_other_thread(self):
        from ub_agents.records import MARKER
        self.github.items[3] = issue(3)
        for number in (1, 3):
            with self.subTest(number=number):
                comment = self.github.create_comment(number, "fixture")
                self.github.update_comment(comment["id"], MARKER + "\nunreadable")
                self.update(state="claiming", expires=iso(timestamp() + 60))
                self.assertTrue(all("lease is live" in row["reason"] for row in self.cleaner.clean(True)))

    def test_pr_lookup_and_coordination_scan_failures_keep_each_artifact(self):
        for target in ("prs_for_branch", "repository_comments", "comments"):
            with self.subTest(target=target), patch.object(self.github, target, side_effect=AgentError("GitHub read failed")):
                rows = self.cleaner.clean(True)
                self.assertEqual(len(rows), 2)
                protected = rows if target != "prs_for_branch" else [rows[-1]]
                self.assertTrue(all(row["action"] == "kept" for row in protected))
                self.assertTrue(all("GitHub read failed" in row["reason"] for row in protected))
                if not self.path.exists():
                    git(self.root, "worktree", "add", str(self.path), self.branch)

    def test_local_unconfirmed_diagnostics_keep_artifacts_without_a_lease(self):
        from ub_agents.hooks import diagnostic
        self.github.store = {}
        diagnostic(self.config, self.lease["run"], "cleanup-unconfirmed", error="uncertain")
        self.assertTrue(all("local diagnostics" in row["reason"] for row in self.cleaner.clean(True)))

    def test_unregistered_live_run_folder_is_kept_then_removed_when_ended(self):
        git(self.root, "worktree", "remove", str(self.path))
        (self.path / "packages").mkdir(parents=True)
        (self.path / "packages" / "leftover").write_text("old")
        self.update(state="running", expires=iso(timestamp() + 60))
        self.assertTrue(all("lease is live" in row["reason"] for row in self.cleaner.clean(True)))
        self.update(state="released")
        self.assertTrue(all(row["action"] == "removed" for row in self.cleaner.clean(True)))

    def test_missing_lease_still_checks_unfinished_cleanup_and_setup_groups(self):
        self.github.store = {}
        directory = self.hook_record(123456)
        with patch("ub_agents.hooks.group_members", return_value=[123456]):
            self.assertTrue(all("hook process group" in row["reason"] for row in self.cleaner.clean(True)))
        (directory / "stopped").write_text("confirmed\n")
        setup = directory.parent.parent / "checkout-setup"
        setup.mkdir()
        (setup / "pid").write_text("123456")
        with patch("ub_agents.worktree_setup.group_members", return_value=[123456]):
            self.assertTrue(all("setup process group" in row["reason"] for row in self.cleaner.clean(True)))

    def test_expired_group_requires_record_and_confirmed_absence(self):
        self.update(state="running", expires=iso(timestamp() - 1))
        self.assertEqual(self.actions()["worktree"]["action"], "would remove")
        self.update(process_group=123456)
        with patch("ub_agents.cleanup.group_members", return_value=[123456]):
            self.assertIn("still present", self.actions(True)["worktree"]["reason"])
        with patch("ub_agents.cleanup.group_members", side_effect=CleanupError("cannot inspect")):
            self.assertIn("cannot be confirmed", self.actions(True)["worktree"]["reason"])
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_expired_outcome_with_confirmed_absent_group_is_removed(self):
        self.update(state="running", expires=iso(timestamp() + 60))
        self.coordinator.report(self.lease, "retry", "reported")
        self.update(expires=iso(timestamp() - 1), process_group=123456)
        with patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertTrue(all(row["action"] == "removed" for row in self.actions(True).values()))

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

    def test_unregistered_folders_and_symlinks_are_removed_without_following(self):
        extra = self.path.parent / "unknown"
        (extra / "packages").mkdir(parents=True)
        (extra / "packages" / "leftover").write_text("orphan")
        rows = self.cleaner.clean(True)
        self.assertFalse(extra.exists())
        self.assertEqual(next(r["action"] for r in rows if r["name"] == str(extra)), "removed")
        extra.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.cleaner.clean(True)[0]["action"], "removed")
        self.assertFalse(extra.is_symlink())
        self.assertTrue((self.root / "file").exists())
        extra.symlink_to(self.root / "missing", target_is_directory=True)
        self.assertEqual(self.cleaner.clean(True)[0]["action"], "removed")
        self.assertFalse(extra.is_symlink())

    def test_unknown_remote_tip_is_removed_with_restoration_sha(self):
        (self.path / "file").write_text("private commit")
        git(self.path, "add", "file")
        self.commit(self.path)
        tip = git(self.path, "rev-parse", "HEAD")
        # A separate remote excludes the local private branch.
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.root, "remote", "set-url", "origin", str(remote))
        git(self.root, "push", "origin", "main")
        output = []
        self.cleaner.output = output.append
        rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertEqual(rows["branch"]["action"], "removed")
        self.assertEqual(output[-1], f"removed branch {self.branch}: No live run or open PR; unpushed tip {tip}")
        self.assert_removed()
        git(self.root, "branch", self.branch, tip)
        self.assertEqual(git(self.root, "rev-parse", self.branch), tip)

    def test_open_pr_keeps_branch_removes_ended_tree_and_merged_head_proves_squashed_tip(self):
        candidate = replace(pr(head=self.head), branch=self.branch)
        self.github.items[2] = candidate
        rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
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
        self.github.change(2, state="closed", head=tip, merged=True)
        row = self.actions(True)["branch"]
        self.assertEqual(row["action"], "removed")
        self.assertNotIn("unpushed tip", row["reason"])

    def test_unmerged_or_unverified_pr_head_does_not_suppress_restoration_sha(self):
        (self.path / "file").write_text("private checkpoint")
        git(self.path, "add", "file")
        self.commit(self.path)
        tip = git(self.path, "rev-parse", "HEAD")
        remote = self.root / "remote.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.root, "remote", "set-url", "origin", str(remote))
        git(self.root, "push", "origin", "main", f"{tip}:refs/pull/2/head")
        for mode in ("unmerged", "changed", "missing"):
            with self.subTest(mode=mode):
                self.github.items[2] = replace(pr(head=tip if mode == "unmerged" else self.head),
                                                branch=self.branch, state="closed", merged=mode != "unmerged")
                if mode == "missing":
                    git(self.root, "push", "origin", ":refs/pull/2/head")
                self.assertIn(f"unpushed tip {tip}", self.actions()["branch"]["reason"])
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
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")

    def test_hook_created_live_lease_keeps_worktree_and_open_pr_keeps_only_branch(self):
        def hook(*args):
            self.update(state="running", expires=iso(timestamp() + 60))
        with patch("ub_agents.cleanup.run_hook", side_effect=hook):
            self.assertTrue(all(row["action"] == "kept" for row in self.cleaner.clean(True)))
        self.update(state="released")
        def open_pr(*args):
            self.github.items[2] = replace(pr(head=self.head), branch=self.branch)
        with patch("ub_agents.cleanup.run_hook", side_effect=open_pr):
            rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertEqual(rows["branch"]["action"], "kept")
        self.assertFalse(self.path.exists())

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

    def test_missing_lease_and_unregistered_folder_hook_receive_null_context(self):
        self.github.store = {}
        output = self.root / ".ub-agents" / "context-copy.json"
        self.hook("import json,os; from pathlib import Path; "
                  f"Path({str(output)!r}).write_text(Path(os.environ['UB_AGENTS_CLEANUP_CONTEXT']).read_text())")
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")
        context = json.loads(output.read_text())
        self.assertEqual(context["run"], self.lease["run"])
        self.assertEqual(context["branch"], self.branch)
        for field in ("agent", "assignment", "kind", "handoff", "status", "outcome"):
            self.assertIsNone(context[field])
        self.path.mkdir()
        self.assertEqual(self.actions(True)["worktree"]["action"], "removed")
        self.assertIsNone(json.loads(output.read_text())["branch"])

    def test_failed_hook_keeps_detached_and_unregistered_worktree_and_branch(self):
        for mode in ("detached", "unregistered"):
            with self.subTest(mode=mode):
                if mode == "detached":
                    git(self.path, "checkout", "--detach")
                else:
                    git(self.root, "worktree", "remove", str(self.path))
                    self.path.mkdir()
                    self.github.store = {}
                self.hook("raise SystemExit(3)")
                self.assertTrue(all(row["action"] == "kept" for row in self.cleaner.clean(True)))
                self.assertTrue(self.path.exists())

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

    def test_hook_unconfirmed_stop_preserves_artifacts_and_reports_each(self):
        self.hook("pass")
        with patch("ub_agents.hooks.supervise", side_effect=CleanupError("cannot confirm stop")):
            rows = self.actions(True)
        self.assertTrue(all(row["action"] == "kept" for row in rows.values()))
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

    def test_branch_continued_on_other_hosts_is_removed_without_process_checks(self):
        from ub_agents.records import body, payload
        self.github.items[2] = replace(pr(head=self.head), branch=self.branch, state="closed", merged=True)
        for host, run in (("other-a", "continued-a"), ("other-b", "continued-b")):
            continued = payload(self.lease) | {"assignment": 2, "assignment_sha": self.head,
                                                "run": run, "host": host, "state": "running",
                                                "expires": iso(timestamp() + 60), "process_group": 123456}
            self.github.create_comment(2, body(continued))
        with patch("ub_agents.cleanup.group_members", side_effect=AssertionError("not a local group")):
            self.assertTrue(all(row["action"] == "removed" for row in self.cleaner.clean(True)))

    def test_continued_local_run_protects_branch_even_with_missing_original_lease(self):
        from ub_agents.records import body, payload
        continued = payload(self.lease) | {"run": "continued", "state": "running",
                                            "expires": iso(timestamp() + 60)}
        self.github.store = {}
        self.github.create_comment(1, body(continued))
        rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertIn("lease is live", rows["branch"]["reason"])

    def test_shared_branch_unconfirmed_cleanup_keeps_other_artifacts(self):
        from ub_agents.records import body, payload
        continued = payload(self.lease) | {"run": "continued", "host": "other-host", "cleanup": "unconfirmed"}
        self.github.create_comment(1, body(continued))
        rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertIn("cleanup is unconfirmed", rows["branch"]["reason"])
        self.assertEqual(git(self.root, "rev-parse", self.branch), self.head)

    def test_branch_checked_out_outside_private_tree_is_kept(self):
        git(self.root, "worktree", "remove", str(self.path))
        outside = self.root / "manual-worktree"
        git(self.root, "worktree", "add", str(outside), self.branch)
        self.assertIn("checked out", self.actions(True)["branch"]["reason"])
        self.assertTrue(outside.exists())

    def test_unneeded_branch_with_mismatched_lease_is_removed(self):
        git(self.root, "worktree", "remove", str(self.path))
        self.update(agent="another")
        self.assertEqual(self.actions(True)["branch"]["action"], "removed")

    def test_fetch_failure_removes_branch_with_restoration_sha(self):
        read = git
        def fail(root, *args):
            if args[:2] == ("fetch", "--prune"):
                raise AgentError("origin unavailable")
            return read(root, *args)
        with patch("ub_agents.cleanup.git", side_effect=fail):
            rows = self.actions(True)
        self.assertEqual(rows["worktree"]["action"], "removed")
        self.assertEqual(rows["branch"]["action"], "removed")
        self.assertIn(f"unpushed tip {self.head}", rows["branch"]["reason"])

    def test_live_lease_and_new_open_pr_during_fetch_keep_branch(self):
        git(self.root, "worktree", "remove", str(self.path))
        read = git
        for change in ("lease", "pr", "checkout"):
            with self.subTest(change=change):
                def race(root, *args):
                    result = read(root, *args)
                    if args[:2] == ("fetch", "--prune"):
                        if change == "lease":
                            self.update(state="running", expires=iso(timestamp() + 60))
                        elif change == "pr":
                            self.github.items[2] = replace(pr(), branch=self.branch)
                        else:
                            git(self.root, "worktree", "add", str(self.path), self.branch)
                    return result
                with patch("ub_agents.cleanup.git", side_effect=race):
                    self.assertEqual(self.actions(True)["branch"]["action"], "kept")
                self.update(state="released")
                self.github.items.pop(2, None)

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
            self.assertTrue(all(row["action"] == "removed" for row in self.cleaner.clean(True)))

    def launch_leftovers(self, poll):
        from contextlib import redirect_stdout
        import io
        from ub_agents.cli import main
        from tests.support import MemoryPublisher, isolate_runtime_state
        isolate_runtime_state(self)
        path = self.root / "ub-agents.yaml"
        path.write_text("repository: org/project\nagents:\n  worker:\n    command: [echo]\n    trigger: ready\n"
                        "    outcomes: {done: {}}\n")
        with patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.launch_checks"), \
                patch("ub_agents.updates.Updates", return_value=None), \
                patch("ub_agents.observations.Publisher", MemoryPublisher), \
                patch("ub_agents.cli.Loop.tick", side_effect=poll), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--config", str(path), "launch", "--once", "--no-ui"]), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in
                          (self.root / ".ub-agents/launch.log").read_text().splitlines()], lines)
        return lines

    def test_launch_removes_leftovers_and_logs_before_the_first_poll(self):
        lines = self.launch_leftovers(self.assert_removed)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(f"removed worktree {self.path.relative_to(self.root)}:"))
        self.assertTrue(lines[1].startswith(f"removed branch {self.branch}:"))

    def test_launch_reports_kept_branch_after_uncertain_hook_and_still_polls(self):
        def poll():
            self.assertTrue(self.path.exists())
            self.assertEqual(git(self.root, "rev-parse", self.branch), self.head)
            return True

        with patch("ub_agents.cleanup.run_hook", side_effect=CleanupError("Hook termination unknown")):
            lines = self.launch_leftovers(poll)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], f"kept worktree {self.path.relative_to(self.root)}: Hook termination unknown")
        self.assertEqual(lines[1], f"kept branch {self.branch}: Cleanup hook failed; artifacts preserved")

    def assert_removed(self):
        self.assertFalse(self.path.exists())
        self.assertNotIn(self.branch, git(self.root, "branch", "--format=%(refname:short)").splitlines())
        return True

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
