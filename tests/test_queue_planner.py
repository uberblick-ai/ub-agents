from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ub_agents.cli.status import status_rows
from ub_agents.coordination import Coordinator
from ub_agents.discovery import Discovery
from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import iso
from ub_agents.view.data import Session, work_rows
from tests.support import FakeGitHub, MemoryPublisher, agent, config, edit_lease, issue


class MovedOnBlockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def blocked_item(self):
        self.roles = tuple(agent(self.root, name=name, triggers=(name,))
                           for name in ("preparer", "reviewer", "implementer"))
        self.github = FakeGitHub(issue(labels=("preparer",)))
        cfg = config(self.root, *self.roles)
        self.publisher = MemoryPublisher()
        self.observer = Observations(cfg, "operator", None, self.publisher)
        self.loop = Loop(cfg, self.github, "operator", output=lambda _: None)
        self.now = 950
        self.loop.coordinator.clock = lambda: self.now
        self.block = self.claim(self.roles[0])
        self.now = 1000
        self.loop.coordinator.release(self.block, "blocked", "Preparation stopped")
        self.github.change(1, labels=frozenset({"implementer"}))
        self.now = 1020

    def claim(self, role):
        self.github.change(1, labels=frozenset(role.triggers))
        lease = self.loop.coordinator.claim(self.loop.coordinator.plan(self.github.items[1], role, ()))
        self.loop.coordinator.update(lease, state="running", started=True)
        return lease

    def progress(self, signal, **changes):
        lease = self.claim(self.roles[1] if signal == "outcome" else self.roles[2])
        if signal == "outcome":
            outcome = self.loop.coordinator.report(lease, "success", "Review approved", outcome="done")
            self.loop.coordinator.accept(lease, outcome)
            self.loop.coordinator.update_outcome(lease, outcome, **({"transition_complete": True} | changes))
            self.loop.coordinator.release(lease, "success", "Review approved")
        else:
            edit_lease(self.github, lease, **changes)
        self.github.change(1, labels=frozenset({"implementer"}))
        self.now += 10
        return lease

    def assert_blocked(self, reason):
        self.loop.observer = self.observer
        self.observer.begin_pass()
        plans = self.loop.plans()
        plan = next(plan for plan in plans if plan.agent == self.roles[0])
        self.assertEqual(plan.state, "blocked")
        self.assertIn(reason, plan.reason)
        snapshot = self.publisher.snapshots[-1]
        self.assertEqual([(row['agent'], row['state']) for row in snapshot['latest_pass']['rows']
                          if row['agent'] == 'preparer'], [('preparer', 'blocked')])
        rows = work_rows(Session(self.root / "session.json", snapshot), self.root)
        eligible = any(plan.state in {'ready', 'recover', 'backoff', 'waiting'} for plan in plans)
        self.assertEqual([(row.item, row.group) for row in rows],
                         [(1, 'Eligible' if eligible else 'Needs attention')])
        if not eligible:
            self.assertIn('preparer', [part.agent for part in rows[0].attention_rows])

    def test_completed_review_then_live_implementation_clears_preparer_in_status_and_view(self):
        self.blocked_item()
        self.progress("outcome")
        self.progress("lease")
        self.loop.observer = self.observer
        self.observer.begin_pass()
        rows = status_rows(self.loop, self.now)
        self.assertEqual([(row["agent"], row["state"]) for row in rows], [("implementer", "owned")])
        rows = work_rows(Session(self.root / "session.json", self.publisher.snapshots[-1]), self.root)
        self.assertFalse(any(row.group == "Needs attention" for row in rows))

    def test_each_later_progress_signal_clears_the_block(self):
        for signal, changes in (("lease", {}), ("lease", {"state": "claiming"}), ("outcome", {})):
            with self.subTest(signal=signal, changes=changes):
                self.blocked_item()
                self.progress(signal, **changes)
                self.assertNotIn("preparer", [row["agent"] for row in status_rows(self.loop, self.now)])

    def test_older_or_simultaneous_progress_does_not_clear_a_newer_release(self):
        for signal in ("lease", "outcome"):
            for created in (975, 1000):
                with self.subTest(signal=signal, created=created):
                    self.blocked_item()
                    self.progress(signal, created=iso(created))
                    self.assert_blocked("Last run blocked")

    def test_no_progress_or_unfinished_progress_keeps_the_block(self):
        for changes in (None, {"accepted": False}, {"transition_complete": False},
                        {"rejected": "Transition rejected"}, {"agent": "preparer"}):
            with self.subTest(changes=changes):
                self.blocked_item()
                if changes is not None:
                    self.progress("outcome", **changes)
                self.assert_blocked("Last run blocked")

    def test_another_roles_nonlive_lease_without_an_outcome_does_not_clear_the_block(self):
        for changes in ({"state": "withdrawn"}, {"state": "released"}, {"expires": iso(1025)}):
            with self.subTest(changes=changes):
                self.blocked_item()
                self.progress("lease", **changes)
                self.assert_blocked("Last run blocked")

    def test_cleanup_and_expired_runs_stay_blocked_after_other_roles_move_on(self):
        for changes, reason in (({"cleanup": "unconfirmed"}, "cleanup was unconfirmed"),
                                ({"state": "running", "result": None}, "Expired run has no outcome")):
            with self.subTest(changes=changes):
                self.blocked_item()
                self.progress("outcome")
                self.progress("lease")
                edit_lease(self.github, self.block, **changes)
                self.assert_blocked(reason)

    def test_returning_issue_trigger_still_needs_retry_after_other_role_completed(self):
        self.blocked_item()
        self.progress("outcome")
        self.github.change(1, labels=frozenset({"preparer"}))
        self.assert_blocked("use ub-agents retry")


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

    def test_status_keeps_the_loop_iterator_patch_boundary(self):
        plans = self.loop.queue_planner.plans()
        with patch.object(self.loop, "iter_plans", return_value=iter(plans)) as iterate:
            self.assertEqual(self.loop.plans(), plans)
        iterate.assert_called_once_with(cached=False)

    def test_planner_traverses_and_targets_without_loop_planning_methods(self):
        with patch.object(self.loop, "iter_plans", side_effect=AssertionError("Loop traversal")), \
                patch.object(self.loop, "item_plans", side_effect=AssertionError("Loop targeting")):
            plans = self.loop.queue_planner.plans()
            item, targeted = self.loop.queue_planner.target_plans(1, self.second.name)
            targeted = list(targeted)
        self.assertEqual([(plan.item.number, plan.agent, plan.state) for plan in plans],
                         [(1, self.first, "ready"), (1, self.second, "ready")])
        self.assertEqual(item.number, 1)
        self.assertEqual([(plan.agent, plan.state) for plan in targeted], [(self.second, "ready")])
        self.assertEqual(self.github.writes, [])

    def test_traversal_reads_replaced_discovery_and_pass_stats_when_reached(self):
        self.github.items[2] = issue(2)
        original = self.loop.discovery
        plans = self.loop.iter_plans(claiming=True)
        self.addCleanup(plans.close)
        self.loop.discovery = Discovery(self.loop.github)
        first_stats = SimpleNamespace(candidates=set())
        second_stats = SimpleNamespace(candidates=set())
        self.loop._pass_stats = first_stats
        with patch.object(original, "observe", side_effect=AssertionError("Old discovery")):
            self.assertEqual(next(plans).item.number, 1)
            self.assertEqual(first_stats.candidates, {1})
            self.loop._pass_stats = second_stats
            self.assertEqual(next(plans).item.number, 1)
            self.assertEqual(second_stats.candidates, set())
            self.assertEqual([plan.item.number for plan in plans], [2, 2])
        self.assertEqual(first_stats.candidates, {1})
        self.assertEqual(second_stats.candidates, {2})
        self.assertEqual(self.github.writes, [])

    def test_queue_diagnostics_reset_on_advancement_and_idle_follows_exhaustion(self):
        previous_waits = {2: ("waiting", "Previous pass")}
        self.loop._queue_waits = previous_waits
        self.loop._has_trigger = None
        self.loop.milestone_wait = "Previous milestone"
        self.loop.milestone_skips = ("Previous skip",)
        events = []
        with patch.object(self.loop, "observe", side_effect=lambda *args: events.append(args)):
            plans = self.loop.iter_plans(claiming=True)
            self.addCleanup(plans.close)
            self.loop._launch_agent = self.first.name
            self.assertIs(self.loop._queue_waits, previous_waits)
            self.assertIsNone(self.loop._has_trigger)
            self.assertEqual(self.loop.milestone_wait, "Previous milestone")
            self.assertEqual(self.loop.milestone_skips, ("Previous skip",))
            self.assertEqual(events, [])
            plan = next(plans)
            self.assertEqual(plan.agent, self.first)
            self.assertIsNot(self.loop._queue_waits, previous_waits)
            self.assertEqual(self.loop._queue_waits, {1: ("ready", "Trigger matched")})
            self.assertTrue(self.loop._has_trigger)
            self.assertIsNone(self.loop.milestone_wait)
            self.assertEqual(self.loop.milestone_skips, ())
            self.assertFalse(any(event[0] == "queue_idle" for event in events))
            self.loop._launch_agent = self.second.name
            self.assertEqual(list(plans), [])
        self.assertEqual(events[-1], ("queue_idle", "Agent second: no eligible work; waiting items: 1 ready"))

    def test_targeted_observation_reads_launch_number_after_each_yield(self):
        _, plans = self.loop.item_plans(1)
        self.addCleanup(plans.close)
        self.loop._launch_agent = self.first.name
        self.loop._launch_number = 1
        self.assertEqual(next(plans).agent, self.first)
        self.assertEqual(self.loop._queue_waits, {})
        self.loop._launch_number = None
        self.assertEqual(next(plans).agent, self.second)
        self.assertEqual(self.loop._queue_waits, {1: ("ready", "Trigger matched")})
        self.assertEqual(list(plans), [])

    def test_swapped_coordinator_clock_controls_lease_expiry(self):
        self.loop.coordinator.clock = lambda: 2000
        now = 1000
        coordinator = Coordinator(self.loop.github, "operator", clock=lambda: now)
        self.loop.coordinator = coordinator
        lease = coordinator.claim(coordinator.plan(self.github.items[1], self.first, ()))
        coordinator.update(lease, state="running", started=True)
        self.github.change(1, labels=frozenset())

        plans = self.loop.plans()
        self.assertEqual([(plan.agent, plan.state) for plan in plans], [(self.first, "owned")])
        now += 61
        plans = self.loop.plans()
        self.assertEqual([(plan.agent, plan.state) for plan in plans], [(self.first, "blocked")])
        self.assertIn("no outcome", plans[0].reason)

    def test_approval_resolution_updates_swapped_coordinator_trust(self):
        original = self.loop.coordinator
        self.loop.config = replace(self.loop.config, trusted_pr_authors=("authorbot[bot]",))
        self.loop.coordinator = Coordinator(self.loop.github, "operator", clock=lambda: 1000)

        self.loop.plans()

        self.assertEqual(self.loop.coordinator.trust.trusted_pr_authors, {"authorbot[bot]"})
        self.assertEqual(original.trust.trusted_pr_authors, set())

    def test_approval_resolution_reads_swapped_github_visibility(self):
        self.loop.config = replace(self.loop.config, approvals=None)
        github = FakeGitHub(issue())
        github.repository_visibility = "private"
        self.loop.github = github

        self.loop.plans()

        self.assertEqual(self.loop.approvals, "off")

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
