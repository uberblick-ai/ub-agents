from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, timestamp
from tests.support import PollGitHub, agent, config, issue, stub_refresh


class RunRateLimitTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.github = PollGitHub(issue())
        self.lines = []
        self.stop, self.interrupt = threading.Event(), threading.Event()
        self.loop = Loop(config(self.root, agent(self.root, kind="issue")), self.github,
                         "operator", self.stop, self.lines.append, interrupt_event=self.interrupt)
        self.now = int(timestamp())
        self.loop.coordinator.clock = lambda: self.now

    def limit(self, delay=12):
        return GitHubError("GET", "comments", "secondary rate limit", retryable=True,
                           rate_limited=True, reset_at=self.now + delay)

    def test_ownership_read_waits_and_releases_normally(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.github.read_results["comments"] = [self.limit()]
            self.writes = self.github.writes.copy()
            self.loop.coordinator.assert_owned(lease)
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            return 0

        def wait(delay):
            self.assertEqual(delay, 12)
            self.assertEqual(self.github.writes, self.writes)
            self.now += delay

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.interrupt, "wait", side_effect=wait) as waits:
            self.assertTrue(self.loop.tick())
        waits.assert_called_once_with(12)
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual((lease["state"], lease["result"]), ("released", "success"))
        self.assertTrue(outcome["accepted"])
        self.assertEqual(attempts([lease, outcome], "worker", self.now), [])
        self.assertEqual(sum(line.startswith("GitHub rate limit reached") for line in self.lines), 1)

    def test_ownership_wait_beyond_lease_leaves_expiry_recovery(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            self.github.read_results["comments"] = [self.limit(60)]
            self.writes = self.github.writes.copy()
            self.loop.coordinator.assert_owned(lease)

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.interrupt, "wait", side_effect=lambda delay: setattr(self, "now", self.now + delay)) as wait, \
                self.assertRaises(LostOwnership):
            self.loop.tick()
        wait.assert_called_once_with(60)
        self.assertEqual(self.github.writes, self.writes)
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual(lease["state"], "running")
        self.assertFalse(outcome["accepted"])
        self.now += 61
        self.assertEqual(self.loop.plans()[0].state, "recover")

    def test_wall_clock_expiry_during_wait_stops_without_retrying_read(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.github.read_results["comments"] = [self.limit()]
            self.writes = self.github.writes.copy()
            self.loop.coordinator.assert_owned(lease)

        def wait(delay):
            # A suspend or slow wakeup can run past the deadline despite a short wait.
            self.now += 61

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.interrupt, "wait", side_effect=wait), \
                self.assertRaisesRegex(LostOwnership, "lost while waiting"):
            self.loop.tick()
        self.assertEqual(self.github.writes, self.writes)

    def test_ctrl_c_during_ownership_wait_follows_run_interrupt_handling(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.github.read_results["comments"] = [self.limit()]
            self.loop.coordinator.assert_owned(lease)

        def interrupt(delay):
            self.interrupt.set()
            self.stop.set()

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.interrupt, "wait", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.loop.tick()
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual((lease["state"], lease["attempt_effect"]), ("released", "unchanged"))
        self.assertIn("Launcher interrupted", outcome["summary"])

    def test_sigterm_during_run_wait_keeps_draining(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.github.read_results["comments"] = [self.limit()]
            self.loop.coordinator.assert_owned(lease)
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            return 0

        def wait(delay):
            signal.raise_signal(signal.SIGTERM)
            self.assertFalse(self.interrupt.is_set())
            self.now += delay

        old = signal.signal(signal.SIGTERM, lambda *_: self.loop.stop_gracefully())
        try:
            with patch("ub_agents.loop.supervise", side_effect=execute), \
                    patch.object(self.interrupt, "wait", side_effect=wait):
                self.loop.launch()
        finally:
            signal.signal(signal.SIGTERM, old)
        self.assertTrue(self.stop.is_set())
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "success")

    def test_completion_and_recovery_reads_wait_under_their_current_lease(self):
        def execute(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            # Completion first checks ownership, then reads the outcome.
            self.github.read_results["comments"] = [None, self.limit()]
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(self.interrupt, "wait") as wait:
            self.loop.tick()
        wait.assert_called_once_with(12)
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "success")

        self.setUp()
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        self.now += 61
        plan = self.loop.plans()[0]
        finalize = self.loop.finalize

        def recover(*args):
            self.github.read_results["item"] = [self.limit()]
            return finalize(*args)

        with patch.object(self.loop, "finalize", side_effect=recover), \
                patch.object(self.interrupt, "wait") as wait:
            self.loop.recover(plan)
        wait.assert_called_once_with(12)
        self.assertTrue(self.loop.coordinator.outcome(lease)["accepted"])
        recovery = next(r for r in self.loop.coordinator.history(1) if r.get("mode") == "recovery")
        self.assertEqual((recovery["state"], recovery["result"]), ("released", "success"))

    def test_rate_limited_write_is_never_retried(self):
        failure = self.limit()
        with patch.object(self.github, "create_comment", side_effect=failure) as write, \
                patch.object(self.interrupt, "wait") as wait, self.assertRaises(GitHubError):
            self.loop.tick()
        write.assert_called_once()
        wait.assert_not_called()
