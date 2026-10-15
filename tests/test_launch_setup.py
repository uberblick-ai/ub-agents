"""Setup must remain visible and cancellable before any launcher work starts."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.errors import AgentError, GitHubError
from ub_agents.loop import Loop
from tests.support import FakeGitHub, MemoryPublisher, config, isolate_runtime_state


class LaunchSetupTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        path = self.root / "ub-agents.yaml"
        path.touch()
        self.argv = ["--config", str(path), "launch"]
        self.github = FakeGitHub()
        self.publisher = MemoryPublisher()
        self.view = Mock()
        self.updates = Mock(banner=None)
        self.enterContext(patch("ub_agents.cli.load_config", return_value=config(self.root)))
        self.enterContext(patch("ub_agents.cli.GitHub", return_value=self.github))
        self.enterContext(patch("ub_agents.cli.repository_checks", return_value=[]))
        self.enterContext(patch("ub_agents.observations.Publisher", return_value=self.publisher))
        self.checks = self.enterContext(patch("ub_agents.cli.launch_checks"))
        self.open_view = self.enterContext(patch("ub_agents.launch_ui.open_view", return_value=self.view))
        self.update_factory = self.enterContext(patch("ub_agents.updates.Updates", return_value=self.updates))
        self.sweep = self.enterContext(patch("ub_agents.cli.startup_cleanup", return_value=False))

        def create(*args, **kwargs):
            self.loop = Loop(*args, **kwargs)
            return self.loop

        def poll(*_):
            self.loop.stop_event.set()
            return True

        self.enterContext(patch("ub_agents.cli.Loop", side_effect=create))
        self.poll = self.enterContext(patch.object(Loop, "tick", side_effect=poll))
        self.item_poll = self.enterContext(patch.object(Loop, "tick_item", side_effect=poll))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))

    def assert_no_work(self):
        self.update_factory.assert_not_called()
        self.sweep.assert_not_called()
        self.poll.assert_not_called()
        self.item_poll.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_view_checks_sweep_poll_order_in_every_launch_form(self):
        for arguments in ([], ["--once"], ["--agent", "worker"], ["42"],
                          ["42", "--agent", "worker"], ["--once", "--agent", "worker"]):
            for no_ui in (False, True):
                with self.subTest(arguments=arguments, no_ui=no_ui):
                    events = []

                    def view(root, session, output, stop, **kwargs):
                        self.assertEqual(kwargs["no_ui"], no_ui)
                        self.assertEqual(session, self.publisher.snapshots[-1]["session"])
                        self.assertEqual(self.publisher.snapshots[-1]["activity"]["state"], "checking setup")
                        events.append("view")
                        return None if no_ui else self.view

                    def checks(*_):
                        self.assertEqual(events, ["view"])
                        self.update_factory.assert_not_called()
                        events.append("checks")

                    def sweep(*args, **kwargs):
                        self.assertEqual(events, ["view", "checks"])
                        self.updates.start.assert_called_once()
                        events.append("sweep")
                        return False

                    def poll(*_):
                        self.assertEqual(events, ["view", "checks", "sweep"])
                        events.append("poll")
                        self.loop.stop_event.set()
                        return True

                    self.open_view.side_effect = view
                    self.checks.side_effect = checks
                    self.sweep.side_effect = sweep
                    self.poll.side_effect = self.item_poll.side_effect = poll
                    self.update_factory.reset_mock()
                    self.updates.reset_mock()
                    self.assertEqual(main(self.argv + arguments + (["--no-ui"] if no_ui else [])), 0)
                    self.assertEqual(events, ["view", "checks", "sweep", "poll"])
                    self.assertEqual(self.github.writes, [])
        self.assertNotIn("checking setup", self.stdout.getvalue())

    def test_fatal_setup_errors_close_view_before_printing_unchanged_message(self):
        errors = (AgentError("control checkout is on feature; launch runs only from main; switch to main"),
                  AgentError("control checkout is dirty (staged, modified or untracked files); "
                             "commit or remove those changes before launch"),
                  AgentError("Missing GitHub workflow labels (ready); run ub-agents doctor for setup commands"),
                  GitHubError("GET", "repos/org/project", "gh auth login required"),
                  GitHubError("GET", "repos/org/project", "Forbidden (HTTP 403)"))
        for error in errors:
            with self.subTest(error=error):
                self.stderr.seek(0)
                self.stderr.truncate()
                self.view.close.reset_mock()

                def closed():
                    self.assertEqual(self.stderr.getvalue(), "")

                self.view.close.side_effect = closed
                self.checks.side_effect = error
                with patch.object(Loop, "_wait") as wait:
                    self.assertEqual(main(self.argv), 1)
                wait.assert_not_called()
                self.view.close.assert_called_once()
                suffix = "; Fix the cause and restart ub-agents launch." if isinstance(error, GitHubError) else ""
                self.assertEqual(self.stderr.getvalue(), f"ub-agents: {error}{suffix}\n")
                self.assertTrue(self.publisher.snapshots[-1]["ended"])
                self.assert_no_work()

    def test_retryable_setup_failures_keep_retrying_with_capped_poll_backoff(self):
        failure = GitHubError("GET", "repos/org/project", "Connection reset\nsecond line", retryable=True)
        for arguments in ([], ["--once"], ["--agent", "worker"], ["42"], ["42", "--agent", "worker"]):
            with self.subTest(arguments=arguments):
                self.checks.reset_mock()
                self.checks.side_effect = [failure] * 8 + [None]
                self.update_factory.reset_mock()
                self.sweep.reset_mock()
                self.poll.reset_mock()
                self.item_poll.reset_mock()
                self.open_view.return_value = None
                self.stdout.seek(0)
                self.stdout.truncate()
                activities = []

                def wait(event, delay):
                    self.assert_no_work()
                    activity = self.publisher.snapshots[-1]["activity"]
                    self.assertEqual(activity["state"], "waiting")
                    self.assertEqual(activity["reason"], "setup retry")
                    self.assertIsNotNone(activity["until"])
                    activities.append(delay)

                with patch("threading.Event.wait", side_effect=wait, autospec=True):
                    self.assertEqual(main(self.argv + arguments + ["--no-ui"]), 0)
                self.assertEqual(activities, [5, 10, 20, 40, 60, 60, 60, 60])
                self.assertEqual(self.checks.call_count, 9)
                lines = [line for line in self.stdout.getvalue().splitlines()
                         if not line.startswith("Serving queue for agent ")]
                self.assertEqual(lines, [f"Skipped GitHub poll: {' '.join(str(failure).split())}; retrying in {delay}s"
                                         for delay in activities])
                self.sweep.assert_called_once()

    def test_signals_interrupt_slow_setup_subprocess_without_work(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                pid = self.root / "setup-pid"
                handler = signal.getsignal(sig)

                def checks(*_):
                    script = ("import os,signal,time; "
                              f"open({str(pid)!r}, 'w').write(str(os.getpid())); "
                              f"os.kill(os.getppid(), {int(sig)}); time.sleep(30)")
                    subprocess.run([sys.executable, "-c", script], check=True, timeout=35)
                    self.fail("Setup continued after stop signal")

                self.checks.side_effect = checks
                started = time.monotonic()
                self.assertEqual(main(self.argv), 0 if sig == signal.SIGTERM else 130)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(signal.getsignal(sig), handler)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid.read_text()), 0)
                self.assert_no_work()
                self.assertTrue(self.publisher.snapshots[-1]["ended"])

    def test_signals_interrupt_setup_retry_wait_without_work(self):
        self.checks.side_effect = GitHubError("GET", "repos/org/project", "Timeout", retryable=True)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.checks.reset_mock()
                with patch("threading.Event.wait", side_effect=lambda *_: signal.raise_signal(sig)):
                    self.assertEqual(main(self.argv), 0 if sig == signal.SIGTERM else 130)
                self.assertEqual(self.checks.call_count, 1)
                self.assert_no_work()

    def test_quit_while_view_opens_prevents_setup_and_work(self):
        self.open_view.side_effect = lambda *args, **kwargs: signal.raise_signal(signal.SIGTERM)
        self.assertEqual(main(self.argv), 0)
        self.checks.assert_not_called()
        self.assert_no_work()
