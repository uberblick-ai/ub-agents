from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from dataclasses import replace
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.config import Priority, Queue, Runtime
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError, GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.records import iso, seconds, timestamp
from tests.support import (AccountGitHub, FakeGitHub, PollGitHub, RecordingRunner, agent, config,
                           isolate_runtime_state, issue, pr)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "ub-agents.yaml").touch()
        self.enterContext(patch("ub_agents.cli.launch_checks"))
        self.argv = ["--config", str(self.root / "ub-agents.yaml"), "launch"]
        self.log = self.root / ".ub-agents" / "launch.log"
        self.config = config(self.root)

    def log_lines(self):
        lines = []
        for line in self.log.read_text().splitlines():
            stamp, text = line.split(" ", 1)
            self.assertTrue(stamp.endswith("Z"))
            self.assertEqual(datetime.fromisoformat(stamp).utcoffset(), timedelta(0))
            lines.append(text)
        return lines

    def test_once_always_observes_and_status_does_not_create_a_publisher(self):
        from tests.support import MemoryPublisher
        publisher = MemoryPublisher()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.observations.Publisher", return_value=publisher) as factory, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.argv + ["--once"]), 0)
            factory.assert_called_once()
            self.assertEqual(publisher.snapshots[-1]["latest_pass"]["state"], "complete")
            self.assertTrue(publisher.snapshots[-1]["ended"])
            factory.reset_mock()
            self.assertEqual(main(self.argv[:-1] + ["status"]), 0)
            factory.assert_not_called()

    def test_once_explains_empty_or_unlabeled_open_work_before_exiting(self):
        for items in ((), (issue(labels=()),), (pr(labels=()),),
                      (issue(labels=("unrelated",)), pr(2, labels=())),
                      (replace(issue(), state="closed"), replace(pr(2), state="closed"))):
            with self.subTest(items=items):
                github = FakeGitHub(*items)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(Loop, "_wait") as wait, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(self.argv + ["--once"]), 0)
                self.assertRegex(stdout.getvalue().splitlines()[0], r'^Discovery pass empty: .*candidates reached=0$')
                self.assertEqual("\n".join(stdout.getvalue().splitlines()[1:]) + "\n",
                                 "No open issue or PR has a trigger label (ready, needs-changes); add one to start\n")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(self.log_lines()[-2:], stdout.getvalue().splitlines())
                wait.assert_not_called()
                self.assertEqual(github.writes, [])

    def test_idle_message_names_all_triggers_once_in_configuration_order(self):
        self.config = config(self.root,
                             agent(self.root, name="preparer", triggers=("needs-preparation",), kind="issue"),
                             agent(self.root, name="worker", triggers=("ready", "needs-changes")),
                             agent(self.root, name="reviewer", triggers=("needs-review", "needs-changes"), kind="pr"),
                             agent(self.root, name="integrator", triggers=("ready-to-merge",), kind="pr"))
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(self.argv + ["--once"]), 0)
        self.assertEqual("\n".join(stdout.getvalue().splitlines()[1:]) + "\n", "No open issue or PR has a trigger label "
                         "(needs-preparation, ready, needs-changes, needs-review, ready-to-merge); add one to start\n")

    def test_triggered_but_parked_work_does_not_suggest_adding_a_trigger(self):
        for item in (issue(labels=("ready", "needs-human")), pr(labels=("ready", "needs-human"))):
            with self.subTest(kind=item.kind):
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=FakeGitHub(item)), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(main(self.argv + ["--once"]), 0)
                self.assertIn("parked", stdout.getvalue())
                self.assertNotIn("add one to start", stdout.getvalue())

    def test_continuous_idle_message_updates_when_trigger_presence_changes(self):
        github = FakeGitHub(issue(labels=("ready", "needs-human")))
        lines = []
        loop = Loop(self.config, github, "operator", output=lines.append)
        loop.interrupt_event = threading.Event()
        waits = []
        def wait(delay):
            waits.append(delay)
            if len(waits) == 1:
                github.change(1, labels=frozenset({"needs-human"}))
            elif len(waits) == 2:
                github.change(1, labels=frozenset({"needs-human", "ready"}))
            else:
                loop.stop_event.set()
        with patch("ub_agents.loop.monotonic", return_value=0), \
                patch.object(loop.stop_event, "wait", side_effect=wait):
            loop.launch()
        self.assertEqual([line for line in lines if "next poll" in line], [
            "No eligible work; next poll in 1s (0 requests last poll)",
            "No open issue or PR has a trigger label (ready, needs-changes); add one to start; "
            "next poll in 1s (0 requests last poll)",
            "No eligible work; next poll in 1s (0 requests last poll)"])

    def test_signals_during_publisher_startup_keep_launcher_interrupt_semantics(self):
        for sig, result in ((signal.SIGTERM, 0), (signal.SIGINT, 130), (signal.SIGHUP, 130)):
            with self.subTest(signal=sig):
                github = FakeGitHub()
                def start(*args, **kwargs):
                    signal.raise_signal(sig)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=github), \
                        patch.object(github, "actor") as actor, \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch("ub_agents.observations.Publisher", side_effect=start), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(self.argv + ["--once"]), result)
                actor.assert_not_called()
                self.assertNotIn("Cannot publish", stdout.getvalue() + stderr.getvalue())

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
        self.assertEqual(stderr.getvalue(), "ub-agents: Invalid configuration\n")
        self.assertEqual(self.log_lines(), ["ub-agents: Invalid configuration"])

    def test_continuous_launch_logs_wait_and_final_stop(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def wait(_):
            self.assertRegex(self.log_lines()[0], r'^Discovery pass empty: .*candidates reached=0$')
            self.assertEqual(self.log_lines()[1:], ["No open issue or PR has a trigger label (ready, needs-changes); "
                                               "add one to start; next poll in 1s (0 requests last poll)"])
            signal.raise_signal(signal.SIGINT)

        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("threading.Event.wait", side_effect=wait), \
                patch("ub_agents.loop.monotonic", return_value=0), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(self.argv), 130)
        self.assertEqual("\n".join(stdout.getvalue().splitlines()[1:]) + "\n", "No open issue or PR has a trigger label (ready, needs-changes); "
                                           "add one to start; next poll in 1s (0 requests last poll)\n")
        self.assertEqual(stderr.getvalue(), "Stopped; supervised execution terminated\n")
        self.assertEqual(self.log_lines()[1:], ["No open issue or PR has a trigger label (ready, needs-changes); "
                                           "add one to start; next poll in 1s (0 requests last poll)",
                                           "Stopped; supervised execution terminated"])

    def test_runtime_pause_start_and_changed_end_are_logged(self):
        stdout = io.StringIO()
        now = seconds("2026-10-04T12:00:00Z")

        def tick(loop):
            loop.coordinator.clock = lambda: now
            loop.usage.limit("claude", now + 300)
            loop.usage.limit("claude", now + 300)
            loop.usage.limit("claude", now + 100)
            return True

        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch.object(Loop, "tick", autospec=True, side_effect=tick), \
                redirect_stdout(stdout):
            self.assertEqual(main(self.argv + ["--once"]), 0)
        lines = [f"claude usage limit reached; pausing claude runs until {iso(now + end)}"
                 for end in (360, 160)]
        self.assertEqual(stdout.getvalue().splitlines(), lines)
        self.assertEqual(self.log_lines(), lines)

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

    def test_sigterm_after_claim_drains_and_logs_output_with_exit_zero(self):
        for once in (False, True):
            with self.subTest(once=once):
                stdout, stderr = io.StringIO(), io.StringIO()

                def tick(loop):
                    loop._end_poll()
                    signal.raise_signal(signal.SIGTERM)
                    self.assertTrue(loop.stop_event.is_set())
                    self.assertFalse(loop.interrupt_event.is_set())
                    loop.output("Finished current assignment")
                    return True

                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(Loop, "tick", autospec=True, side_effect=tick) as ticks, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(self.argv + (["--once"] if once else [])), 0)
                self.assertEqual(ticks.call_count, 1)
                self.assertEqual(stdout.getvalue(), "Finished current assignment\n")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(self.log_lines()[-1], "Finished current assignment")

    def test_interrupt_during_initial_authentication_is_logged(self):
        github = GitHub("org/project")
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=github), \
                patch("ub_agents.github.subprocess.run", return_value=
                      subprocess.CompletedProcess([], -signal.SIGINT, "", "")), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                redirect_stderr(stderr):
            self.assertEqual(main(self.argv), 130)
        self.assertEqual(stderr.getvalue(), "Stopped; supervised execution terminated\n")
        self.assertRegex(self.log_lines()[0], r'^Discovery pass empty: .*gh calls=1, REST quota=0, '
                         r'HTTP 304=0, GraphQL calls=0; candidates reached=0$')
        self.assertEqual(self.log_lines()[1:], ["Stopped; supervised execution terminated"])

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


class TargetedLaunchTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "ub-agents.yaml").touch()
        self.enterContext(patch("ub_agents.cli.launch_checks"))
        self.argv = ["--config", str(self.root / "ub-agents.yaml"), "launch"]
        self.config = config(self.root)
        self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
        self.now = timestamp()
        self.loop = None

    def coordinator(self, github=None):
        github = self.github if github is None else github
        return Coordinator(github, github.actor(), clock=lambda: self.now, queue=self.config.queue,
                           output=lambda *_: None, launchers=self.config.launchers)

    def report_success(self, command, cwd, env, *args, **kwargs):
        co = self.loop.coordinator
        number = int(env["UB_AGENTS_ASSIGNMENT"])
        lease = next(r for r in reversed(co.history(number)) if r["kind"] == "lease")
        co.report(lease, "success", "Completed", outcome="done")
        return 0

    def launch(self, *args, execute=None, refresh=None, hidden=False):
        def create(*args, **kwargs):
            self.loop = Loop(*args, **kwargs)
            self.loop.coordinator.clock = lambda: self.now
            self.loop.stop_event.wait = Mock()
            return self.loop

        def open_view(root, session, output, stop, *, no_ui=False, poll=None):
            if not hidden or no_ui:
                return None
            output.hide()
            view = Mock()
            view.close.side_effect = lambda: output.resume(final=True)
            return view

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("ub_agents.cli.load_config", side_effect=lambda *_: self.config), \
                patch("ub_agents.loop.load_config", side_effect=lambda *_: self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop", side_effect=create), \
                patch("ub_agents.launch_ui.open_view", side_effect=open_view), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.loop.refresh_checkout", side_effect=refresh), \
                patch("ub_agents.loop.supervise", side_effect=execute or self.report_success) as run, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(self.argv + list(args))
        self.loop.stop_event.wait.assert_not_called()
        log = self.root / ".ub-agents" / "launch.log"
        self.assertTrue(log.exists())
        logged = "\n".join(line.split(" ", 1)[1] for line in log.read_text().splitlines())
        for line in (stdout.getvalue() + stderr.getvalue()).splitlines():
            self.assertIn(line, logged)
        return code, stdout.getvalue(), stderr.getvalue(), run

    def assert_scoped(self, number=11):
        forbidden = {"observe", "repository_comments", "dependency_graph", "milestone_order"}
        self.assertFalse(forbidden.intersection(name for name, _ in self.github.reads), self.github.reads)
        item_reads = {"item", "comments", "timeline", "issue_content", "pr_content", "reviews",
                      "review_comments", "blocked_by"}
        self.assertTrue(all(args[0] == number for name, args in self.github.reads if name in item_reads),
                        self.github.reads)
        self.assertNotIn(1, self.github.store)
        self.assertEqual(self.github.items[1].labels, frozenset({"ready", "urgent"}))

    def assert_output(self, stdout, expected):
        passes = [line for line in stdout.splitlines() if line.startswith("Discovery pass ")]
        self.assertEqual(len(passes), 1)
        self.assertRegex(passes[0], r'^Discovery pass empty: .*candidates reached=1$')
        plans = [line for line in stdout.splitlines() if not line.startswith("Discovery pass ")]
        self.assertEqual("\n".join(plans) + "\n", expected)
        self.assertTrue(stdout.endswith(expected), stdout)

    def test_eligible_item_runs_once_without_discovering_or_ranking_other_work(self):
        self.config = replace(config(self.root, queue=Queue(milestones="order", priority=Priority(("urgent",)))),
                              launchers=("OPERATOR", "peer"))
        self.github.dependencies[1] = [11]  # Would also cause priority inheritance in a queue pass.
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertIn("#11 worker: claimed", stdout)
        lease, outcome = self.coordinator().history(11)
        self.assertEqual((lease["state"], lease["result"], outcome["accepted"]),
                         ("released", "success", True))
        snapshot = self.loop.observer.publisher.snapshots[-1]
        self.assertEqual(snapshot["latest_pass"]["rows"], [])
        self.assertEqual(snapshot["latest_pass"]["state"], "partial")
        self.assertEqual(snapshot["outcomes"][0]["acceptance"], "finalized")
        self.assertTrue(snapshot["ended"])
        self.assert_scoped()

    def test_explicit_once_is_accepted_with_number(self):
        code, _, _, run = self.launch("11", "--once")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assert_scoped()

    def test_live_lease_refusal_includes_status_process_reason_and_owner(self):
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.config.agents[0], ()))
        co.update(lease, state="running", started=True, host="remote-host")
        writes = self.github.writes[:]
        self.github.reads.clear()
        with patch("ub_agents.loop.socket.gethostname", return_value="local-host"):
            code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assertIn("#11 worker: owned — claimed by @operator on remote-host", stdout)
        self.assertIn("Process can't be checked from here; lease is on another host.", stdout)
        run.assert_not_called()
        self.assertEqual(self.github.writes, writes)
        self.assert_scoped()

    def test_configured_launchers_honor_peer_ownership_with_status_process_reason(self):
        self.config = replace(self.config, launchers=("OPERATOR", "PEER"))
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        peer.update(lease, state="running", started=True, host="remote-host")
        writes = self.github.writes[:]
        self.github.reads.clear()
        with patch("ub_agents.loop.socket.gethostname", return_value="local-host"):
            code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assertIn("#11 worker: owned — claimed by @peer on remote-host", stdout)
        self.assertIn("Process can't be checked from here; lease is on another host.", stdout)
        run.assert_not_called()
        self.assertEqual(self.github.writes, writes)
        self.assert_scoped()

    def test_configured_launchers_ignore_unlisted_peer_ownership(self):
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        peer.update(lease, state="running", started=True)
        self.config = replace(self.config, launchers=("OPERATOR",))
        self.github.reads.clear()
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        self.assertIn("#11 worker: claimed", stdout)
        run.assert_called_once()
        self.assertEqual(self.coordinator().history(11)[0]["actor"], "operator")
        self.assert_scoped()

    def test_unreadable_peer_role_refuses_without_claiming(self):
        self.config = replace(self.config, launchers=("operator", "peer"))
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        self.github.roles["peer"] = None
        writes = self.github.writes[:]
        self.github.reads.clear()
        code, _, stderr, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assertIn("Launcher account @peer's repository role could not be read", stderr)
        run.assert_not_called()
        self.assertEqual(self.github.writes, writes)
        self.assert_scoped()

    def test_untrusted_launcher_refuses_without_claim_or_approval_parking(self):
        for launchers, role, reason in (
                (("peer",), "write", "Launcher account @operator is not listed in launchers"),
                (("operator", "peer"), "read",
                 "Launcher account @operator has repository role read; write or higher is required")):
            with self.subTest(launchers=launchers, role=role):
                self.config = replace(self.config, launchers=launchers)
                self.github.roles["operator"] = role
                self.github.timelines[11] = []  # Approval would normally park the item.
                code, stdout, _, run = self.launch("11")
                self.assertEqual(code, 1)
                self.assert_output(stdout, f"#11 worker: blocked — {reason}\n")
                self.assertEqual(self.github.writes, [])
                run.assert_not_called()
                self.assert_scoped()

    def test_stop_label_refuses_with_status_reason_without_writes(self):
        self.github.change(11, labels=frozenset({"ready", "needs-human"}))
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: parked — Stop label needs-human is present; "
                           "parked until a person acts on its Action needed notice, "
                           "removes the stop label and restores a trigger\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_open_dependency_refuses_and_reads_only_targets_blockers(self):
        self.github.dependencies[11] = [1]
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: parked — Waiting for blockers #1\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_no_trigger_refuses_with_labels_to_add(self):
        self.github.change(11, labels=frozenset())
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11: No trigger matches; add a trigger label (worker: ready, needs-changes)\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_missing_and_closed_items_are_named(self):
        self.github.read_results["item"] = [GitHubError("GET", "repos/org/project/issues/11", "HTTP 404")]
        # A failed read is an ordinary one-off launch error, with the item named.
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(self.argv + ["11"]), 1)
        self.assertIn("Cannot read #11", stderr.getvalue())
        self.assertIn("HTTP 404", stderr.getvalue())
        self.assertEqual(self.github.writes, [])
        self.github.change(11, state="closed")
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11: issue is closed\n")
        run.assert_not_called()
        self.assert_scoped()

    def test_named_agent_and_default_configuration_order(self):
        outcomes = {"done": {"add": ("needs-review",), "remove": ()}}
        first = agent(self.root, name="first", outcomes=outcomes)
        second = agent(self.root, name="second", outcomes=outcomes)
        reviewer = agent(self.root, name="reviewer", triggers=("needs-review",))
        self.config = config(self.root, first, second, reviewer)
        for args, expected in (((), "first"), (("--agent", "second"), "second")):
            with self.subTest(args=args):
                self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
                code, _, _, run = self.launch("11", *args)
                self.assertEqual(code, 0)
                run.assert_called_once()
                self.assertEqual(self.coordinator().history(11)[0]["agent"], expected)
                snapshot = self.loop.observer.publisher.snapshots[-1]
                self.assertEqual([row["agent"] for row in snapshot["latest_pass"]["rows"]], ["reviewer"])
                self.assertTrue(snapshot["ended"])
                self.assert_scoped()

    def test_default_skips_ineligible_agent_and_named_agent_evaluates_only_it(self):
        first = agent(self.root, name="first", command=("missing-command-for-test",))
        second = agent(self.root, name="second")
        self.config = config(self.root, first, second)
        code, stdout, _, run = self.launch("11", "--agent", "first")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 first: blocked — Command is not installed: missing-command-for-test\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(self.coordinator().history(11)[0]["agent"], "second")
        self.assert_scoped()

    def test_named_agent_kind_and_trigger_mismatches_refuse(self):
        self.config = config(self.root, agent(self.root, name="reviewer", kind="pr"),
                             agent(self.root, name="other", triggers=("prepare",)))
        for name, reason in (("reviewer", "No evaluated agent applies to this issue"),
                             ("other", "No trigger matches; add a trigger label (other: prepare)")):
            with self.subTest(name=name):
                code, stdout, _, run = self.launch("11", "--agent", name)
                self.assertEqual(code, 1)
                self.assert_output(stdout, f"#11: {reason}\n")
                run.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assert_scoped()

    def test_each_ineligible_agent_gets_its_status_reason(self):
        self.config = config(self.root, agent(self.root, name="first"), agent(self.root, name="second"))
        self.github.change(11, labels=frozenset({"ready", "needs-human"}))
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        reason = ("parked — Stop label needs-human is present; "
                  "parked until a person acts on its Action needed notice, "
                  "removes the stop label and restores a trigger\n")
        self.assert_output(stdout, f"#11 first: {reason}#11 second: {reason}")
        run.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_owned_noop_leaves_the_owner_visible_with_and_without_the_view(self):
        self.config = replace(self.config, launchers=("operator", "peer"))
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        peer.update(lease, state="running", started=True, host="remote-host", log_dir="private-logs")
        writes = self.github.writes[:]
        reasons = []
        for args, hidden in ((("--no-ui",), False), ((), False), ((), True)):
            with self.subTest(args=args, hidden=hidden):
                code, stdout, stderr, run = self.launch("11", *args, hidden=hidden)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                line = stdout.splitlines()[-1]
                self.assertIn("#11 worker: owned — claimed by @peer on remote-host", line)
                self.assertIn("lease ends", line)
                self.assertNotIn("private-logs", line)
                self.assertEqual(sum("#11 worker:" in row for row in stdout.splitlines()), 1)
                if hidden:
                    self.assertEqual(stdout, line + "\n")
                reasons.append(line)
                run.assert_not_called()
                self.assertEqual(self.github.writes, writes)
                self.assert_scoped()
        self.assertEqual(len(set(reasons)), 1)

    def test_local_owned_noop_omits_the_running_process_log_path(self):
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.config.agents[0], ()))
        co.update(lease, state="running", started=True, host="local-host", process_group=42,
                  log_dir="private-logs")
        with patch("ub_agents.loop.socket.gethostname", return_value="local-host"), \
                patch("ub_agents.status.group_members", return_value=[42]):
            code, stdout, _, run = self.launch("11", hidden=True)
        self.assertEqual(code, 1)
        self.assertIn("#11 worker: running — claimed by @operator on local-host", stdout)
        self.assertIn("lease ends", stdout)
        self.assertIn("Agent running.", stdout)
        self.assertNotIn("private-logs", stdout)
        self.assertNotIn("log:", stdout)
        run.assert_not_called()

    def test_stop_without_trigger_keeps_each_reason_and_named_agent_selection(self):
        self.config = config(self.root, agent(self.root, name="first"), agent(self.root, name="second"))
        self.github.change(11, labels=frozenset({"needs-human"}))
        reason = ("parked — Stop label needs-human is present; "
                  "parked until a person acts on its Action needed notice, "
                  "removes the stop label and restores a trigger\n")
        for hidden in (False, True):
            for args, names in (((), ("first", "second")), (("--agent", "second"), ("second",))):
                with self.subTest(hidden=hidden, args=args):
                    code, stdout, stderr, run = self.launch("11", *args, hidden=hidden)
                    self.assertEqual(code, 1)
                    self.assertEqual(stderr, "")
                    expected = "".join(f"#11 {name}: {reason}" for name in names)
                    if hidden:
                        self.assertEqual(stdout, expected)
                    else:
                        self.assert_output(stdout, expected)
                    self.assertNotIn("No trigger matches", stdout)
                    run.assert_not_called()
                    self.assertEqual(self.github.writes, [])
                    self.assert_scoped()

    def test_claim_race_reports_the_fresh_owner_after_a_ready_plan(self):
        original_claim = Coordinator.claim
        self.config = replace(self.config, launchers=("operator", "peer"))
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
                self.github.roles["peer"] = "write"
                winner = []

                def claim(co, plan, *args, **kwargs):
                    self.assertEqual(plan.state, "ready")
                    peer = self.coordinator(AccountGitHub(self.github, "peer"))
                    lease = original_claim(peer, peer.plan(self.github.items[11], self.config.agents[0], ()))
                    peer.update(lease, state="running", started=True, host="race-host")
                    winner.append(lease.copy())
                    return original_claim(co, plan, *args, **kwargs)

                with patch.object(Coordinator, "claim", new=claim):
                    code, stdout, _, run = self.launch("11", hidden=hidden)
                self.assertEqual(code, 1)
                reason = stdout.splitlines()[-1]
                self.assertIn("#11 worker: owned — claimed by @peer on race-host", reason)
                self.assertIn("lease ends", reason)
                self.assertNotIn("ready", reason)
                self.assertEqual(sum("#11 worker:" in row for row in stdout.splitlines()), 1)
                if hidden:
                    self.assertEqual(stdout, reason + "\n")
                run.assert_not_called()
                self.assertEqual(self.coordinator().history(11), winner)
                self.assert_scoped()

    def test_lost_election_replays_the_winner_and_withdraws_only_our_claim(self):
        self.config = replace(self.config, launchers=("operator", "peer"))
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
                self.github.roles["peer"] = "write"
                create_comment = self.github.create_comment
                winner = []

                def create(number, text, *, login=None):
                    if login is None:
                        peer = self.coordinator(AccountGitHub(self.github, "peer"))
                        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
                        peer.update(lease, state="running", started=True, host="election-host")
                        winner.append(lease.copy())
                    return create_comment(number, text, login=login)

                with patch.object(self.github, "create_comment", side_effect=create):
                    code, stdout, _, run = self.launch("11", hidden=hidden)
                self.assertEqual(code, 1)
                reason = stdout.splitlines()[-1]
                self.assertIn("#11 worker: owned — claimed by @peer on election-host", reason)
                self.assertIn("lease ends", reason)
                self.assertEqual(sum("#11 worker:" in row for row in stdout.splitlines()), 1)
                if hidden:
                    self.assertEqual(stdout, reason + "\n")
                run.assert_not_called()
                owner, withdrawn = self.coordinator().history(11)
                self.assertEqual(owner, winner[0])
                self.assertEqual((withdrawn["actor"], withdrawn["state"], withdrawn["started"]),
                                 ("operator", "withdrawn", False))
                self.assert_scoped()

    def test_refresh_with_no_plan_names_the_final_missing_trigger(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))

                def refresh(*_, on_fetch=None):
                    self.github.change(11, labels=frozenset())

                code, stdout, _, run = self.launch("11", refresh=refresh, hidden=hidden)
                self.assertEqual(code, 1)
                expected = "#11 worker: declined — No trigger matches; add a trigger label (worker: ready, needs-changes)\n"
                if hidden:
                    self.assertEqual(stdout, expected)
                else:
                    self.assert_output(stdout, expected)
                run.assert_not_called()
                self.assertEqual(self.github.writes, [])
                self.assert_scoped()

    def test_health_wait_keeps_the_evaluated_agents_reason_after_view_close(self):
        with patch("ub_agents.agent_health.AgentHealth.check", return_value="Service is offline; retry later"):
            code, stdout, _, run = self.launch("11", hidden=True)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "#11 worker: waiting — Service is offline; retry later\n")
        run.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_read_errors_remain_visible_errors_after_view_close(self):
        for read in ("item", "comments", "blocked_by", "active_milestone"):
            with self.subTest(read=read):
                self.github = PollGitHub(issue(11))
                self.config = config(self.root, queue=Queue(milestones="gate"))
                self.github.dependencies[11] = [1]
                self.github.items[1] = issue(1)
                self.github.read_results[read] = [GitHubError("GET", "repos/org/project/issues/11", "HTTP 500")]
                code, stdout, stderr, run = self.launch("11", hidden=True)
                self.assertEqual(code, 1)
                self.assertIn("ub-agents:", stderr)
                self.assertIn("HTTP 500", stderr)
                self.assertNotIn("#11 worker:", stdout)
                run.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_agent_without_number_unknown_agent_and_invalid_numbers_are_usage_errors(self):
        for args in (("--agent", "worker"), ("11", "--agent", "unknown"), ("0",), ("-1",),
                     ("--number", "11")):
            with self.subTest(args=args), \
                    patch("ub_agents.cli.load_config", return_value=self.config), \
                    patch("ub_agents.cli.GitHub") as github, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(self.argv + list(args))
            self.assertEqual(raised.exception.code, 2)
            github.assert_not_called()

    def test_approval_refusal_performs_normal_parking_without_claiming(self):
        self.github.timelines[11] = []
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assertIn("#11 worker: parked — No maintainer", stdout)
        run.assert_not_called()
        self.assertIn("needs-human", self.github.items[11].labels)
        self.assertEqual(self.coordinator().history(11), [])
        self.assertTrue(self.github.writes)
        self.assert_scoped()

    def test_milestone_gate_refuses_but_order_does_not_rank(self):
        self.config = config(self.root, queue=Queue(milestones="gate"))
        self.github.milestones = [{"number": 3, "state": "open", "created_at": "2026-01-01T00:00:00Z"}]
        self.github.change(1, milestone=3)
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: parked — Waiting for active milestone #3\n")
        self.assertIn(("active_milestone", ()), self.github.reads)
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_other_gates_suppress_approval_parking_as_in_a_normal_pass(self):
        self.github.timelines[11] = []
        self.github.dependencies[11] = [1]
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: parked — Waiting for blockers #1\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_runtime_pause_uses_the_status_waiting_reason(self):
        worker = agent(self.root, command=(), runtimes=(Runtime("codex", "model", "high"),))
        self.config = config(self.root, worker)
        pause = {"reason": "usage limit reached", "ends_at": "2026-10-04T00:00:00Z"}
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", return_value=True), \
                patch("ub_agents.runtime_usage.RuntimeUsage.paused", return_value=pause):
            code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: waiting — Waiting for codex: usage limit reached; "
                                 "pause ends 2026-10-04T00:00:00Z\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_runtime_unavailability_uses_the_same_gate_as_queue_launch(self):
        worker = agent(self.root, command=(), runtimes=(Runtime("codex", "model", "high"),))
        self.config = config(self.root, worker)
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", side_effect=lambda cli: cli == "gh"):
            code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: blocked — No eligible runtime executable is installed, "
                                 "usable and available\n")
        self.assertEqual(self.github.writes, [])
        run.assert_not_called()
        self.assert_scoped()

    def test_backoff_and_attempt_limit_match_status_reasons(self):
        worker = replace(self.config.agents[0], backoff_seconds=120, max_backoff_seconds=120)
        self.config = config(self.root, worker)
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], worker, ()))
        co.update(lease, state="running", started=True)
        co.report(lease, "retry", "Try later")
        co.release(lease, "retry", "Try later", 120, attempt_effect="failure")
        writes = self.github.writes[:]
        for role, reason in ((worker, "backoff — Durable retry backoff has not elapsed"),
                             (replace(worker, max_attempts=1),
                              "blocked — Attempt limit exhausted; inspect failures and use "
                              "ub-agents retry 11 --agent worker --reason TEXT")):
            self.config = config(self.root, role)
            code, stdout, _, run = self.launch("11")
            self.assertEqual(code, 1)
            self.assert_output(stdout, f"#11 worker: {reason}\n")
            run.assert_not_called()
            self.assertEqual(self.github.writes, writes)
        self.assert_scoped()

    def test_pending_completion_recovers_even_without_a_trigger_and_with_stop_label(self):
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.config.agents[0], ()))
        co.update(lease, state="running", started=True)
        outcome = co.report(lease, "retry", "Reported before launcher stopped")
        self.now += 61
        self.github.change(11, labels=frozenset({"needs-human"}))
        self.github.reads.clear()
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("#11 worker: recovered durable outcome; no execution started", stdout)
        history = self.coordinator().history(11)
        recovery = next(r for r in history if r.get("mode") == "recovery")
        self.assertEqual(recovery["recovered_lease_id"], lease["id"])
        self.assertEqual(recovery["state"], "released")
        self.assertEqual(history[1]["id"], outcome["id"])
        self.assert_scoped()

    def test_pending_success_is_accepted_without_reexecution(self):
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.config.agents[0], ()))
        co.update(lease, state="running", started=True)
        co.report(lease, "success", "Completed before launcher stopped", outcome="done")
        self.now += 61
        self.github.reads.clear()
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        self.assertIn("#11 worker: recovered durable outcome; no execution started", stdout)
        run.assert_not_called()
        history = co.history(11)
        self.assertTrue(history[1]["accepted"])
        recovery = next(r for r in history if r.get("mode") == "recovery")
        self.assertEqual((recovery["state"], recovery["result"]), ("released", "success"))
        self.assert_scoped()

    def test_refresh_rechecks_only_target_and_refuses_new_stop_label(self):
        self.config = replace(self.config, launchers=("operator", "peer"))

        def refresh(*_, on_fetch=None):
            self.github.change(11, labels=frozenset({"ready", "needs-human"}))

        code, stdout, _, run = self.launch("11", refresh=refresh)
        self.assertEqual(code, 1)
        self.assert_output(stdout, "#11 worker: parked — Stop label needs-human is present; "
                           "parked until a person acts on its Action needed notice, "
                           "removes the stop label and restores a trigger\n")
        run.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assert_scoped()

    def test_refresh_removes_launcher_authority_before_claiming(self):
        for launchers, role, reason in (
                (("peer",), "write", "Launcher account @operator is not listed in launchers"),
                (("operator", "peer"), "read",
                 "Launcher account @operator has repository role read; write or higher is required")):
            with self.subTest(launchers=launchers, role=role):
                self.config = replace(self.config, launchers=("operator", "peer"))
                self.github.roles["operator"] = "write"

                def refresh(*_, on_fetch=None):
                    self.config = replace(self.config, launchers=launchers)
                    self.github.roles["operator"] = role

                code, stdout, _, run = self.launch("11", refresh=refresh)
                self.assertEqual(code, 1)
                self.assert_output(stdout, f"#11 worker: blocked — {reason}\n")
                run.assert_not_called()
                self.assertEqual(self.github.writes, [])
                self.assert_scoped()

    def test_refresh_adds_peer_owner_and_refuses_without_queue_reads(self):
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        peer.update(lease, state="running", started=True, host="remote-host")
        self.config = replace(self.config, launchers=("operator",))
        writes = self.github.writes[:]
        self.github.reads.clear()

        def refresh(*_, on_fetch=None):
            self.config = replace(self.config, launchers=("OPERATOR", "PEER"))

        with patch("ub_agents.loop.socket.gethostname", return_value="local-host"):
            code, stdout, _, run = self.launch("11", refresh=refresh)
        self.assertEqual(code, 1)
        self.assertIn("#11 worker: owned — claimed by @peer on remote-host", stdout)
        self.assertIn("Process can't be checked from here; lease is on another host.", stdout)
        run.assert_not_called()
        self.assertEqual(self.github.writes, writes)
        self.assert_scoped()

    def test_refresh_removes_new_peer_owner_and_claim_uses_refreshed_trust(self):
        self.config = replace(self.config, launchers=("operator", "peer"))
        self.github.roles["peer"] = "write"

        def refresh(*_, on_fetch=None):
            peer = self.coordinator(AccountGitHub(self.github, "peer"))
            lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
            peer.update(lease, state="running", started=True)
            self.config = replace(self.config, launchers=("OPERATOR",))

        code, stdout, _, run = self.launch("11", refresh=refresh)
        self.assertEqual(code, 0)
        self.assertIn("#11 worker: claimed", stdout)
        run.assert_called_once()
        lease, outcome = self.coordinator().history(11)
        self.assertEqual((lease["actor"], lease["state"], outcome["accepted"]),
                         ("operator", "released", True))
        self.assert_scoped()

    def test_configured_launchers_recover_peer_outcome_without_reexecution(self):
        self.config = replace(self.config, launchers=("operator", "peer"))
        self.github.roles["peer"] = "write"
        peer = self.coordinator(AccountGitHub(self.github, "peer"))
        lease = peer.claim(peer.plan(self.github.items[11], self.config.agents[0], ()))
        peer.update(lease, state="running", started=True)
        peer.report(lease, "success", "Peer finished before launcher stopped", outcome="done")
        self.now += 61
        self.github.reads.clear()
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        self.assertIn("#11 worker: recovered durable outcome; no execution started", stdout)
        run.assert_not_called()
        history = self.coordinator().history(11)
        self.assertTrue(history[1]["accepted"])
        recovery = next(r for r in history if r.get("mode") == "recovery")
        self.assertEqual((recovery["actor"], recovery["recovered_lease_id"], recovery["state"]),
                         ("operator", lease["id"], "released"))
        self.assert_scoped()

    def test_sigterm_drains_target_and_sigint_terminates_it_with_normal_exit_codes(self):
        for sig, expected in ((signal.SIGTERM, 0), (signal.SIGINT, 130)):
            with self.subTest(signal=sig):
                self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
                handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}

                def execute(*args, **kwargs):
                    signal.raise_signal(sig)
                    self.assertTrue(self.loop.stop_event.is_set())
                    self.assertEqual(self.loop.interrupt_event.is_set(), sig == signal.SIGINT)
                    if sig == signal.SIGINT:
                        raise KeyboardInterrupt
                    return self.report_success(*args, **kwargs)

                code, stdout, stderr, run = self.launch("11", execute=execute)
                self.assertEqual(code, expected)
                run.assert_called_once()
                lease = self.coordinator().history(11)[0]
                self.assertEqual(lease["state"], "released")
                if sig == signal.SIGINT:
                    self.assertEqual(lease["attempt_effect"], "unchanged")
                    self.assertEqual(stderr, "Stopped; supervised execution terminated\n")
                self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)
                self.assert_scoped()

    def test_pr_target_uses_pr_approval_without_queue_or_issue_dependency_reads(self):
        self.github.items[11] = pr(11)
        code, stdout, _, run = self.launch("11")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertIn("#11 worker: claimed", stdout)
        self.assertIn(("pr_content", (11,)), self.github.reads)
        self.assertFalse(any(name == "blocked_by" for name, _ in self.github.reads))
        self.assert_scoped()
