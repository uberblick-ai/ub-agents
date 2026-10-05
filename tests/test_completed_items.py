"""Completed items retain obligations, not stale requests to restore a trigger."""

import json
from pathlib import Path
import tempfile
import unittest

from ub_agents.cli import status_rows
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import body, iso, payload, timestamp
from tests.support import FakeGitHub, MemoryPublisher, agent, config, edit_lease, issue, pr, stub_refresh


class CompletedItemTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.agent = agent(self.root)
        self.cfg = config(self.root, self.agent)
        self.now = timestamp()

    def setup_run(self, item):
        github = FakeGitHub(item)
        loop = Loop(self.cfg, github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        lease = loop.coordinator.claim(loop.plans()[0])
        return github, loop, lease

    def check_plans(self, loop, number):
        targeted = list(loop.item_plans(number)[1])
        queued = loop.plans()
        rows = status_rows(loop, self.now)
        self.assertEqual([(p.state, p.reason) for p in targeted],
                         [(p.state, p.reason) for p in queued])
        self.assertEqual([(p.state, p.reason) for p in targeted],
                         [(r["state"], r["reason"]) for r in rows])
        return targeted

    def test_released_retry_and_blocked_need_a_trigger_only_on_open_items(self):
        for item in (issue(), pr()):
            for state in (("open", "closed") if item.kind == "issue" else ("open", "closed", "merged")):
                for result in ("retry", "blocked"):
                    for labels in ((), tuple(item.labels), ("needs-human",)):
                        with self.subTest(kind=item.kind, state=state, result=result, labels=labels):
                            github, loop, lease = self.setup_run(item)
                            edit_lease(github, lease, state="released", result=result,
                                       expires=iso(self.now), attempt_effect="unchanged", summary="Interrupted")
                            github.change(item.number, state=state, labels=frozenset(labels))
                            before = json.dumps(github.store, sort_keys=True), list(github.writes)
                            plans = self.check_plans(loop, item.number)
                            if state != "open":
                                self.assertEqual(plans, [])
                                self.assertFalse(loop.tick())
                            else:
                                self.assertEqual(len(plans), 1)
                                if not item.labels.intersection(labels):
                                    self.assertEqual(plans[0].state, "blocked")
                                    self.assertIn("restore a trigger", plans[0].reason)
                            self.assertEqual((json.dumps(github.store, sort_keys=True), github.writes), before)
                            self.assertEqual(github.item(item.number).labels, frozenset(labels))

    def test_closed_items_keep_live_and_expired_unfinished_leases(self):
        for item in (issue(), pr()):
            for state in ("claiming", "running"):
                for expired in (False, True):
                    with self.subTest(kind=item.kind, state=state, expired=expired):
                        github, loop, lease = self.setup_run(item)
                        edit_lease(github, lease, state=state)
                        if expired:
                            self.now += 61
                        github.change(item.number, state="closed", labels=frozenset())
                        plan, = self.check_plans(loop, item.number)
                        self.assertEqual(plan.state, "blocked" if expired else "owned")
                        self.assertIn("Expired run has no outcome" if expired else "unexpired assignment", plan.reason)

    def test_closed_items_keep_pending_outcomes_and_started_transitions(self):
        for item in (issue(), pr()):
            for started in (False, True):
                with self.subTest(kind=item.kind, started=started):
                    github, loop, lease = self.setup_run(item)
                    outcome = loop.coordinator.report(lease, "success", "Work done", outcome="done")
                    if started:
                        loop.coordinator.update_outcome(lease, outcome,
                                                        transition=outcome["transition"] | {"started": True})
                    self.now += 61
                    github.change(item.number, state="closed", labels=frozenset())
                    plan, = self.check_plans(loop, item.number)
                    self.assertEqual(plan.state, "recover")
                    self.assertIn("explicit outcome to validate", plan.reason)

    def test_closed_items_keep_unconfirmed_cleanup(self):
        for item in (issue(), pr()):
            for result in (None, "retry", "blocked"):
                with self.subTest(kind=item.kind, result=result):
                    github, loop, lease = self.setup_run(item)
                    edit_lease(github, lease, state="released" if result else "running",
                               result=result, cleanup="unconfirmed", expires=iso(self.now),
                               attempt_effect="failure" if result else "pending")
                    github.change(item.number, state="closed", labels=frozenset())
                    plan, = self.check_plans(loop, item.number)
                    self.assertEqual(plan.state, "blocked")
                    self.assertIn("Previous cleanup was unconfirmed", plan.reason)

    def test_closed_items_keep_unreadable_or_conflicting_coordination(self):
        for item in (issue(), pr()):
            for malformed in (False, True):
                with self.subTest(kind=item.kind, malformed=malformed):
                    github, loop, lease = self.setup_run(item)
                    if malformed:
                        github.update_comment(lease["id"], body(payload(lease) | {"state": "invalid"}))
                    else:
                        outcome = loop.coordinator.report(lease, "success", "Work done", outcome="done")
                        github.create_comment(item.number, body(payload(outcome)))
                    self.now += 61
                    github.change(item.number, state="closed", labels=frozenset())
                    plan, = self.check_plans(loop, item.number)
                    self.assertEqual(plan.state, "blocked")
                    self.assertIn("Malformed ub-agents comment" if malformed else "conflicting outcomes", plan.reason)

    def test_completed_pass_removes_stale_row_without_recording_activity(self):
        github, loop, lease = self.setup_run(issue())
        edit_lease(github, lease, state="released", result="retry", expires=iso(self.now),
                   attempt_effect="unchanged", summary="Interrupted")
        github.change(1, labels=frozenset())
        memory = MemoryPublisher()
        observer = Observations(self.cfg, "operator", None, memory)
        loop.observer = observer
        self.assertFalse(loop.tick())
        self.assertEqual(memory.snapshots[-1]["latest_pass"]["rows"][0]["state"], "blocked")
        before = list(github.writes)
        github.change(1, state="closed")
        self.assertFalse(loop.tick())
        self.assertEqual(memory.snapshots[-1]["latest_pass"],
                         {"started_at": observer.state["latest_pass"]["started_at"], "state": "complete", "rows": []})
        self.assertEqual(memory.snapshots[-1]["outcomes"], [])
        self.assertEqual(github.writes, before)
