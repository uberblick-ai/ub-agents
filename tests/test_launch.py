from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import AgentError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from tests.support import FakeGitHub, RecordingRunner, config


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.argv = ["--config", str(self.root / "ub-agent.yaml"), "launch"]
        self.log = self.root / ".ub-agent" / "launch.log"
        self.config = config(self.root)

    def log_lines(self):
        lines = []
        for line in self.log.read_text().splitlines():
            stamp, text = line.split(" ", 1)
            self.assertTrue(stamp.endswith("Z"))
            self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timedelta(0))
            lines.append(text)
        return lines

    def test_stdout_stderr_multiline_output_and_second_launch_append(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def launch(*, once):
            self.assertTrue(once)
            print("Progress — working")
            print("Diagnostic\nsecond line\n", file=sys.stderr)
            # The file is visible before launch returns or explicitly flushes.
            self.assertEqual(self.log_lines()[-4:],
                             ["Progress — working", "Diagnostic", "second line", ""])

        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch.object(Loop, "launch", side_effect=launch), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            for _ in range(2):
                self.assertEqual(main(self.argv + ["--once"]), 0)
        self.assertEqual(stdout.getvalue(), "Progress — working\n" * 2)
        self.assertEqual(stderr.getvalue(), "Diagnostic\nsecond line\n\n" * 2)
        self.assertEqual(self.log_lines(),
                         ["Progress — working", "Diagnostic", "second line", ""] * 2)

    def test_configuration_error_is_logged_before_loop_startup(self):
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", side_effect=AgentError("Invalid configuration")), \
                redirect_stderr(stderr):
            self.assertEqual(main(self.argv), 1)
        self.assertEqual(stderr.getvalue(), "ub-agent: Invalid configuration\n")
        self.assertEqual(self.log_lines(), ["ub-agent: Invalid configuration"])

    def test_continuous_launch_logs_wait_and_final_stop(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def wait(_):
            self.assertEqual(self.log_lines(), ["Waiting for eligible GitHub work"])
            signal.raise_signal(signal.SIGINT)

        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("threading.Event.wait", side_effect=wait), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(self.argv), 130)
        self.assertEqual(stdout.getvalue(), "Waiting for eligible GitHub work\n")
        self.assertEqual(stderr.getvalue(), "Stopped; supervised execution terminated\n")
        self.assertEqual(self.log_lines(), ["Waiting for eligible GitHub work",
                                           "Stopped; supervised execution terminated"])

    def test_sigint_during_github_request_stops_without_retry_or_error(self):
        for once in (False, True):
            for response in (
                    subprocess.CompletedProcess([], -signal.SIGINT, "", ""),
                    subprocess.CompletedProcess([], 130, "", ""),
                    subprocess.CompletedProcess([], 1, "", "connection reset"),
                    subprocess.CompletedProcess([], 0, "[]", ""),
                    subprocess.TimeoutExpired("gh", 20)):
                with self.subTest(once=once, response=response):
                    runner = RecordingRunner(self.root)
                    endpoint = "repos/org/project/issues/comments?per_page=100"
                    command = ("gh", "api", "--hostname", "github.com", "--method", "GET",
                               "-H", "Accept: application/vnd.github+json", "--include", endpoint)
                    runner.responses[command] = response

                    def interrupted_request(*args, **kwargs):
                        signal.raise_signal(signal.SIGINT)
                        return runner(*args, **kwargs)

                    github = GitHub("org/project", interrupted_request)
                    stderr = io.StringIO()
                    original_handler = signal.getsignal(signal.SIGINT)
                    with patch("ub_agents.cli.load_config", return_value=self.config), \
                            patch("ub_agents.cli.GitHub", return_value=github), \
                            patch.object(github, "actor", return_value="operator"), \
                            patch("ub_agents.cli.repository_checks", return_value=[]), \
                            patch.object(Loop, "tick", side_effect=lambda: github.request(endpoint, array=True)), \
                            patch("threading.Event.wait") as wait, redirect_stderr(stderr):
                        self.assertEqual(main(self.argv + (["--once"] if once else [])), 130)
                    self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)
                    wait.assert_not_called()
                    self.assertEqual(len(runner.calls), 1)
                    self.assertEqual(stderr.getvalue(), "Stopped; supervised execution terminated\n")
                    self.assertEqual(self.log_lines()[-1], "Stopped; supervised execution terminated")

    def test_interrupt_during_initial_authentication_is_logged(self):
        github = GitHub("org/project")
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=github), \
                patch("ub_agents.github.subprocess.run", return_value=
                      subprocess.CompletedProcess([], -signal.SIGINT, "", "")), \
                redirect_stderr(stderr):
            self.assertEqual(main(self.argv), 130)
        self.assertEqual(stderr.getvalue(), "Stopped; supervised execution terminated\n")
        self.assertEqual(self.log_lines(), ["Stopped; supervised execution terminated"])

    def test_piped_terminal_output_is_visible_while_launch_is_running(self):
        script = """import sys
from unittest.mock import patch
from ub_agents.cli import main
def launch(args):
    print('Progress before exit')
    print('Diagnostic before exit', file=sys.stderr)
    sys.stdin.readline()
with patch('ub_agents.cli.run', side_effect=launch):
    sys.exit(main(sys.argv[1:]))
"""
        env = os.environ.copy()
        env.pop("PYTHONUNBUFFERED", None)
        process = subprocess.Popen([sys.executable, "-c", script, *self.argv],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        try:
            for stream, expected in ((process.stdout, "Progress before exit\n"),
                                     (process.stderr, "Diagnostic before exit\n")):
                readable, _, _ = select.select([stream], [], [], 5)
                self.assertTrue(readable, "Launch output remained buffered")
                self.assertEqual(stream.readline(), expected)
            self.assertIsNone(process.poll())
            process.communicate("exit\n", timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(self.log_lines(), ["Progress before exit", "Diagnostic before exit"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
