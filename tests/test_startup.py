from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import AgentError
from tests.support import DoctorGitHub, MemoryPublisher, RecordingRunner


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "ub-agents.yaml"
        self.path.write_text("""repository: org/project
agents:
  worker:
    command: [git, --version]
    trigger: [ready, start]
    outcomes:
      done: {add: [review], remove: [old]}
""")
        self.runner = RecordingRunner(self.root)
        self.github = DoctorGitHub()
        self.github.label_names = ["ready", "start", "review", "old", "needs-human"]

    def response(self, arguments, value):
        self.runner.responses[("git", "-C", str(self.root), *arguments)] = value

    def read_git(self, root, *arguments):
        self.assertEqual(root, self.root)
        result = self.runner(["git", "-C", str(root), *arguments])
        if result.returncode:
            raise AgentError(result.stderr)
        return result.stdout.strip()

    def launch(self, *arguments):
        with patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.execution.git", side_effect=self.read_git), \
                patch("ub_agents.refresh.git", side_effect=self.read_git), \
                patch("ub_agents.observations.Publisher", MemoryPublisher), \
                patch("ub_agents.updates.Updates", return_value=None), \
                patch("ub_agents.cli.Loop.launch") as launch, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = main(["--config", str(self.path), "launch", *arguments])
        return code, stdout.getvalue(), stderr.getvalue(), launch

    def doctor(self):
        with patch("ub_agents.doctor.subprocess.run", self.runner), \
                patch("ub_agents.doctor.shutil.which", side_effect=lambda name: f"/tools/{name}"), \
                patch("ub_agents.doctor.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["--config", str(self.path), "doctor", "--json"]), 0)
        self.assertEqual(stderr.getvalue(), "")
        return json.loads(stdout.getvalue())

    def test_each_checkout_condition_refuses_launch_and_only_warns_in_doctor(self):
        cases = [
            (("rev-parse", "--abbrev-ref", "HEAD"), "ub-agents-setup", "is on ub-agents-setup", "switch to main"),
            (("rev-parse", "--abbrev-ref", "HEAD"), "HEAD", "detached HEAD", "switch to main"),
            (("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"),
             "M  ub-agents.yaml", "dirty", "commit or remove"),
            (("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"),
             " M ub-agents.yaml", "dirty", "commit or remove"),
            (("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"),
             "?? role.md", "dirty", "commit or remove"),
            (("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"),
             "1\t0", "local commits not on origin", "reconcile main with origin/main"),
            (("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"),
             "1\t1", "diverged", "reconcile main with origin/main"),
        ]
        for command, response, condition, fix in cases:
            with self.subTest(condition=condition, response=response):
                previous = self.runner.responses[("git", "-C", str(self.root), *command)]
                self.response(command, response)
                result = self.doctor()
                self.assertTrue(result["ok"], result)
                warnings = [check for check in result["checks"] if check["status"] == "warn"]
                self.assertEqual(len(warnings), 1)
                self.assertFalse(warnings[0]["required"])
                message = warnings[0]["message"]
                self.assertIn(condition, message)
                self.assertIn(fix, message)
                for arguments in ((), ("--once",), ("42",)):
                    code, stdout, stderr, launch = self.launch(*arguments)
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, f"ub-agents: {message}\n")
                    launch.assert_not_called()
                self.response(command, previous)
        self.assertEqual(self.github.writes, [])
        self.assertFalse(any("fetch" in command or "merge" in command for command, _ in self.runner.calls))

    def test_doctor_reports_all_checkout_warnings(self):
        self.response(("rev-parse", "--abbrev-ref", "HEAD"), "setup")
        self.response(("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"), "?? role.md")
        self.response(("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"), "1\t0")
        result = self.doctor()
        self.assertTrue(result["ok"])
        self.assertEqual(sum(check["status"] == "warn" for check in result["checks"]), 3)

    def test_checkout_check_uses_the_repository_default_branch(self):
        self.github.metadata["default_branch"] = "trunk"
        self.response(("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/trunk"), "0\t0")
        result = self.doctor()
        warning = next(check for check in result["checks"] if check["status"] == "warn")
        self.assertIn("launch runs only from trunk; switch to trunk", warning["message"])
        with patch.object(self.github, "default_branch", return_value="trunk"):
            code, _, stderr, launch = self.launch("--once")
        self.assertEqual(code, 1)
        self.assertEqual(stderr, f"ub-agents: {warning['message']}\n")
        launch.assert_not_called()

    def test_clean_checkout_on_origin_or_behind_it_can_launch(self):
        for counts in ("0\t0", "0\t2"):
            with self.subTest(counts=counts):
                self.response(("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"), counts)
                self.assertTrue(self.doctor()["ok"])
                code, _, stderr, launch = self.launch("--once")
                self.assertEqual((code, stderr), (0, ""))
                launch.assert_called_once_with(once=True)
        self.assertFalse(any("fetch" in command or "merge" in command for command, _ in self.runner.calls))

    def test_missing_origin_ref_has_a_fix_in_both_commands(self):
        self.response(("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"),
                      subprocess.CompletedProcess([], 1, "", "unknown revision"))
        result = self.doctor()
        self.assertTrue(result["ok"])
        warning = next(check for check in result["checks"] if check["status"] == "warn")
        self.assertIn("run git fetch origin", warning["message"])
        code, _, stderr, launch = self.launch("--once")
        self.assertEqual(code, 1)
        self.assertEqual(stderr, f"ub-agents: {warning['message']}\n")
        launch.assert_not_called()

    def test_every_trigger_and_add_remove_transition_label_is_required_at_startup(self):
        for name in ("ready", "start", "review", "old"):
            with self.subTest(label=name):
                self.github.label_names.remove(name)
                for arguments in ((), ("--once",), ("42",)):
                    code, stdout, stderr, launch = self.launch(*arguments)
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, f"ub-agents: Missing GitHub workflow labels ({name}); "
                                            "run ub-agents doctor for setup commands\n")
                    launch.assert_not_called()
                self.github.label_names.append(name)
        self.assertEqual(self.github.writes, [])

    def test_stop_only_label_is_optional_and_required_label_matching_ignores_case(self):
        self.github.label_names = ["READY", "START", "REVIEW", "OLD"]
        code, _, stderr, launch = self.launch("--once")
        self.assertEqual((code, stderr), (0, ""))
        launch.assert_called_once_with(once=True)
