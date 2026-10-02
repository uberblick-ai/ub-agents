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
from ub_agents.polling import idle_interval
from tests.support import RecordingRunner, config


def quota(remaining=999, reset=4600, limit=5000):
    return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-limit": str(limit),
            "x-ratelimit-reset": str(reset)}


class IdlePollingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = replace(config(self.root), poll_seconds=30)
        self.runner = RecordingRunner(self.root)
        self.github = GitHub("org/project", self.runner)
        self.lines = []
        self.loop = Loop(self.config, self.github, "operator", output=self.lines.append)
        self.now = 1000
        self.loop.coordinator.clock = lambda: self.now

    def response(self, endpoint, payload=None, *, method="GET", data=None, headers=None, status=200):
        command = ("gh", "api", "--hostname", "github.com", "--method", method, "-H",
                   "Accept: application/vnd.github+json", "--include", endpoint)
        if data is not None:
            command += ("--input", "-")
        header_text = "".join(f"{name}: {value}\n" for name, value in (headers or {}).items())
        self.runner.responses[command] = subprocess.CompletedProcess(
            [], int(status >= 400), f"HTTP/2.0 {status} Response\n{header_text}\n"
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
                patch.object(self.loop, "tick", side_effect=pass_), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                self.assertRaises(KeyboardInterrupt):
            self.loop.launch()
        return starts, waits

    def test_rest_cost_gap_from_pass_start_and_hour_cap(self):
        for requests, duration, gap in ((0, 3, 30), (1, 3, 30), (5, 3, 72),
                                        (230, 10, 3312), (300, 10, 3600), (5, 100, 100)):
            with self.subTest(requests=requests, duration=duration):
                self.setUp()
                self.response("user")

                def tick():
                    for _ in range(requests):
                        self.github.request("user")
                    self.now += duration
                    return False

                starts, waits = self.run_passes(tick)
                self.assertEqual(starts, [1000, 1000 + gap])
                self.assertEqual(waits, [gap - duration] if gap > duration else [])
                self.assertIn(f"({requests} requests last poll)", self.lines[0])

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
        self.assertAlmostEqual(waits[0], 57.6)
        self.assertEqual(self.github.rest_requests, 4)
        self.assertEqual(len(self.runner.calls), 14)
        self.assertIn("(4 requests last poll)", self.lines[0])
        self.assertEqual(self.github.resource_quotas["graphql"]["x-ratelimit-remaining"], "2000")
        self.assertFalse(any("rate_limit" in command for command, _ in self.runner.calls))

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
                self.assertEqual(starts, [1000, 1000 + max(30, duration)])
                self.assertEqual(waits, [duration, 30 - duration] if duration < 30 else [duration])

    def test_low_quota_reset_bounds_threshold_and_cap(self):
        for resource in ("core", "graphql"):
            for remaining, reset, requests, expected in (
                    (1000, 4600, 5, 72), (999, 4600, 5, 144), (999, 1100, 5, 100),
                    (999, 1010, 5, 72), (999, 900, 5, 72), (999, 9999, 200, 3600)):
                with self.subTest(resource=resource, remaining=remaining, reset=reset):
                    interval, low = idle_interval(requests, 30, {resource: quota(remaining, reset)}, 1003, 3)
                    self.assertEqual(interval, expected)
                    self.assertEqual(low, frozenset({resource}) if remaining < 1000 else frozenset())
        interval, low = idle_interval(5, 30, {"core": quota(reset=1100), "graphql": quota(reset=1080)}, 1000, 0)
        self.assertEqual(interval, 80)
        self.assertEqual(low, frozenset({"core", "graphql"}))
        for headers in ({}, quota(limit=0), quota(remaining=-1), quota(reset="nan"),
                        quota(remaining="bad"), quota(limit="inf")):
            with self.subTest(headers=headers):
                self.assertEqual(idle_interval(5, 30, {"core": headers}, 1000, 0), (72, frozenset()))

    def test_low_quota_wait_is_bounded_by_reset_from_pass_start(self):
        self.response("user", headers={"X-RateLimit-Resource": "core"} | quota(reset=1100))

        def tick():
            for _ in range(5):
                self.github.request("user")
            self.now += 10
            return False

        starts, waits = self.run_passes(tick)
        self.assertEqual(starts, [1000, 1100])
        self.assertEqual(waits, [90])
        self.assertIn("next poll in 1.5 min", self.lines[0])

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
        self.assertEqual(waits, [72, 72, 144, 144, 72, 30, 144, 144])
        self.assertEqual(self.lines, [
            "No eligible work; next poll in 1.2 min (5 requests last poll)",
            "No eligible work; next poll in 2.4 min (5 requests last poll)",
            "No eligible work; next poll in 1.2 min (5 requests last poll)",
            "No eligible work; next poll in 2.4 min (5 requests last poll)"])

    def test_once_empty_poll_never_waits_or_logs_idle(self):
        self.github.rest_requests = 500
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
                    for _ in range(5):
                        self.github.request("user")
                    return False

                def wait(delay):
                    self.assertGreater(delay, 60)
                    signal.raise_signal(sig)

                with patch("ub_agents.cli.load_config", return_value=self.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(self.github, "actor", return_value="operator"), \
                        patch.object(Loop, "tick", side_effect=tick) as ticks, \
                        patch("threading.Event.wait", side_effect=wait), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["launch"]), 0 if sig == signal.SIGTERM else 130)
                ticks.assert_called_once()
                self.assertEqual(signal.getsignal(sig), handler)
