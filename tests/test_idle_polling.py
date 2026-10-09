from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.polling import DiscoveryBudget, idle_interval
from ub_agents.request_stats import PassOutput
from tests.support import RecordingRunner, config, isolate_observations


def quota(remaining=999, reset=4600, limit=5000):
    return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-limit": str(limit),
            "x-ratelimit-reset": str(reset)}


class IdlePollingTests(unittest.TestCase):
    def setUp(self):
        isolate_observations(self)
        self.enterContext(patch("ub_agents.cli.launch_checks"))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = replace(config(self.root), poll_seconds=30)
        self.runner = RecordingRunner(self.root)
        self.github = GitHub("org/project", self.runner)
        self.lines = []
        self.loop = Loop(self.config, self.github, "operator", output=self.lines.append)
        self.loop.pass_output = PassOutput(lambda *_: None)
        self.now = 1000
        self.loop.coordinator.clock = lambda: self.now

    def response(self, endpoint, payload=None, *, method="GET", data=None, headers=None, status=200,
                 validator=None):
        command = ("gh", "api", "--hostname", "github.com", "--method", method, "-H",
                   "Accept: application/vnd.github+json", "--include", endpoint)
        if data is not None:
            command += ("--input", "-")
        if validator is not None:
            command += ("-H", f"If-None-Match: {validator}")
        header_text = "".join(f"{name}: {value}\n" for name, value in (headers or {}).items())
        self.runner.responses[command] = subprocess.CompletedProcess(
            [], int(status >= 400 or status == 304), f"HTTP/2.0 {status} Response\n{header_text}\n"
            + json.dumps({} if payload is None else payload), "")

    def run_passes(self, tick, count=2):
        starts, waits = [], []

        def pass_():
            starts.append(self.now)
            if len(starts) == count:
                self.loop.stop_event.set()
                return False
            return tick()

        def wait(delay):
            waits.append(delay)
            self.now += delay

        with patch("ub_agents.loop.monotonic", side_effect=lambda: self.now), \
                patch.object(self.loop, "_discovery_tick", side_effect=pass_), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        return starts, waits

    def test_rest_balance_and_minimum_gap_from_pass_start(self):
        for requests, duration, gap in ((0, 3, 30), (1, 3, 30), (5, 3, 30), (123, 3, 30),
                                        (230, 10, 1536.4), (300, 10, 2544.4), (5, 100, 100)):
            with self.subTest(requests=requests, duration=duration):
                self.setUp()
                self.response("user")

                def tick():
                    for _ in range(requests):
                        self.github.request("user")
                    self.now += duration
                    return False

                starts, waits = self.run_passes(tick)
                self.assertAlmostEqual(starts[1], 1000 + gap)
                self.assertEqual(len(waits), int(gap > duration))
                if waits:
                    self.assertAlmostEqual(waits[0], gap - duration)
                self.assertIn(f"({requests} requests last poll)", self.lines[0])

    def test_cold_burst_followed_by_cheap_passes_keeps_minimum_gap(self):
        self.response("user")
        costs = iter([123, 1, 1])

        def tick():
            for _ in range(next(costs)):
                self.github.request("user")
            return False

        starts, waits = self.run_passes(tick, count=4)
        self.assertEqual(starts, [1000, 1030, 1060, 1090])
        self.assertEqual(waits, [30, 30, 30])

    def test_sustained_300_request_passes_keep_debt_and_cap_each_gap_at_an_hour(self):
        self.response("user")

        def tick():
            for _ in range(300):
                self.github.request("user")
            return False

        starts, waits = self.run_passes(tick, count=4)
        self.assertAlmostEqual(waits[0], 2534.4)
        self.assertEqual(waits[1:], [3600, 3600])
        self.assertLess(self.loop.discovery_budget.balance, 0)

    def test_claiming_passes_after_work_bypass_budget_then_idle_repays_all_spend(self):
        self.response("user")
        results = iter([True, True, False])

        def tick():
            for _ in range(100):
                self.github.request("user")
            return next(results)

        starts, waits = self.run_passes(tick, count=4)
        self.assertEqual(starts[:3], [1000, 1000, 1000])
        self.assertAlmostEqual(waits[0], 2534.4)
        self.assertAlmostEqual(self.loop.discovery_budget.balance, 1)

    def test_failed_pass_debits_and_retry_backoff_does_not_erase_debt(self):
        from ub_agents.errors import GitHubError
        self.response("user")
        calls = 0

        def tick():
            nonlocal calls
            calls += 1
            if calls == 1:
                for _ in range(230):
                    self.github.request("user")
                raise GitHubError("GET", "user", "temporary", retryable=True)
            return False

        starts, waits = self.run_passes(tick, count=3)
        self.assertEqual(starts[:2], [1000, 1005])
        self.assertAlmostEqual(starts[2], 2526.4)
        self.assertEqual(waits[0], 5)
        self.assertAlmostEqual(waits[1], 1521.4)

    def test_runtime_pause_expiry_wakes_idle_poll_with_discovery_debt(self):
        self.response("user")
        costs = iter([230, 0])

        def tick():
            requests = next(costs)
            for _ in range(requests):
                self.github.request("user")
            if requests:
                self.loop.usage.pauses["claude"] = self.now + 300
            else:
                self.assertIsNone(self.loop.usage.paused("claude"))
                self.assertLess(self.loop.discovery_budget.balance, 0)
            return False

        starts, waits = self.run_passes(tick, count=3)
        self.assertEqual(starts[:2], [1000, 1300])
        self.assertAlmostEqual(starts[2], 2526.4)
        self.assertEqual(waits[0], 300)
        self.assertAlmostEqual(waits[1], 1226.4)
        self.assertIn("next poll in 5 min (230 requests last poll)", self.lines[0])

    def test_idle_rechecks_debit_from_cancelled_observation_during_wait(self):
        def wait(delay):
            self.now += delay
            if self.now == 1030:
                self.loop.discovery_budget.debit(230)

        with patch.object(self.loop.stop_event, "wait", side_effect=wait):
            starts = []

            def tick():
                starts.append(self.now)
                if len(starts) == 2:
                    self.loop.stop_event.set()
                return False

            with patch("ub_agents.loop.monotonic", side_effect=lambda: self.now), \
                    patch.object(self.loop, "_discovery_tick", side_effect=tick), \
                    self.assertRaises(KeyboardInterrupt):
                self.loop.launch()
        self.assertAlmostEqual(starts[1], 2556.4)

    def test_cancelled_observation_debt_cannot_extend_wait_past_runtime_pause(self):
        starts, waits = [], []

        def wait(delay):
            waits.append(delay)
            self.now += delay
            if self.now == 1030:
                self.loop.discovery_budget.debit(230)

        def tick():
            starts.append(self.now)
            if len(starts) == 2:
                self.loop.stop_event.set()
            else:
                self.loop.usage.pauses["claude"] = self.now + 300
            return False

        with patch("ub_agents.loop.monotonic", side_effect=lambda: self.now), \
                patch.object(self.loop, "_discovery_tick", side_effect=tick), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        self.assertEqual(starts, [1000, 1300])
        self.assertEqual(waits, [30, 270])
        self.assertLess(self.loop.discovery_budget.balance, 0)

    def test_idle_output_rounds_seconds_under_a_minute_and_minutes_otherwise(self):
        for delay, expected in ((0.4, "0s"), (55.625, "56s"), (59.6, "60s"),
                                (60, "1 min"), (72, "1 min"), (90, "2 min"), (120.4, "2 min")):
            with self.subTest(delay=delay):
                self.config = replace(self.config, poll_seconds=delay)
                self.loop.config = self.config
                self.lines.clear()
                self.loop.stop_event.clear()
                self.run_passes(lambda: False)
                self.assertIn(f"next poll in {expected} (0 requests last poll)", self.lines[0])

    def test_pages_writes_and_graphql_use_real_adapter(self):
        self.response("rows?per_page=100&page=1", [{}] * 100)
        self.response("rows?per_page=100&page=2", [])
        self.response("park", method="POST", data={})
        self.response("park", method="DELETE", status=204)
        self.response("graphql", method="POST", data={},
                      headers={"X-RateLimit-Resource": "graphql"} | quota(2000))

        def tick():
            self.github.request("rows", paginate=True)
            self.github.request("park", "POST", {})
            self.github.request("park", "DELETE")
            for _ in range(10):
                self.github.request("graphql", "POST", {})
            return False

        starts, waits = self.run_passes(tick)
        self.assertEqual(waits[0], 30)
        self.assertEqual(self.github.rest_requests, 4)
        self.assertEqual(self.github.quota_requests, 4)
        self.assertEqual(len(self.runner.calls), 14)
        self.assertIn("(4 requests last poll)", self.lines[0])
        self.assertEqual(self.github.resource_quotas["graphql"]["x-ratelimit-remaining"], "2000")
        self.assertFalse(any("rate_limit" in command for command, _ in self.runner.calls))

    def test_idle_budget_excludes_revalidations_and_observes_their_quota_headers(self):
        for remaining, gap in ((2000, 30), (999, 60)):
            with self.subTest(remaining=remaining):
                self.setUp()
                for number in range(5):
                    endpoint = f"rows/{number}"
                    self.response(endpoint, {"version": 1}, headers={"ETag": '"one"'})
                    self.github.request(endpoint)
                    self.response(endpoint, {"version": 2} if number == 0 else None,
                                  status=200 if number == 0 else 304, validator='"one"',
                                  headers={"ETag": '"two"' if number == 0 else '"one"',
                                           "X-RateLimit-Resource": "core"} | quota(remaining))

                def tick():
                    for number in range(5):
                        self.assertEqual(self.github.request(f"rows/{number}"),
                                         {"version": 2 if number == 0 else 1})
                    return False

                starts, waits = self.run_passes(tick)
                self.assertEqual(starts, [1000, 1000 + gap])
                self.assertEqual(waits, [gap])
                self.assertEqual(self.github.rest_requests, 10)
                self.assertEqual(self.github.quota_requests, 6)
                self.assertIn("(1 requests last poll)", self.lines[0])
                self.assertEqual(self.github.resource_quotas["core"]["x-ratelimit-remaining"],
                                 str(remaining))

    def test_rate_limit_retries_count_and_wait_consumes_idle_gap(self):
        for duration in (10, 100):
            with self.subTest(duration=duration):
                self.setUp()
                self.response("user", {"login": "operator"}, status=429,
                              headers={"Retry-After": str(duration)})
                response = self.runner.responses.copy()
                self.response("user", {"login": "operator"})
                success = self.runner.responses.copy()
                calls = [0]

                def runner(command, **kwargs):
                    calls[0] += 1
                    self.runner.responses = response if calls[0] == 1 else success
                    return self.runner(command, **kwargs)

                self.github.runner = runner
                with patch("ub_agents.github.timestamp", side_effect=lambda: self.now):
                    starts, waits = self.run_passes(lambda: self.loop.github.actor() and False)
                self.assertEqual(self.github.rest_requests, 2)
                self.assertEqual(self.github.quota_requests, 2)
                self.assertEqual(starts, [1000, 1000 + max(30, duration)])
                self.assertEqual(waits, [duration, 30 - duration] if duration < 30 else [duration])

    def test_low_quota_reset_bounds_threshold_and_cap(self):
        for resource in ("core", "graphql"):
            for remaining, reset, budget_wait, expected in (
                    (1000, 4600, 69, 72), (999, 4600, 69, 144), (999, 1100, 69, 100),
                    (999, 1010, 69, 72), (999, 900, 69, 72), (999, 9999, 2877, 3600)):
                with self.subTest(resource=resource, remaining=remaining, reset=reset):
                    interval, low = idle_interval(budget_wait, 30, {resource: quota(remaining, reset)}, 1003, 3)
                    self.assertEqual(interval, expected)
                    self.assertEqual(low, frozenset({resource})
                                     if remaining < 1000 and reset > 1003 else frozenset())
        interval, low = idle_interval(72, 30, {"core": quota(reset=1100), "graphql": quota(reset=1080)}, 1000, 0)
        self.assertEqual(interval, 80)
        self.assertEqual(low, frozenset({"core", "graphql"}))
        for headers in ({}, quota(limit=0), quota(remaining=-1), quota(reset="nan"),
                        quota(remaining="bad"), quota(limit="inf")):
            with self.subTest(headers=headers):
                self.assertEqual(idle_interval(72, 30, {"core": headers}, 1000, 0), (72, frozenset()))

    def test_expired_low_resource_does_not_bound_live_low_resource(self):
        for live, expired in (("core", "graphql"), ("graphql", "core")):
            for expired_reset in (400, 1000):
                for live_reset, expected in ((3400, 144), (1100, 100)):
                    with self.subTest(live=live, expired_reset=expired_reset,
                                      live_reset=live_reset):
                        quotas = {live: quota(reset=live_reset),
                                  expired: quota(reset=expired_reset)}
                        self.assertEqual(idle_interval(72, 30, quotas, 1000, 0),
                                         (expected, frozenset({live})))

    def test_idle_logging_changes_when_retained_low_quota_expires(self):
        self.github.resource_quotas = {"graphql": quota(reset=1040)}
        self.response("user")

        def tick():
            for _ in range(5):
                self.github.request("user")
            return False

        starts, waits = self.run_passes(tick, count=4)
        self.assertEqual(waits, [40, 30, 30])
        self.assertEqual(self.lines, [
            "No eligible work; next poll in 40s (5 requests last poll)",
            "No eligible work; next poll in 30s (5 requests last poll)"])

    def test_low_quota_wait_is_bounded_by_reset_from_pass_start(self):
        self.response("user", headers={"X-RateLimit-Resource": "core"} | quota(reset=1040))

        def tick():
            for _ in range(5):
                self.github.request("user")
            self.now += 10
            return False

        starts, waits = self.run_passes(tick)
        self.assertEqual(starts, [1000, 1040])
        self.assertEqual(waits, [30])
        self.assertIn("next poll in 30s", self.lines[0])

    def test_idle_logging_changes_only_with_work_or_low_resource_state(self):
        states = iter([(False, 2000), (False, 2000), (False, 999), (False, 999),
                       (False, 1000), (True, 999), (False, 999), (False, 999)])

        def tick():
            worked, remaining = next(states)
            self.response("user", headers={"X-RateLimit-Resource": "core"} | quota(remaining, 99999))
            for _ in range(5):
                self.github.request("user")
            return worked

        starts, waits = self.run_passes(tick, count=9)
        self.assertEqual(waits, [30, 30, 60, 60, 30, 60, 60])
        self.assertEqual(self.lines, [
            "No eligible work; next poll in 30s (5 requests last poll)",
            "No eligible work; next poll in 1 min (5 requests last poll)",
            "No eligible work; next poll in 30s (5 requests last poll)",
            "No eligible work; next poll in 1 min (5 requests last poll)"])

    def test_once_empty_poll_never_waits_or_logs_idle(self):
        self.github.quota_requests = 500
        self.github.resource_quotas = {"core": quota()}
        with patch.object(self.loop, "tick", return_value=False), \
                patch.object(self.loop.stop_event, "wait") as wait:
            self.loop.launch(once=True)
        wait.assert_not_called()
        self.assertEqual(self.lines, [])

    def test_stop_signals_interrupt_budget_wait(self):
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=sig):
                self.setUp()
                self.response("user")
                handler = signal.getsignal(sig)

                def tick():
                    for _ in range(230):
                        self.github.request("user")
                    return False

                def wait(delay):
                    self.assertGreater(delay, 60)
                    signal.raise_signal(sig)

                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(self.github, "actor", return_value="operator"), \
                        patch.object(Loop, "_discovery_tick", side_effect=tick) as ticks, \
                        patch("threading.Event.wait", side_effect=wait), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["launch"]), 0 if sig == signal.SIGTERM else 130)
                ticks.assert_called_once()
                self.assertEqual(signal.getsignal(sig), handler)


class DiscoveryBudgetTests(unittest.TestCase):
    def test_starts_full_refills_at_250_per_hour_keeps_debt_and_caps_savings(self):
        now = 0
        budget = DiscoveryBudget(clock=lambda: now)
        self.assertEqual(budget.balance, 125)
        budget.debit(230)
        self.assertEqual(budget.balance, -105)
        self.assertAlmostEqual(budget.wait_seconds(), 1526.4)
        now += 720
        self.assertAlmostEqual(budget.wait_seconds(), 806.4)
        self.assertEqual(budget.balance, -55)
        budget.debit(100)
        now += 3600
        self.assertEqual(budget.wait_seconds(), 0)
        self.assertEqual(budget.balance, 95)
        now += 3600
        self.assertEqual(budget.wait_seconds(), 0)
        self.assertEqual(budget.balance, 125)
