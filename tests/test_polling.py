from contextlib import redirect_stderr
from dataclasses import replace
import io
import signal
import threading
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Queue, Runtime
from ub_agents.errors import AgentError, CleanupError, GitHubError, LostOwnership, RecordError
from ub_agents.github import GitHub
from ub_agents.loop import Loop, POLL_FAILURE_LIMIT, POLL_RETRY_BASE_SECONDS, POLL_RETRY_MAX_SECONDS
from ub_agents.records import attempts, iso, timestamp
from tests.support import stub_refresh, PollGitHub, RecordingRunner, agent, config, issue, pr


class PollingTests(unittest.TestCase):
    def setUp(self):
        clock = patch("ub_agents.loop.monotonic", return_value=0)
        clock.start()
        self.addCleanup(clock.stop)
        self.refresh = stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = PollGitHub(issue())
        self.config = config(self.root, agent(self.root, kind="issue"))
        self.lines = []
        self.loop = Loop(self.config, self.github, "operator", output=self.lines.append)

    def request_error(self, response):
        """Use the real gh adapter's classification, driven by the recording fake."""
        runner = RecordingRunner(self.root)
        endpoint = "repos/org/project/issues/comments?per_page=100"
        command = ("gh", "api", "--hostname", "github.com", "--method", "GET", "-H",
                   "Accept: application/vnd.github+json", "--include", endpoint)
        runner.responses[command] = response
        with self.assertRaises(GitHubError) as raised:
            GitHub("org/project", runner).request(endpoint, array=True)
        self.assertEqual(len(runner.calls), 1)
        return raised.exception

    def http_error(self, status=504, headers="", detail="Gateway timeout"):
        return self.request_error(subprocess.CompletedProcess(
            [], 1, f"HTTP/2.0 {status} Error\n{headers}\n{{}}", f"gh: {detail} (HTTP {status})"))

    def finish(self, *args, **kwargs):
        lease = self.loop.coordinator.history(1)[0]
        self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        self.loop.stop_event.set()
        return 0

    def test_timeout_and_504_skip_entire_poll_then_start_work(self):
        for error in (self.http_error(), self.request_error(subprocess.TimeoutExpired("gh", 20))):
            with self.subTest(error=str(error)):
                self.setUp()
                self.github.read_results["repository_comments"] = [error]

                def wait(delay):
                    self.assertEqual(delay, POLL_RETRY_BASE_SECONDS)
                    self.assertEqual(self.github.writes, [])
                    self.assertFalse(any(name == "item" for name, _ in self.github.reads))
                    self.assertEqual(self.github.items[1].labels, frozenset({"ready"}))

                with patch.object(self.loop.stop_event, "wait", side_effect=wait) as waits, \
                        patch("ub_agents.loop.supervise", side_effect=self.finish) as execute, \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                execute.assert_called_once()
                waits.assert_called_once()
                self.assertEqual(sum(line.startswith("Skipped") for line in self.lines), 1)
                self.assertNotIn("Waiting for eligible GitHub work", self.lines)

    def test_failed_reads_through_claim_revalidation_never_write(self):
        cases = [("observe", [], Queue()), ("repository_comments", [], Queue()),
                 ("comments", [], Queue()), ("comments", [None], Queue()),
                 ("item", [], Queue()), ("active_milestone", [], Queue(milestones="gate")),
                 ("active_milestone", [None], Queue(milestones="gate")),
                 ("blocked_by", [], Queue()), ("blocked_by", [None], Queue())]
        for name, earlier, queue in cases:
            with self.subTest(read=name, earlier=len(earlier)):
                self.setUp()
                self.loop.config = replace(self.config, queue=queue)
                self.loop.coordinator.queue = queue
                self.github.read_results[name] = earlier + [self.http_error()]

                def stop(delay):
                    self.assertEqual(self.github.writes, [])
                    self.loop.stop_event.set()

                with patch.object(self.loop.stop_event, "wait", side_effect=stop), \
                        patch("ub_agents.loop.supervise") as execute, self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                execute.assert_not_called()
                self.assertEqual(len(self.lines), 1)
                self.assertTrue(self.lines[0].startswith("Skipped"))

    def test_increasing_capped_delays_and_exhaustion_exit_one(self):
        self.github.read_results["observe"] = [self.http_error()] * POLL_FAILURE_LIMIT
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch.object(self.loop.stop_event, "wait") as waits, redirect_stderr(stderr):
            self.assertEqual(main(["launch"]), 1)
        self.assertEqual([call.args[0] for call in waits.call_args_list], [5, 10, 20, 40, 60])
        self.assertEqual(len(self.lines), POLL_FAILURE_LIMIT - 1)
        self.assertEqual(self.github.writes, [])
        self.assertIn("GET repos/org/project/issues/comments", stderr.getvalue())
        self.assertIn("HTTP 504", stderr.getvalue())
        self.assertIn("retries exhausted after 6", stderr.getvalue())
        self.assertIn("Fix the cause and restart ub-agent launch", stderr.getvalue())

    def test_completed_empty_poll_resets_failure_count(self):
        self.github.items.clear()
        failure = self.http_error()
        self.github.read_results["observe"] = [failure, failure, None, failure]
        delays = []

        def wait(delay):
            delays.append(delay)
            if len(delays) == 4:
                self.loop.stop_event.set()

        with patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [5, 10, self.config.poll_seconds, 5])
        self.assertEqual(self.lines.count("Waiting for eligible GitHub work"), 1)

    def test_completed_work_poll_resets_failure_count(self):
        failure = self.http_error()
        self.github.read_results["observe"] = [failure, None, failure]
        delays = []

        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            return 0

        def wait(delay):
            delays.append(delay)
            if len(delays) == 3:
                self.loop.stop_event.set()

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [5, self.config.poll_seconds, 5])

    def test_poll_retries_preserve_item_failures_and_success_resets_them(self):
        lease = self.loop.coordinator.claim(self.loop.plans()[0])
        self.loop.coordinator.update(lease, state="running", started=True)
        self.loop.coordinator.report(lease, "retry", "Earlier execution failed")
        self.loop.coordinator.release(lease, "retry", "Earlier execution failed")
        earlier_writes = list(self.github.writes)
        failure = self.http_error()
        self.github.read_results["observe"] = [failure, None, failure, None]
        delays = []
        executions = []

        def count():
            return len(attempts(self.loop.coordinator.history(1), "worker", timestamp()))

        def wait(delay):
            delays.append(delay)
            if delay == self.config.poll_seconds:
                return
            self.assertEqual(count(), sum(d != self.config.poll_seconds for d in delays))
            if len(delays) == 1:
                self.assertEqual(self.github.writes, earlier_writes)
            leases = [r for r in self.loop.coordinator.history(1) if r["kind"] == "lease"]
            self.assertEqual(len(leases), sum(d != self.config.poll_seconds for d in delays))
            self.assertEqual(leases[-1]["result"], "retry")

        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[-1]
            executions.append(lease["attempt"])
            if len(executions) == 1:
                self.loop.coordinator.report(lease, "retry", "Execution failed again")
            else:
                self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
                self.loop.stop_event.set()
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [5, self.config.poll_seconds, 5])
        self.assertEqual(executions, [2, 3])
        self.assertEqual(count(), 0)

    def test_rate_limit_waits_for_reset_and_shares_failure_count(self):
        with patch("ub_agents.github.timestamp", return_value=1000):
            limited = self.http_error(429, "Retry-After: 12\n", "secondary rate limit")
        primary = self.http_error(403, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1060\n",
                                  "API rate limit exceeded")
        self.github.items.clear()
        self.github.read_results["observe"] = [limited, primary, self.http_error(), None]
        delays = []

        def wait(delay):
            self.assertEqual(self.github.writes, [])
            delays.append(delay)
            if len(delays) == 4:
                self.loop.stop_event.set()

        with patch("ub_agents.loop.timestamp", return_value=1000), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [12, 60, 20, self.config.poll_seconds])

    def test_repeated_rate_limits_exhaust_same_limit(self):
        self.github.read_results["observe"] = [GitHubError(
            "GET", "repos/org/project/issues", "HTTP 429 rate limit", retryable=True, reset_at=1010
        )] * POLL_FAILURE_LIMIT
        with patch("ub_agents.loop.timestamp", return_value=1000), \
                patch.object(self.loop.stop_event, "wait") as waits, \
                self.assertRaisesRegex(AgentError, "retries exhausted"):
            self.loop.launch()
        self.assertEqual([call.args[0] for call in waits.call_args_list], [10] * (POLL_FAILURE_LIMIT - 1))
        self.assertEqual(self.github.writes, [])

    def test_unusable_or_distant_rate_limit_reset_stops_without_wait(self):
        headers = ["", "Retry-After: invalid\n", "Retry-After: nan\n", "Retry-After: inf\n",
                   "Retry-After: -1\n", "Retry-After: 61\n",
                   "X-RateLimit-Remaining: 0\n", "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: invalid\n",
                   "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 0\n",
                   "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1061\n"]
        for header in headers:
            with self.subTest(header=header), patch("ub_agents.github.timestamp", return_value=1000):
                self.github.read_results["observe"] = [self.http_error(429, header, "rate limit")]
                with patch("ub_agents.loop.timestamp", return_value=1000), \
                        patch.object(self.loop.stop_event, "wait") as waits, \
                        self.assertRaisesRegex(AgentError, "Fix the cause and restart ub-agent launch"):
                    self.loop.launch()
                waits.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_permanent_and_unclassified_failures_stop_on_first_occurrence(self):
        errors = [self.http_error(401, detail="Bad credentials"),
                  self.http_error(403, detail="Resource not accessible"),
                  self.http_error(404, detail="Not Found"),
                  self.request_error("not json"), self.request_error("{}"),
                  self.request_error(subprocess.CompletedProcess([], 1, "", "unknown failure")),
                  self.request_error(FileNotFoundError("gh is missing")),
                  self.request_error(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"))]
        for error in errors:
            with self.subTest(error=str(error)):
                self.github.read_results["observe"] = [error]
                with patch.object(self.loop.stop_event, "wait") as waits, \
                        self.assertRaisesRegex(AgentError, "not retryable; retries not exhausted"):
                    self.loop.launch()
                waits.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_schema_error_names_request_and_stops(self):
        runner = RecordingRunner(self.root)
        endpoint = "repos/org/project/milestones?state=open&per_page=100&page=1"
        command = ("gh", "api", "--hostname", "github.com", "--method", "GET", "-H",
                   "Accept: application/vnd.github+json", "--include", endpoint)
        runner.responses[command] = '[{"number":1,"created_at":"invalid"}]'
        with self.assertRaises(GitHubError) as raised:
            GitHub("org/project", runner).active_milestone()
        self.github.read_results["active_milestone"] = [raised.exception]
        self.loop.config = replace(self.config, queue=Queue(milestones="gate"))
        with patch.object(self.loop.stop_event, "wait") as waits, \
                self.assertRaisesRegex(AgentError, "GET repos/org/project/milestones.*Unreadable"):
            self.loop.launch()
        waits.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_once_and_status_exit_one_without_retry(self):
        for argv in (["launch", "--once"], ["status"]):
            with self.subTest(argv=argv):
                self.github.read_results["observe"] = [self.http_error()]
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(self.loop.stop_event, "wait") as waits, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(argv), 1)
                waits.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_stop_during_wait_prevents_another_poll(self):
        self.github.read_results["observe"] = [self.http_error()]
        with patch.object(self.loop.stop_event, "wait", side_effect=lambda _: self.loop.stop_event.set()), \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(self.github.reads, [("observe", ())])
        self.assertEqual(self.github.writes, [])

    def test_cli_stop_signals_interrupt_retry_wait(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.setUp()
                self.github.read_results["observe"] = [self.http_error()]
                handler = signal.getsignal(sig)
                interrupt = threading.Event()
                self.loop.interrupt_event = interrupt
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch("ub_agents.cli.Loop", return_value=self.loop), \
                        patch("ub_agents.cli.threading.Event", side_effect=[self.loop.stop_event, interrupt]), \
                        patch.object(self.loop.stop_event, "wait", side_effect=lambda _: signal.raise_signal(sig)), \
                        redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["launch"]), 0 if sig == signal.SIGTERM else 130)
                self.assertEqual(signal.getsignal(sig), handler)
                self.assertEqual(self.github.reads, [("observe", ())])
                self.assertEqual(self.github.writes, [])

    def test_provenance_read_failure_skips_poll_instead_of_blocking_a_plan(self):
        builder = agent(self.root, name="builder", kind="issue", command=(),
                        runtimes=(Runtime("codex", "author-model", "high"),))
        reviewer = agent(self.root, name="reviewer", kind="pr", different_from="builder", command=(),
                         runtimes=(Runtime("claude", "review-model", "high"),))
        self.github.items[2] = pr(head="a" * 40)
        self.loop = Loop(config(self.root, builder, reviewer), self.github, "operator", output=self.lines.append)
        with patch("ub_agents.coordination.shutil.which", return_value="/synthetic/runtime"):
            plan = next(p for p in self.loop.plans() if p.agent.name == "builder")
            lease = self.loop.coordinator.claim(plan)
            self.loop.coordinator.update(lease, state="running", started=True)
            outcome = self.loop.coordinator.report(lease, "success", "Implemented", 2, outcome="done")
            self.loop.coordinator.accept(lease, outcome)
            self.loop.coordinator.release(lease, "success", "Implemented")
        self.github.change(1, labels=frozenset())
        writes = list(self.github.writes)
        # PR history succeeds, then independent-runtime provenance rereads issue #1.
        self.github.read_results["comments"] = [None, self.http_error()]

        def stop(delay):
            self.assertEqual(self.github.writes, writes)
            self.loop.stop_event.set()

        with patch("ub_agents.coordination.shutil.which", return_value="/synthetic/runtime"), \
                patch.object(self.loop.stop_event, "wait", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("Skipped"))

    def test_shared_branch_discovery_and_revalidation_failures_do_not_claim(self):
        for earlier in ([], [None]):
            with self.subTest(pr_reads_before_failure=len(earlier)):
                self.setUp()
                lease = self.loop.coordinator.claim(self.loop.plans()[0])
                self.loop.coordinator.update(lease, branch="feature/test", state="released", result="retry",
                                             started=True, expires=iso(timestamp() - 1))
                writes = list(self.github.writes)
                self.github.read_results["prs_for_branch"] = earlier + [self.http_error()]

                def stop(delay):
                    self.assertEqual(self.github.writes, writes)
                    self.loop.stop_event.set()

                with patch.object(self.loop.stop_event, "wait", side_effect=stop), \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                self.assertEqual(len(self.lines), 1)
                self.assertTrue(self.lines[0].startswith("Skipped"))

    def test_claim_write_and_post_write_reads_keep_first_failure_handling(self):
        for stage in ("lease write", "election read", "after withdrawn claim"):
            with self.subTest(stage=stage):
                self.setUp()
                failure = self.http_error()
                if stage == "lease write":
                    failure_patch = patch.object(self.github, "create_comment", side_effect=failure)
                elif stage == "election read":
                    self.github.read_results["comments"] = [None, None, None, failure]
                    failure_patch = patch("ub_agents.loop.supervise")
                else:
                    # A lost claim can return False and try another item in the same
                    # tick, but its later failure must not become a skipped poll.
                    self.github.items[2] = issue(2)
                    self.github.read_results["item"] = [None, failure]
                    failure_patch = patch("ub_agents.coordination.live_leases", return_value=[])
                with failure_patch, patch.object(self.loop.stop_event, "wait") as waits, \
                        self.assertRaises(GitHubError) as raised:
                    self.loop.launch()
                self.assertIs(raised.exception, failure)
                waits.assert_not_called()
                self.assertFalse(any(line.startswith("Skipped") for line in self.lines))

    def test_cleanup_and_ownership_errors_are_never_poll_retries(self):
        for error in (CleanupError("cleanup uncertain"), LostOwnership("lease lost"), RecordError("bad record")):
            self.github.read_results["observe"] = [error]
            with patch.object(self.loop.stop_event, "wait") as waits, self.assertRaises(type(error)) as raised:
                self.loop.launch()
            self.assertIs(raised.exception, error)
            waits.assert_not_called()

    def test_per_item_bad_record_remains_a_blocked_plan(self):
        self.github.read_results["comments"] = [RecordError("bad record")]
        with patch.object(self.loop.stop_event, "wait", side_effect=lambda _: self.loop.stop_event.set()), \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertTrue(any("blocked — bad record" in line for line in self.lines))
        self.assertEqual(self.github.writes, [])

    def test_recovery_read_failure_is_retryable_before_recovery_lease(self):
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        self.loop.coordinator.clock = lambda: timestamp() + 61
        writes = list(self.github.writes)
        self.github.read_results["comments"] = [None, self.http_error()]

        def stop(delay):
            self.assertEqual(self.github.writes, writes)
            self.loop.stop_event.set()

        with patch.object(self.loop.stop_event, "wait", side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(sum(line.startswith("Skipped") for line in self.lines), 1)

    def test_pass_start_gap_after_short_and_long_runs_and_idle_polls(self):
        for worked in (False, True):
            for duration in (3, 30, 45):
                with self.subTest(worked=worked, duration=duration):
                    self.setUp()
                    self.loop.config = replace(self.config, poll_seconds=30)
                    clock, starts, waits = [100], [], []
                    def tick():
                        starts.append(clock[0])
                        if len(starts) == 2:
                            self.loop.stop_event.set()
                        else:
                            # Completion/cleanup finish inside tick, before any wait.
                            clock[0] += duration
                            self.assertEqual(waits, [])
                        return worked
                    def wait(delay):
                        waits.append(delay)
                        clock[0] += delay
                    with patch("ub_agents.loop.monotonic", side_effect=lambda: clock[0]), \
                            patch.object(self.loop, "tick", side_effect=tick), \
                            patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                            self.assertRaises(KeyboardInterrupt):
                        self.loop.launch()
                    self.assertEqual(starts, [100, 100 + max(30, duration)])
                    self.assertEqual(waits, [30 - duration] if duration < 30 else [])

    def test_stop_wakes_gap_wait_after_work(self):
        self.loop.interrupt_event = threading.Event()
        with patch.object(self.loop, "tick", return_value=True) as tick, \
                patch.object(self.loop.stop_event, "wait", side_effect=lambda _: self.loop.stop_event.set()):
            self.loop.launch()
        tick.assert_called_once()

    def test_once_never_waits_after_work(self):
        with patch.object(self.loop, "tick", return_value=True), \
                patch.object(self.loop.stop_event, "wait") as wait:
            self.loop.launch(once=True)
        wait.assert_not_called()

    def test_documented_fixed_limits_match_code(self):
        docs = (Path(__file__).resolve().parents[1] / "docs/configuration.md").read_text()
        self.assertIn(f"**{POLL_RETRY_BASE_SECONDS} seconds**", docs)
        self.assertIn(f"**{POLL_RETRY_MAX_SECONDS} seconds**", docs)
        self.assertEqual(POLL_FAILURE_LIMIT, 6)
        self.assertIn("**sixth consecutive failed poll**", docs)
