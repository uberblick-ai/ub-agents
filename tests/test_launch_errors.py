from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.cli.launch import launch_error_message
from ub_agents.config import CleanupHook
from ub_agents.errors import (AgentError, CheckoutRefreshError, CleanupError, ConfigurationError,
                              GitHubError, InstructionError, LostOwnership, RecordError,
                              RetryableExecutionError, RuntimePaused)
from ub_agents.execution import Workspace, group_members, stop_group
from ub_agents.github import GitHub
from ub_agents.hooks import confirm_hook_groups_stopped, diagnostic, run_hook
from ub_agents.logs.launch_log import launch_output
from ub_agents.updates.self_updates import RestartLauncher
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
        self.assertEqual(message, f"ub-agents: Cleanup could not finish safely. {step[0].upper() + step[1:]}, "
                         f"then launch again. Here is the error: {problem}\n")
        self.assertNotIn("preserve artifacts", message)

    def test_ps_failures_name_the_remedy_without_advising_other_commands_to_launch(self):
        cases = [
            (PermissionError("ps denied"), "Cannot inspect owned process group 321: ps denied", "make ps usable"),
            (subprocess.TimeoutExpired("ps", 5),
             "Cannot inspect owned process group 321: Command 'ps' timed out after 5 seconds", "make ps usable"),
            (subprocess.CompletedProcess([], 1, "", "ps unavailable\nsecond line"),
             "Cannot inspect owned process group 321: ps unavailable", "make ps usable"),
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
                    for command in ("status", "doctor"):
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
                             f"Cannot remove owned private worktree {workspace.private}: Git operation failed: denied",
                             f"remove {workspace.private} by hand")
        self.assertTrue(workspace.created)

    def test_redirected_worktree_path_names_the_path_to_restore(self):
        workspace = self.workspace()
        workspace.private.parent.mkdir(parents=True)
        workspace.private.symlink_to(self.root, target_is_directory=True)
        with patch("ub_agents.execution.git") as remove:
            self.assert_stop(workspace.cleanup,
                             f"Private worktree path {workspace.private} redirects outside its owned directory",
                             f"restore {workspace.private} without redirects")
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
                         f"restore {directory} without redirects")
        directory.unlink()
        directory.mkdir()
        events = directory / "events.jsonl"
        events.symlink_to(self.path)
        self.assert_stop(lambda: diagnostic(self.config, "run", "test"),
                         f"Run diagnostics file {events} redirects",
                         f"restore {events} without redirects")
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
                             f"restore {directory} without redirects")
        execute.assert_not_called()

    def test_error_categories_have_one_shape_and_a_concrete_action(self):
        cases = (
            (CheckoutRefreshError("fetch failed", next_step="Run git fetch origin, then launch again"),
             "The control checkout could not be refreshed", "git fetch origin"),
            (CheckoutRefreshError("broken checkout"), "The control checkout could not be refreshed", "git status"),
            (CleanupError("Directory not empty", next_step="Remove .ub-agents/work/794 by hand"),
             "Cleanup could not finish safely", ".ub-agents/work/794"),
            (GitHubError("GET", "repos/acme/app/issues/12", "gh: Not Found (HTTP 404)"),
             "GitHub rejected a request that ub-agents does not retry", "`gh auth status`"),
            (GitHubError("GET", "repos/acme/app", "Connection reset", retryable=True),
             "A GitHub request failed", "`gh auth status`"),
            (InstructionError("policy.md is unreadable"), "An instruction file is invalid or unreadable", "ub-agents.yaml"),
            (ConfigurationError("max-attempts must be positive", path=self.path), "ub-agents.yaml is invalid", str(self.path)),
            (RecordError("Malformed comment 123"), "A coordination record is invalid", "ub-agents status"),
            (LostOwnership("Lease expired"), "The launcher lost ownership of its run", "ub-agents status"),
            (RetryableExecutionError("CLI failed"), "An agent runtime could not run", "ub-agents doctor"),
            (RuntimePaused("Limit reached"), "An agent runtime could not run", "ub-agents doctor"),
            (AgentError("Unknown failure\nextra detail"), "The launcher could not continue", "ub-agents doctor"),
            (PermissionError(13, "Permission denied", "policy.md"), "A local operation failed", "policy.md"),
            (RuntimeError("unexpected\nextra detail"), "The launcher stopped unexpectedly", "Please report it with launch.log"),
        )
        for error, problem, action in cases:
            with self.subTest(error=error):
                message = launch_error_message(error, self.root)
                self.assertTrue(message.startswith(problem + ". "), message)
                recovery, detail = message.split(". Here is the error: ")
                self.assertIn(action, recovery)
                self.assertTrue(detail)
                self.assertEqual(len(message.splitlines()), 1)
                self.assertNotIn("extra detail", message)

    def test_wrapped_errors_keep_the_original_remedy_and_request(self):
        cleanup = CleanupError("cannot inspect group 321", next_step="Confirm process group 321 has exited")
        github = GitHubError("GET", "repos/acme/app", "gh: Not Found (HTTP 404)")
        for original in (cleanup, github):
            wrapper = AgentError("Repeated error; fix the cause")
            wrapper.__cause__ = original
            self.assertEqual(launch_error_message(wrapper, self.root), launch_error_message(original, self.root))
        fallback = OSError("fallback signal failed")
        fallback.__context__ = cleanup
        self.assertEqual(launch_error_message(fallback, self.root), launch_error_message(cleanup, self.root))

    def test_github_output_is_short_on_terminal_and_complete_in_log(self):
        stdout = 'HTTP/2 404 Not Found\n\n{"message":"Not Found","documentation_url":"https://example.com/help"}'
        stderr = "gh: Not Found (HTTP 404)\nRepeated detail\nRun gh auth login"
        github = GitHub("acme/app", runner=Mock(return_value=subprocess.CompletedProcess([], 1, stdout, stderr)))
        message = self.invoke(lambda: github.request("repos/acme/app/issues/12"))
        self.assertTrue(message.endswith("Here is the error: GET repos/acme/app/issues/12: HTTP 404 Not Found\n"), message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertNotIn("gh:", message)
        self.assertNotIn("Repeated detail", message)
        log = (self.root / ".ub-agents/launch.log").read_text()
        for line in (stdout + "\n" + stderr).splitlines():
            self.assertIn(line, log)
        self.assertTrue(log.endswith(message))

    def test_authentication_advice_and_duplicate_status_are_removed(self):
        error = GitHubError("GET", "user", "Authentication failed (HTTP 401); check gh auth status: "
                            "gh: Bad credentials (HTTP 401)\nExtra gh advice", status=401)
        message = launch_error_message(error, self.root)
        self.assertTrue(message.endswith("GET user: HTTP 401 Bad credentials"), message)
        self.assertEqual(message.count("gh auth status"), 1)

    def test_invalid_configuration_names_the_files_to_fix_before_starting(self):
        self.path.write_text('repository: acme/app\nagents:\n  worker:\n    command: [echo]\n'
                             '    trigger: ready\n    outcomes: {done: {}}\n    max-attempts: 2\n')
        local = self.root / "ub-agents.local.yaml"
        for text in ('agents: [', 'agents: {worker: {max-attempts: 0}}'):
            with self.subTest(text=text):
                local.write_text(text)
                stderr = io.StringIO()
                with redirect_stderr(stderr), patch("ub_agents.cli._runtime.GitHub") as github:
                    self.assertEqual(main(["--config", str(self.path), "launch"]), 1)
                self.assertIn(str(local), stderr.getvalue().split(". Here is the error: ")[0])
                self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                github.assert_not_called()

    def test_gh_authentication_instructions_stay_out_of_the_error_part(self):
        github = GitHub("acme/app", runner=Mock(return_value=subprocess.CompletedProcess(
            [], 4, "", "To get started with GitHub CLI, please run: gh auth login\nAlternatively, set a token")))
        message = self.invoke(lambda: github.request("user"))
        self.assertTrue(message.endswith("GET user: GitHub CLI authentication is required\n"), message)
        self.assertNotIn("please run", message)

    def test_unexpected_exception_exits_one_and_logs_traceback(self):
        def fail():
            raise ValueError("unexpected value\nfull details")
        message = self.invoke(fail)
        self.assertEqual(message, "ub-agents: The launcher stopped unexpectedly. This looks like a bug in ub-agents. "
                         "Please report it with launch.log. Here is the error: unexpected value\n")
        log = (self.root / ".ub-agents/launch.log").read_text()
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("raise ValueError", log)
        self.assertIn("full details", log)
        self.assertTrue(log.endswith(message))

    def test_unexpected_resource_close_failure_is_also_reported(self):
        @contextmanager
        def resources():
            yield
            raise ValueError("resource close failed")
        with patch("ub_agents.launcher_code.startup_copy", resources):
            message = self.invoke(lambda: 0)
        self.assertIn("Please report it with launch.log. Here is the error: resource close failed", message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertIn("Traceback", (self.root / ".ub-agents/launch.log").read_text())

    def test_restart_failure_uses_the_same_shape_and_logs_details(self):
        restart = RestartLauncher("/new/ub-agents", None, self.root)
        with patch("ub_agents.cli.launch.os.execv", side_effect=OSError("Cannot execute\nfull details")):
            message = self.invoke(lambda: (_ for _ in ()).throw(restart))
        self.assertEqual(message, "ub-agents: The automatic launcher restart failed. Run ub-agents launch again by hand. "
                         "Here is the error: Cannot execute\n")
        log = (self.root / ".ub-agents/launch.log").read_text()
        self.assertIn("full details", log)
        self.assertTrue(log.endswith(message))

    def test_log_only_diagnostics_do_not_replace_the_terminal_last_line(self):
        terminal = io.StringIO()
        with redirect_stderr(terminal), launch_output(self.root) as output:
            output.hide()
            output.error(RuntimeError("full detail\nsecond line"))
            print("ub-agents: short exit", file=output.stderr)
            output.resume(final=True)
        self.assertEqual(terminal.getvalue(), "ub-agents: short exit\n")

    def test_long_causes_are_short_on_terminal_and_complete_in_log(self):
        failure = RuntimeError("x" * 2000)
        message = self.invoke(Mock(side_effect=failure))
        self.assertEqual(len(message.split("Here is the error: ")[1].strip()), 300)
        self.assertIn("x" * 2000, (self.root / ".ub-agents/launch.log").read_text())
