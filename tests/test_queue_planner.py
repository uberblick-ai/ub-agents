from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from tests.support import FakeGitHub, agent, config, issue


class QueuePlannerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = agent(self.root, name="first")
        self.second = agent(self.root, name="second")
        self.github = FakeGitHub(issue())
        self.loop = Loop(config(self.root, self.first, self.second), self.github,
                         "operator", output=lambda _: None)
        self.loop.coordinator.clock = lambda: 1000

    def test_input_errors_follow_live_target_and_claim_state(self):
        error = GitHubError("GET", "issue content", "Forbidden")
        with patch.object(self.github, "issue_content", side_effect=error):
            for targeted, claiming in ((False, False), (True, False), (False, True), (False, False)):
                with self.subTest(targeted=targeted, claiming=claiming):
                    self.loop._targeted_pass = targeted
                    self.loop._poll_complete = claiming
                    if targeted or claiming:
                        with self.assertRaises(GitHubError) as raised:
                            self.loop.input_check(self.github.items[1])
                        self.assertIs(raised.exception, error)
                    else:
                        approval = self.loop.input_check(self.github.items[1])
                        self.assertFalse(approval.allowed)
                        self.assertIn("unreadable", approval.reason)
        self.assertEqual(self.github.writes, [])

    def test_reconciliation_follows_each_yield_and_only_normal_exhaustion(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust):
                events = []
                with patch.object(self.loop, "before_claim", side_effect=lambda: events.append("boundary")), \
                        patch.object(self.loop, "reconcile_blocked_notices",
                                     side_effect=lambda *args: events.append("reconcile")):
                    _, plans = self.loop.item_plans(1, reconcile_notices=True)
                    self.assertEqual(events, [])
                    self.assertEqual(next(plans).agent, self.first)
                    self.assertEqual(events, ["boundary", "reconcile"])
                    if exhaust:
                        self.assertEqual(next(plans).agent, self.second)
                        self.assertEqual(events, ["boundary", "reconcile"] * 2)
                        with self.assertRaises(StopIteration):
                            next(plans)
                        self.assertEqual(events, ["boundary", "reconcile"] * 3)
                    else:
                        plans.close()
                        self.assertEqual(events, ["boundary", "reconcile"])
        self.assertEqual(self.github.writes, [])

    def test_closed_item_without_rows_reconciles_on_exhaustion(self):
        coordinator = self.loop.coordinator
        lease = coordinator.claim(coordinator.plan(self.github.items[1], self.first, ()))
        coordinator.update(lease, state="running", started=True)
        coordinator.report(lease, "blocked", "Needs a decision", action=["Maintainer: decide the approach."])
        coordinator.release(lease, "blocked", "Needs a decision")
        self.github.change(1, state="closed", labels=frozenset())
        self.github.writes.clear()
        with patch.object(self.loop, "reconcile_blocked_notices") as reconcile:
            _, plans = self.loop.item_plans(1, reconcile_notices=True)
            reconcile.assert_not_called()
            self.assertEqual(list(plans), [])
        reconcile.assert_called_once()
        self.assertEqual({record["agent"] for record in reconcile.call_args.args[0]}, {self.first.name})
        self.assertEqual(self.github.writes, [])

    def test_claiming_replan_does_not_enable_notice_reconciliation(self):
        with patch.object(self.loop, "reconcile_blocked_notices") as reconcile:
            plans = list(self.loop.iter_plans(claiming=True))
        self.assertEqual([(plan.agent, plan.state) for plan in plans],
                         [(self.first, "ready"), (self.second, "ready")])
        reconcile.assert_not_called()
        self.assertEqual(self.github.writes, [])
