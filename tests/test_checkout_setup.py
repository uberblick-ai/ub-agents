from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from ub_agents.checkout_setup import read_record, run_setup, setup_directory
from ub_agents.cli import main
from ub_agents.config import CheckoutSetup, load_config
from ub_agents.errors import AgentError, CheckoutRefreshError
from ub_agents.execution import git, supervise
from ub_agents.loop import Loop
from ub_agents.records import attempts, timestamp
from tests.support import issue
from tests import test_refresh


class CheckoutSetupTests(unittest.TestCase):
    setUp = test_refresh.RefreshTests.setUp
    commit = test_refresh.RefreshTests.commit
    push_policy = test_refresh.RefreshTests.push_policy
    execute = test_refresh.RefreshTests.execute
    assert_success = test_refresh.RefreshTests.assert_success

    def setup_setting(self, script="print('install output')", timeout=600):
        path = self.upstream / "ub-agents.yaml"
        setting = {"command": [sys.executable, "-c", script],
                   "when-changed": ["pnpm-lock.yaml", "mise.toml"], "timeout-seconds": timeout}
        path.write_text(path.read_text() + "checkout-setup: " + json.dumps(setting) + "\n")

    def new_loop(self, *, observer=None):
        path = self.root / "ub-agents.yaml"
        self.lines = []
        self.loop = Loop(load_config(path), self.github, "operator", output=self.lines.append,
                         config_path=path, observer=observer, interrupt_event=threading.Event())

    def push_setup(self, script="print('install output')", timeout=600):
        self.setup_setting(script, timeout)
        (self.upstream / "pnpm-lock.yaml").write_text("lock version 1\n")
        self.push_policy()
        self.new_loop()

    def record(self):
        return read_record(setup_directory(self.root))

    def test_setting_and_lockfile_added_together_run_before_claim_once(self):
        from unittest.mock import Mock
        observer = Mock()
        self.push_setup()
        self.loop.observer = observer
        before = git(self.root, "rev-parse", "HEAD")

        def installed(cwd, prompt):
            self.assertEqual(self.record(), {"baseline": git(self.root, "rev-parse", "HEAD"),
                                            "succeeded": True, "pending": None})
            self.assertNotEqual(self.record()["baseline"], before)
            self.assertEqual(sum(line.startswith("checkout setup") for line in self.lines), 1)
            self.assertIn("checkout setup running after pnpm-lock.yaml changed", self.lines[0])
            directory = setup_directory(self.root)
            self.assertFalse(directory.is_relative_to(self.root))
            logs = list(directory.glob("attempts/*/process.log"))
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].read_text(), "install output\n")
            self.assertNotIn("install output\n", self.lines)
            self.assertEqual(git(self.root, "status", "--porcelain"), "")

        def install(*args, **kwargs):
            self.assertEqual(self.github.writes, [])
            self.assertEqual(args[1], self.root)
            return supervise(*args, **kwargs)

        with patch("ub_agents.checkout_setup.supervise", side_effect=install):
            self.execute(installed)
        observer.activity.assert_any_call("checkout setup running: pnpm-lock.yaml changed")
        self.execute()
        self.assertEqual(len(list(setup_directory(self.root).glob("attempts/*/process.log"))), 1)

    def test_unchanged_watched_files_skip_even_after_other_files_change(self):
        self.setup_setting()
        self.push_policy()
        git(self.root, "pull", "--ff-only")
        self.new_loop()
        with patch("ub_agents.checkout_setup.supervise") as setup:
            self.execute()
            self.push_policy("Another policy\n")
            self.execute()
        setup.assert_not_called()
        self.assertFalse(any(line.startswith("checkout setup") for line in self.lines))

    def failed_launch(self):
        errors, output = io.StringIO(), io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.launch_checks"), \
                patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise") as role, \
                patch("ub_agents.loop.Workspace.prepare") as prepare, \
                patch.object(self.loop.stop_event, "wait") as wait, \
                redirect_stderr(errors), redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.root / "ub-agents.yaml"),
                                   "launch", "--once", "--no-ui"]), 1)
        role.assert_not_called()
        prepare.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])
        self.assertIn("checkout setup failed after pnpm-lock.yaml changed", errors.getvalue())
        self.assertIn("log:", errors.getvalue())
        self.assertIn(f"Fix the install in {self.root} and launch again.", errors.getvalue())
        return errors.getvalue()

    def test_failure_retries_without_another_fast_forward_until_success(self):
        marker = setup_directory(self.root) / "fixed"
        self.push_setup(f"from pathlib import Path; import sys; print('install failed'); "
                        f"sys.exit(0 if Path({str(marker)!r}).exists() else 1)")
        previous = git(self.root, "rev-parse", "HEAD")
        self.assertIn("exited 1", self.failed_launch())
        refreshed = git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(previous, refreshed)
        self.assertEqual(self.record()["baseline"], previous)
        self.assertEqual(self.record()["pending"], "pnpm-lock.yaml")
        self.new_loop()
        self.assertIn("exited 1", self.failed_launch())
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), refreshed)
        marker.touch()
        self.new_loop()
        self.execute()
        self.assertEqual(self.record(), {"baseline": refreshed, "succeeded": True, "pending": None})
        self.assertEqual(len(list(setup_directory(self.root).glob("attempts/*/process.log"))), 3)
        self.execute()
        self.assertEqual(len(list(setup_directory(self.root).glob("attempts/*/process.log"))), 3)

    def test_start_failure_keeps_setup_pending(self):
        self.push_setup()
        path = self.upstream / "ub-agents.yaml"
        path.write_text(path.read_text().replace(sys.executable, "/missing/install-command"))
        self.push_policy("Start failure\n")
        self.assertIn("Cannot start configured execution", self.failed_launch())
        self.assertEqual(self.record()["pending"], "pnpm-lock.yaml")

    def test_timeout_stops_command_and_keeps_pending(self):
        self.push_setup("import time; time.sleep(30)", 0.1)
        self.assertIn("timed out after 0.1 seconds", self.failed_launch())
        attempt = next((setup_directory(self.root) / "attempts").iterdir())
        self.assertEqual((attempt / "stopped").read_text(), "confirmed\n")
        self.assertEqual(self.record()["pending"], "pnpm-lock.yaml")

    def test_interrupt_stops_command_without_claiming(self):
        self.push_setup("import time; time.sleep(30)")
        self.loop.interrupt_event = threading.Event()
        with patch("ub_agents.checkout_setup.supervise", side_effect=KeyboardInterrupt):
            self.assertIn("interrupted", self.failed_launch())
        self.assertEqual(self.record()["pending"], "pnpm-lock.yaml")

    def test_successful_baseline_is_retained_across_unrelated_refreshes(self):
        self.push_setup()
        self.execute()
        installed = self.record()["baseline"]
        self.push_policy("Unrelated change\n")
        self.execute()
        self.assertEqual(self.record()["baseline"], installed)
        (self.upstream / "mise.toml").write_text("[tools]\n")
        self.push_policy("Tool change\n")
        self.github.items[5] = issue(5)
        self.new_loop()
        self.execute()
        self.assertIn("after mise.toml changed", self.lines[0])
        self.assertNotEqual(self.record()["baseline"], installed)

    def test_failed_install_remains_pending_even_if_lockfile_is_reverted(self):
        self.push_setup("import sys; sys.exit(1)")
        self.failed_launch()
        (self.upstream / "pnpm-lock.yaml").unlink()
        self.push_policy("Revert lockfile\n")
        self.new_loop()
        self.assertIn("exited 1", self.failed_launch())

    def test_literal_paths_do_not_match_git_pathspec_patterns(self):
        self.push_setup()
        path = self.upstream / "ub-agents.yaml"
        path.write_text(path.read_text().replace('"pnpm-lock.yaml", "mise.toml"', '"*.yaml"'))
        self.push_policy("Literal path\n")
        with patch("ub_agents.checkout_setup.supervise") as setup:
            self.execute()
        setup.assert_not_called()


class CheckoutSetupConfigTests(unittest.TestCase):
    def test_strict_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ub-agents.yaml"
            base = "repository: org/project\nagents:\n  worker:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
            path.write_text(base)
            self.assertIsNone(load_config(path).checkout_setup)
            path.write_text(base + "checkout-setup: {command: [mise, run, install], when-changed: [pnpm-lock.yaml, mise.toml]}\n")
            self.assertEqual(load_config(path).checkout_setup,
                             CheckoutSetup(("mise", "run", "install"), ("pnpm-lock.yaml", "mise.toml")))
            invalid = ["null", "[]", "{}", "{command: [echo]}", "{when-changed: [lock]}",
                       "{command: echo, when-changed: [lock]}",
                       "{command: [], when-changed: [lock]}",
                       "{command: [echo], when-changed: lock}",
                       "{command: [echo], when-changed: []}",
                       "{command: [echo], when-changed: [true]}",
                       "{command: [echo], when-changed: ['']}",
                       "{command: [echo], when-changed: [/lock]}",
                       "{command: [echo], when-changed: [../lock]}",
                       "{command: [echo], when-changed: [a/../lock]}",
                       "{command: [echo], when-changed: ['.']}",
                       "{command: [echo], when-changed: [lock], surprise: true}"]
            for timeout in ("0", "-1", "3601", "true", "null", "inf", ".inf", ".nan"):
                invalid.append("{command: [echo], when-changed: [lock], timeout-seconds: " + timeout + "}")
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(base + f"checkout-setup: {value}\n")
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                        self.assertEqual(main(["--config", str(path), "check"]), 1)
                    self.assertIn("checkout-setup", error.getvalue())
