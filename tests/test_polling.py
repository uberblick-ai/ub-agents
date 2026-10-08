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
from ub_agents.config import Priority, Queue, Runtime
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
        self.enterContext(patch("ub_agents.cli.launch_checks"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = PollGitHub(issue())
        self.config = config(self.root, agent(self.root, kind="issue"))
        self.lines = []
        self.loop = Loop(self.config, self.github, "operator", output=self.lines.append)

    def request_error(self, response, *, method="GET", endpoint="repos/org/project/issues/comments?per_page=100"):
        """Use the real gh adapter's classification, driven by the recording fake."""
        runner = RecordingRunner(self.root)
        command = ("gh", "api", "--hostname", "github.com", "--method", method, "-H",
                   "Accept: application/vnd.github+json", "--include", endpoint)
        runner.responses[command] = response
        with self.assertRaises(GitHubError) as raised:
            GitHub("org/project", runner).request(endpoint, method=method, array=True)
        self.assertEqual(len(runner.calls), 1)
        return raised.exception

    def http_error(self, status=504, headers="", detail="Gateway timeout"):
        return self.request_error(subprocess.CompletedProcess(
            [], 1, f"HTTP/2.0 {status} Error\n{headers}\n{{}}", f"gh: {detail} (HTTP {status})"))

    def claim_error(self):
        return self.request_error(subprocess.CompletedProcess([], 1, "", "unexpected end of JSON input"),
                                  method="POST", endpoint="repos/org/project/issues/1/comments")

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
                self.assertFalse(any("next poll in" in line for line in self.lines))

    def test_failed_reads_through_claim_revalidation_never_write(self):
        cases = [("observe", [], Queue()), ("repository_comments", [], Queue()),
                 ("comments", [], Queue()), ("comments", [None], Queue()),
                 ("item", [], Queue()), ("milestone_order", [], Queue(milestones="order")),
                 ("active_milestone", [], Queue(milestones="gate")),
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
        self.assertIn("Fix the cause and restart ub-agents launch", stderr.getvalue())

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
        self.assertEqual(self.lines.count("No open issue or PR has a trigger label (ready, needs-changes); "
                                          "add one to start; next poll in 1s (0 requests last poll)"), 1)

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
            if len(delays) == 2:
                self.loop.stop_event.set()

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [5, 5])

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
        self.assertEqual(delays, [5, 5])
        self.assertEqual(executions, [2, 3])
        self.assertEqual(count(), 0)

    def test_rate_limits_wait_without_counting_poll_failures(self):
        with patch("ub_agents.github.timestamp", return_value=1000):
            limited = self.http_error(429, "Retry-After: 12\n", "secondary rate limit")
        primary = self.http_error(403, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 4595\n",
                                  "API rate limit exceeded")
        self.github.items.clear()
        failure = self.http_error()
        self.github.read_results["observe"] = [failure, limited, primary, failure, None]
        delays = []

        def wait(delay):
            self.assertEqual(self.github.writes, [])
            delays.append(delay)
            if len(delays) == 5:
                self.loop.stop_event.set()

        self.loop.coordinator.clock = lambda: 1000
        with patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [5, 12, 3600, 10, self.config.poll_seconds])
        self.assertIn("GitHub rate limit reached; waiting until 1970-01-01T01:16:40Z (60 min)", self.lines)
        self.assertEqual(sum(line.startswith("GitHub rate limit reached") for line in self.lines), 2)

    def test_repeated_rate_limits_never_exhaust_poll_limit(self):
        self.github.items.clear()
        self.github.read_results["observe"] = [GitHubError(
            "GET", "repos/org/project/issues", "HTTP 429 secondary rate limit",
            retryable=True, reset_at=1010, rate_limited=True
        )] * (POLL_FAILURE_LIMIT + 1)
        delays = []

        def wait(delay):
            delays.append(delay)
            if len(delays) == POLL_FAILURE_LIMIT + 2:
                self.loop.stop_event.set()

        self.loop.coordinator.clock = lambda: 1000
        with patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [10] * (POLL_FAILURE_LIMIT + 1) + [self.config.poll_seconds])
        self.assertEqual(self.github.writes, [])

    def test_lazy_cached_reads_wait_then_claim_without_parking(self):
        cases = [("comments", "issue"), ("timeline", "issue"), ("role", "issue"),
                 ("issue_content", "issue"), ("blocked_by", "issue"),
                 ("dependency_graph", "issue"), ("item", "pr"),
                 ("pr_content", "pr"), ("reviews", "pr"), ("review_comments", "pr")]
        for name, kind in cases:
            with self.subTest(read=name, kind=kind):
                self.setUp()
                item = issue(1) if kind == "issue" else pr(1, body="")
                self.github.items = {1: item, 2: replace(item, number=2)}
                queue = Queue(priority=Priority(("urgent", "low"))) if name == "dependency_graph" else Queue()
                self.loop.config = replace(self.config, agents=(agent(self.root, kind=kind),), queue=queue)
                self.loop.coordinator.queue = queue
                limited = GitHubError("GET", name, "secondary rate limit", rate_limited=True,
                                      reset_at=1012)
                self.github.read_results[name] = [limited]
                self.loop.coordinator.clock = lambda: 1000

                def wait(delay):
                    self.assertEqual(delay, 12)
                    self.assertEqual(self.github.writes, [])

                with patch.object(self.loop.stop_event, "wait", side_effect=wait) as waits, \
                        patch("ub_agents.loop.supervise", side_effect=self.finish), \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                waits.assert_called_once_with(12)
                self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "success")
                self.assertEqual(sum(line.startswith("GitHub rate limit reached") for line in self.lines), 1)
                self.assertFalse(any(line.startswith("Skipped") for line in self.lines))
                self.assertNotIn("needs-human", self.github.items[1].labels)
                self.assertFalse(any(read in {"item", "comments", "timeline", "issue_content", "pr_content"}
                                     and args[0] == 2 for read, args in self.github.reads))

    def test_rate_limit_wait_counts_toward_minimum_poll_gap(self):
        for duration in (12, 40):
            with self.subTest(duration=duration):
                self.setUp()
                self.github.items.clear()
                self.github.read_results["observe"] = [GitHubError(
                    "GET", "issues", "rate limited", rate_limited=True, reset_at=1000 + duration)]
                now, delays, starts = [0], [], []
                self.loop.coordinator.clock = lambda: 1000 + now[0]
                tick = self.loop.tick

                def discover():
                    starts.append(now[0])
                    if len(starts) == 2:
                        self.loop.stop_event.set()
                    return tick()

                def wait(delay):
                    self.assertEqual(self.github.writes, [])
                    delays.append(delay)
                    now[0] += delay

                with patch("ub_agents.loop.monotonic", side_effect=lambda: now[0]), \
                        patch.object(self.loop, "tick", side_effect=discover), \
                        patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                self.assertEqual(starts, [0, max(duration, self.config.poll_seconds)])
                expected = [duration]
                if duration < self.config.poll_seconds:
                    expected.append(self.config.poll_seconds - duration)
                self.assertEqual(delays, expected)

    def test_primary_reset_an_hour_away_retries_at_cap_then_waits_for_margin(self):
        primary = self.http_error(403, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 4600\n",
                                  "API rate limit exceeded")
        self.github.items.clear()
        self.github.read_results["observe"] = [primary, primary, None]
        now, delays = [1000], []
        self.loop.coordinator.clock = lambda: now[0]

        def wait(delay):
            self.assertEqual(self.github.writes, [])
            delays.append(delay)
            now[0] += delay
            if len(delays) == 3:
                self.loop.stop_event.set()

        with patch.object(self.loop.stop_event, "wait", side_effect=wait), self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(delays, [3600, 5, self.config.poll_seconds])
        self.assertEqual(self.github.reads[:3], [("observe", ())] * 3)
        self.assertEqual(sum(line.startswith("GitHub rate limit reached") for line in self.lines), 2)

    def test_missing_unreadable_and_distant_resets_fallback_or_cap(self):
        cases = [("", 60), ("Retry-After: invalid\n", 60), ("Retry-After: nan\n", 60),
                 ("Retry-After: inf\n", 60), ("Retry-After: -1\n", 60), ("Retry-After: 61\n", 61),
                 ("X-RateLimit-Remaining: 0\n", 60),
                 ("X-RateLimit-Remaining: 0\nX-RateLimit-Reset: invalid\n", 60),
                 ("X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 0\n", 60),
                 ("X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 4600\n", 3600)]
        for header, delay in cases:
            with self.subTest(header=header), patch("ub_agents.github.timestamp", return_value=1000):
                self.setUp()
                self.loop.coordinator.clock = lambda: 1000
                self.github.read_results["observe"] = [self.http_error(429, header, "secondary rate limit")]
                with patch.object(self.loop.stop_event, "wait", side_effect=lambda _: self.loop.stop_event.set()) as wait, \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                wait.assert_called_once_with(delay)
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
        for mode, name in (("gate", "active_milestone"), ("order", "milestone_order")):
            with self.subTest(mode=mode):
                with self.assertRaises(GitHubError) as raised:
                    getattr(GitHub("org/project", runner), name)()
                self.github.read_results[name] = [raised.exception]
                self.loop.config = replace(self.config, queue=Queue(milestones=mode))
                with patch.object(self.loop.stop_event, "wait") as waits, \
                        self.assertRaisesRegex(AgentError, "GET repos/org/project/milestones.*Unreadable"):
                    self.loop.launch()
                waits.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_once_numbered_and_status_exit_one_without_retry(self):
        for argv in (["launch", "--once"], ["launch", "1"], ["status"]):
            with self.subTest(argv=argv):
                error = self.http_error(403, "X-RateLimit-Remaining: 0\n", "Rate limit\nsecond gh line")
                name = "item" if argv == ["launch", "1"] else "observe"
                self.github.read_results[name] = [error]
                stderr = io.StringIO()
                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch("ub_agents.cli.Loop", return_value=self.loop), \
                        patch.object(self.loop.stop_event, "wait") as waits, redirect_stderr(stderr):
                    self.assertEqual(main(argv), 1)
                waits.assert_not_called()
                self.assertEqual(self.github.writes, [])
                if argv[0] == "launch":
                    prefix = "Cannot read #1: " if argv == ["launch", "1"] else ""
                    self.assertEqual(stderr.getvalue(), f"ub-agents: {prefix}{' '.join(str(error).split())}; "
                                     "Fix the cause and restart ub-agents launch.\n")
                else:
                    self.assertEqual(stderr.getvalue(), f"ub-agents: {error}\n")

    def test_launch_startup_github_failure_also_has_one_line_and_next_step(self):
        error = self.request_error(subprocess.CompletedProcess([], 1, "", "gh: unavailable\nsecond line"))
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.launch_checks", side_effect=error), redirect_stderr(stderr):
            self.assertEqual(main(["launch", "--once"]), 1)
        self.assertEqual(stderr.getvalue(), f"ub-agents: {' '.join(str(error).split())}; "
                         "Fix the cause and restart ub-agents launch.\n")
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
                self.github.read_results["observe"] = [self.http_error(403, "X-RateLimit-Remaining: 0\n")]
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

    def test_initial_authentication_rate_limit_waits_with_signal_handlers_installed(self):
        self.loop.coordinator.actor = None
        limited = self.http_error(403, "X-RateLimit-Remaining: 0\n")
        interrupt = threading.Event()
        self.loop.interrupt_event = interrupt
        with patch.object(self.github, "actor", side_effect=limited), \
                patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.threading.Event", side_effect=[self.loop.stop_event, interrupt]), \
                patch.object(self.loop.stop_event, "wait", side_effect=lambda _: signal.raise_signal(signal.SIGTERM)), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["launch"]), 0)
        self.assertEqual(self.github.writes, [])
        self.assertTrue(self.lines[0].startswith("GitHub rate limit reached"))

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

    def test_retryable_claim_post_without_comment_skips_then_claims(self):
        failure = self.claim_error()
        create = self.github.create_comment
        calls = []

        def post(number, body):
            calls.append(number)
            if len(calls) == 1:
                raise failure
            return create(number, body)

        def wait(delay):
            self.assertEqual(delay, POLL_RETRY_BASE_SECONDS)
            self.assertEqual(self.github.writes, [])
            self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])

        with patch.object(self.github, "create_comment", side_effect=post), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait) as waits, \
                patch("ub_agents.loop.supervise", side_effect=self.finish) as execute, \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        execute.assert_called_once()
        waits.assert_called_once()
        lease = self.loop.coordinator.history(1)[0]
        self.assertEqual(lease["attempt"], 1)
        self.assertIsNone(self.loop.coordinator.lost_claim)
        self.assertIn(f"Skipped GitHub poll: {failure}; retrying in 5s", self.lines)

    def test_lost_claim_response_with_comment_withdraws_before_planning(self):
        self.loop.config = replace(self.config, agents=(replace(self.config.agents[0], max_attempts=1,
                                   backoff_seconds=60, max_backoff_seconds=60),))
        create = self.github.create_comment
        calls = []

        def post(number, body):
            comment = create(number, body)
            calls.append(number)
            if len(calls) == 1:
                raise self.claim_error()
            return comment

        observe = self.github.observe
        passes = []

        def discover(*args, **kwargs):
            passes.append(len(passes))
            if len(passes) == 2:
                history = self.loop.coordinator.history(1)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["state"], "withdrawn")
                self.assertEqual(history[0]["summary"], "Claim response was lost; withdrawn")
                self.assertFalse(history[0]["started"])
                self.assertEqual(history[0]["attempt_effect"], "unchanged")
                self.assertEqual(attempts(history, "worker", timestamp()), [])
            return observe(*args, **kwargs)

        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[-1]
            self.assertEqual(lease["attempt"], 1)
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            self.loop.stop_event.set()
            return 0

        with patch.object(self.github, "create_comment", side_effect=post), \
                patch.object(self.github, "observe", side_effect=discover), \
                patch.object(self.loop.stop_event, "wait") as waits, \
                patch("ub_agents.loop.supervise", side_effect=execute) as supervise, \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        supervise.assert_called_once()
        waits.assert_called_once_with(POLL_RETRY_BASE_SECONDS)
        self.assertEqual(len(passes), 2)
        leases = [r for r in self.loop.coordinator.history(1) if r["kind"] == "lease"]
        self.assertEqual([r["attempt"] for r in leases], [1, 1])
        self.assertNotEqual(leases[0]["run"], leases[1]["run"])
        self.assertEqual(sum(line.startswith("Skipped") for line in self.lines), 1)

    def test_lost_claim_withdrawal_failures_retry_without_new_claims(self):
        for stage in ("history read", "withdrawal write", "withdrawal response"):
            with self.subTest(stage=stage):
                self.setUp()
                create, update = self.github.create_comment, self.github.update_comment
                posts, withdrawals, delays = [], [], []

                def post(number, body):
                    comment = create(number, body)
                    posts.append(number)
                    if len(posts) == 1:
                        raise self.claim_error()
                    return comment

                def withdraw(comment_id, body):
                    if "Claim response was lost; withdrawn" in body:
                        withdrawals.append(comment_id)
                        if len(withdrawals) == 1:
                            if stage == "withdrawal response":
                                update(comment_id, body)
                            if stage != "history read":
                                raise self.request_error(subprocess.CompletedProcess(
                                    [], 1, "", "unexpected end of JSON input"), method="PATCH",
                                    endpoint=f"repos/org/project/issues/comments/{comment_id}")
                    return update(comment_id, body)

                def wait(delay):
                    delays.append(delay)
                    self.assertEqual(posts, [1])
                    if len(delays) == 1 and stage == "history read":
                        self.github.read_results["comments"] = [self.http_error()]

                def execute(*args, **kwargs):
                    history = self.loop.coordinator.history(1)
                    self.assertEqual(history[0]["state"], "withdrawn")
                    self.assertEqual(history[-1]["attempt"], 1)
                    self.loop.coordinator.report(history[-1], "success", "Completed", outcome="done")
                    self.loop.stop_event.set()
                    return 0

                with patch.object(self.github, "create_comment", side_effect=post), \
                        patch.object(self.github, "update_comment", side_effect=withdraw), \
                        patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                        patch("ub_agents.loop.supervise", side_effect=execute) as supervise, \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                supervise.assert_called_once()
                self.assertEqual(delays, [5, 10])
                self.assertEqual(len(withdrawals), 2 if stage == "withdrawal write" else 1)
                self.assertIsNone(self.loop.coordinator.lost_claim)

    def test_retryable_claim_failures_exhaust_existing_poll_limit(self):
        failure = self.claim_error()
        with patch.object(self.github, "create_comment", side_effect=failure) as posts, \
                patch.object(self.loop.stop_event, "wait") as waits, \
                patch("ub_agents.loop.supervise") as execute, self.assertRaises(AgentError) as raised:
            self.loop.launch()
        self.assertEqual(posts.call_count, POLL_FAILURE_LIMIT)
        self.assertEqual([call.args[0] for call in waits.call_args_list], [5, 10, 20, 40, 60])
        self.assertIn("POST repos/org/project/issues/1/comments", str(raised.exception))
        self.assertIn("retries exhausted after 6 consecutive failed polls", str(raised.exception))
        self.assertIn("Fix the cause and restart ub-agents launch", str(raised.exception))
        self.assertEqual(sum(line.startswith("Skipped") for line in self.lines), POLL_FAILURE_LIMIT - 1)
        self.assertEqual(self.github.writes, [])
        execute.assert_not_called()

    def test_once_and_nonretryable_claim_writes_fail_on_first_error(self):
        for once, failure in ((True, self.claim_error()),
                              (False, GitHubError("POST", "repos/org/project/issues/1/comments", "permission denied"))):
            with self.subTest(once=once):
                self.setUp()
                with patch.object(self.github, "create_comment", side_effect=failure) as post, \
                        patch.object(self.loop.stop_event, "wait") as waits, \
                        self.assertRaises(GitHubError) as raised:
                    self.loop.launch(once=once)
                self.assertIs(raised.exception, failure)
                post.assert_called_once()
                waits.assert_not_called()
                self.assertFalse(any(line.startswith("Skipped") for line in self.lines))

    def test_post_write_reads_keep_first_failure_handling(self):
        for stage in ("election read", "after withdrawn claim"):
            with self.subTest(stage=stage):
                self.setUp()
                failure = self.http_error()
                if stage == "election read":
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
                    gap = duration if worked else max(30, duration)
                    self.assertEqual(starts, [100, 100 + gap])
                    self.assertEqual(waits, [30 - duration] if not worked and duration < 30 else [])

    def test_graceful_stop_after_work_prevents_immediate_next_pass(self):
        self.loop.interrupt_event = threading.Event()

        def finish():
            self.loop.stop_event.set()
            return True

        with patch.object(self.loop, "tick", side_effect=finish) as tick, \
                patch.object(self.loop.stop_event, "wait") as wait:
            self.loop.launch()
        tick.assert_called_once()
        wait.assert_not_called()

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
