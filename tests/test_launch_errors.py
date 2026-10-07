from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.config import CleanupHook
from ub_agents.errors import AgentError
from ub_agents.execution import Workspace, group_members, stop_group
from ub_agents.hooks import confirm_hook_groups_stopped, diagnostic, run_hook
from tests.support import FakeGitHub, agent, config, issue


class LaunchErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "ub-agents.yaml"
        self.path.touch()
        self.config = config(self.root)

    def invoke(self, operation, command="launch", *options):
        stderr = io.StringIO()
        with patch("ub_agents.cli.run", side_effect=lambda _: operation()), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = main(["--config", str(self.path), command, *options])
        self.assertEqual(code, 1)
        return stderr.getvalue()

    def assert_stop(self, operation, problem, step):
        message = self.invoke(operation)
        self.assertEqual(message, f"ub-agents: {problem}; {step}, then launch again\n")
        self.assertNotIn("preserve artifacts", message)

    def test_ps_failures_name_the_remedy_without_advising_other_commands_to_launch(self):
        cases = [
            (PermissionError("ps denied"), "Cannot inspect owned process group 321: ps denied", "make ps usable"),
            (subprocess.TimeoutExpired("ps", 5),
             "Cannot inspect owned process group 321: Command 'ps' timed out after 5 seconds", "make ps usable"),
            (subprocess.CompletedProcess([], 1, "", "ps unavailable\nsecond line"),
             "Cannot inspect owned process group 321: ps unavailable second line", "make ps usable"),
            (subprocess.CompletedProcess([], 0, "321 321\n", ""),
             "Unreadable process table", "make ps output readable"),
            (subprocess.CompletedProcess([], 0, "unknown 321 S\n", ""),
             "Unreadable process ids", "make ps output readable"),
        ]
        for result, problem, step in cases:
            with self.subTest(result=result):
                kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("ub_agents.execution.subprocess.run", **kwargs):
                    self.assert_stop(lambda: group_members(321), problem, step)
                    for command in ("status", "cleanup", "doctor"):
                        output = self.invoke(lambda: group_members(321), command)
                        self.assertNotIn("launch again", output)
                        self.assertNotIn("restart ub-agents launch", output)

    def test_signalling_failure_and_surviving_group_name_the_group_to_confirm(self):
        process = Mock(pid=321)
        with patch("ub_agents.execution.group_members", return_value=[321]), \
                patch("ub_agents.execution.os.killpg", side_effect=PermissionError("denied")):
            self.assert_stop(lambda: stop_group(process, grace=0),
                             "Cannot signal owned process group 321", "confirm process group 321 has exited")
        with patch("ub_agents.execution.group_members", return_value=[321]), \
                patch("ub_agents.execution.os.killpg"):
            self.assert_stop(lambda: stop_group(process, grace=0),
                             "Process group 321 survived termination", "confirm process group 321 has exited")

    def workspace(self):
        workspace = Workspace(self.config, agent(self.root, worktree=True), issue(),
                              {"run": "run"}, FakeGitHub(issue()))
        workspace.created = True
        return workspace

    def test_worktree_removal_failure_keeps_artifacts_and_names_the_remedy(self):
        workspace = self.workspace()
        with patch("ub_agents.execution.git", side_effect=AgentError("Git operation failed: denied\nsecond line")):
            self.assert_stop(workspace.cleanup,
                             f"Cannot remove owned private worktree {workspace.private}: Git operation failed: denied second line",
                             "resolve the worktree removal error")
        self.assertTrue(workspace.created)

    def test_redirected_worktree_path_names_the_path_to_restore(self):
        workspace = self.workspace()
        workspace.private.parent.mkdir(parents=True)
        workspace.private.symlink_to(self.root, target_is_directory=True)
        with patch("ub_agents.execution.git") as remove:
            self.assert_stop(workspace.cleanup,
                             f"Private worktree path {workspace.private} redirects outside its owned directory",
                             "restore the owned worktree path without redirects")
        remove.assert_not_called()
        self.assertTrue(workspace.created)

    def test_present_cleanup_hook_group_names_the_group_to_confirm(self):
        directory = self.root / ".ub-agents" / "runs" / "run" / "cleanup" / "hook"
        directory.mkdir(parents=True)
        (directory / "pid").write_text("321")
        with patch("ub_agents.hooks.group_members", return_value=[321]):
            self.assert_stop(lambda: confirm_hook_groups_stopped(self.config, "run"),
                             "Cleanup hook process group 321 is still present", "confirm process group 321 has exited")
        self.assertTrue(directory.is_dir())

    def test_unconfirmed_hook_records_name_the_records_to_repair(self):
        parent = self.root / ".ub-agents" / "runs" / "run" / "cleanup"
        directory = parent / "hook"
        directory.mkdir(parents=True)
        for text, problem in ((None, "hook has no recorded process group or confirmed stop"),
                              ("0", "invalid hook process group"),
                              ("unknown", "invalid literal for int() with base 10: 'unknown'")):
            with self.subTest(text=text):
                if text is not None:
                    (directory / "pid").write_text(text)
                self.assert_stop(lambda: confirm_hook_groups_stopped(self.config, "run"),
                                 f"Cleanup hook process check cannot be confirmed: {problem}",
                                 f"confirm the hook has exited and repair its process records in {parent}")

    def test_redirected_diagnostics_name_the_path_to_restore(self):
        directory = self.root / ".ub-agents" / "runs" / "run"
        directory.parent.mkdir(parents=True)
        directory.symlink_to(self.root, target_is_directory=True)
        self.assert_stop(lambda: diagnostic(self.config, "run", "test"),
                         f"Run diagnostics path {directory} redirects",
                         "restore the run diagnostics path without redirects")
        directory.unlink()
        directory.mkdir()
        events = directory / "events.jsonl"
        events.symlink_to(self.path)
        self.assert_stop(lambda: diagnostic(self.config, "run", "test"),
                         f"Run diagnostics file {events} redirects",
                         "restore the run diagnostics file without redirects")
        self.assertEqual(self.path.read_text(), "")

    def test_hook_diagnostics_redirect_after_confirmation_stops_before_spawning(self):
        directory = self.root / ".ub-agents" / "runs" / "run" / "cleanup" / "hook"

        def redirect():
            directory.parent.mkdir(parents=True)
            directory.symlink_to(self.root, target_is_directory=True)
            return Mock(hex="hook")

        cfg = replace(self.config, cleanup=CleanupHook(("cleanup-command",), 3))
        with patch("ub_agents.hooks.uuid.uuid4", side_effect=redirect), \
                patch("ub_agents.hooks.supervise") as execute:
            self.assert_stop(lambda: run_hook(cfg, {"run": "run"}, self.root),
                             f"Hook diagnostics path {directory} redirects",
                             "restore the hook diagnostics path without redirects")
        execute.assert_not_called()
