from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.errors import AgentError, CleanupError, LostOwnership
from ub_agents.execution import Workspace, command_for, git, group_members, supervise
from tests.support import agent, config, issue, pr, FakeGitHub


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_process(self, script, timeout=3, stop=None):
        return supervise([sys.executable, "-c", script], self.root, os.environ.copy(),
                         self.root / "run", timeout, stop or threading.Event())

    def test_thin_adapters_keep_argv_model_effort_and_explicit_permissions(self):
        configured = agent(self.root, command=(), runtime_args=("--sandbox", "read-only"))
        codex = command_for(configured, Runtime("codex", "configured-model", "high"))
        self.assertEqual(codex, ["codex", "exec", "--json", "--model", "configured-model", "--config",
                                 'model_reasoning_effort="high"', "--sandbox", "read-only"])
        claude = command_for(replace(configured, runtime_args=()), Runtime("claude", "opus", "high"))
        self.assertEqual(claude, ["claude", "--print", "--output-format", "stream-json", "--verbose",
                                  "--model", "opus", "--effort", "high"])
        extra = ("--verbose", "--allowedTools", "Bash(git *)")
        self.assertEqual(command_for(replace(configured, runtime_args=extra), Runtime("claude", "opus", "high")),
                         claude + list(extra))

    def test_prompt_cwd_environment_and_exit_are_delivered_to_recording_command(self):
        script = "import os,sys; print(os.getcwd()); print(os.environ['TEST_UB_CONTEXT']); print(sys.stdin.read())"
        env = os.environ.copy() | {"TEST_UB_CONTEXT": "context"}
        result = supervise([sys.executable, "-c", script], self.root, env, self.root / "run", 3,
                           threading.Event(), "project instructions")
        self.assertEqual(result, 0)
        log = (self.root / "run" / "process.log").read_text()
        self.assertIn("project instructions", log)
        self.assertIn("context", log)
        self.assertIn(str(self.root), log)

    def test_timeout_kills_uncooperative_owned_group(self):
        start = time.monotonic()
        with self.assertRaisesRegex(AgentError, "timed out"):
            self.run_process("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)", timeout=.1)
        pid = int((self.root / "run" / "pid").read_text())
        self.assertEqual(group_members(pid), [])
        self.assertLess(time.monotonic() - start, 6)

    def test_wall_clock_lease_expiry_ends_the_process(self):
        with self.assertRaisesRegex(LostOwnership, "lease deadline"):
            supervise([sys.executable, "-c", "import time; time.sleep(30)"], self.root, os.environ.copy(),
                      self.root / "run", 3, threading.Event(), expires=time.time() - 1)
        self.assertEqual(group_members(int((self.root / "run" / "pid").read_text())), [])

    def test_interruption_ends_the_process(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(KeyboardInterrupt):
            self.run_process("import time; time.sleep(30)", stop=stop)
        self.assertEqual(group_members(int((self.root / "run" / "pid").read_text())), [])

    def test_natural_leader_exit_reaps_surviving_group_child(self):
        script = """import os,signal,time
if os.fork() == 0:
    signal.alarm(10)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
else:
    time.sleep(.1)
"""
        self.assertEqual(self.run_process(script), 0)
        self.assertEqual(group_members(int((self.root / "run" / "pid").read_text())), [])

    def test_shared_workspace_is_never_removed(self):
        workspace = Workspace(config(self.root), agent(self.root), issue(), {"run": "private"}, FakeGitHub(issue()))
        with patch("ub_agents.execution.git", side_effect=AssertionError("shared cleanup")):
            workspace.cleanup()
        self.assertTrue(self.root.is_dir())

    def test_uncertain_cleanup_never_removes_the_private_worktree(self):
        workspace = Workspace(config(self.root), agent(self.root, worktree=True), issue(), {"run": "private"}, FakeGitHub(issue()))
        workspace.created = True
        with patch("ub_agents.execution.git", side_effect=AgentError("worktree removal failed")) as git:
            with self.assertRaises(CleanupError):
                workspace.cleanup()
            git.assert_called_once()
            self.assertTrue(workspace.created)

    def test_process_inspection_failure_still_terminates_the_known_owned_child(self):
        with patch("ub_agents.execution.group_members", side_effect=CleanupError("inspection failed")):
            with self.assertRaises(CleanupError):
                self.run_process("import time; time.sleep(30)", timeout=.05)
        pid = int((self.root / "run" / "pid").read_text())
        self.assertEqual(group_members(pid), [])

    def test_private_path_redirect_never_scans_or_signals_shared_directory(self):
        workspace = Workspace(config(self.root), agent(self.root, worktree=True), issue(), {"run": "private"}, FakeGitHub(issue()))
        workspace.private.parent.mkdir(parents=True)
        workspace.private.symlink_to(self.root, target_is_directory=True)
        workspace.created = True
        with patch("ub_agents.execution.git") as reap:
            with self.assertRaises(CleanupError):
                workspace.cleanup()
            reap.assert_not_called()

    def test_private_worktree_uses_exact_candidate_and_cleanup_preserves_shared_files(self):
        git(self.root, "init", "-b", "main")
        source = self.root / "file.txt"
        source.write_text("original")
        git(self.root, "add", "file.txt")
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "remote", "add", "origin", str(self.root))
        git(self.root, "update-ref", "refs/pull/2/head", head)
        candidate = pr(head=head)
        workspace = Workspace(config(self.root), agent(self.root, worktree=True), candidate,
                              {"run": "test-candidate"}, FakeGitHub(candidate))
        cwd = workspace.prepare()
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), head)
        (cwd / "file.txt").write_text("private edit")
        workspace.cleanup()
        self.assertEqual(source.read_text(), "original")
        self.assertFalse(cwd.exists())
        moved = Workspace(config(self.root), agent(self.root, worktree=True), pr(head="a" * 40),
                          {"run": "moved-candidate"}, FakeGitHub(candidate))
        with self.assertRaisesRegex(AgentError, "Candidate changed"):
            moved.prepare()
        self.assertFalse(moved.created)

    def test_process_group_is_recorded_before_execution_and_callback_failure_stops_child(self):
        groups = []
        self.assertEqual(supervise([sys.executable, "-c", "pass"], self.root, os.environ.copy(),
                         self.root / "recorded", 3, threading.Event(), process_started=groups.append), 0)
        self.assertEqual(groups, [int((self.root / "recorded" / "pid").read_text())])
        def fail(group):
            raise LostOwnership("Cannot persist process group")
        with self.assertRaises(LostOwnership):
            supervise([sys.executable, "-c", "import time; time.sleep(30)"], self.root,
                      os.environ.copy(), self.root / "failure", 3, threading.Event(), process_started=fail)
        self.assertEqual(group_members(int((self.root / "failure" / "pid").read_text())), [])
