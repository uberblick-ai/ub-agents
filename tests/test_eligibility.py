from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Queue
from ub_agents.coordination import Plan
from ub_agents.eligibility import AgentMatches, check_start
from ub_agents.loop import Loop
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def loop(self, item, worker, queue, blocked=False):
        github = FakeGitHub(item, issue(31, labels=(), milestone=10))
        github.milestones = [dict(number=10, state="open", created_at=item.created_at)]
        if blocked:
            github.dependencies[item.number] = [31]
        return Loop(config(self.root, worker, queue=queue), github, "operator", output=lambda _: None)

    def test_start_rules_agree_in_discovery_parking_and_claim_and_recovery_skips_them(self):
        gate = Queue(milestones="gate")
        cases = [
            ("closed", replace(issue(), state="closed"), "either", Queue(), False, False, "issue is closed"),
            ("no trigger", issue(labels=()), "either", Queue(), False, False, "No trigger matches"),
            ("wrong kind", issue(), "pr", Queue(), False, False, "Agent worker does not apply to this issue"),
            ("stop", issue(labels=("ready", "needs-human")), "either", Queue(), False, False,
             "Stop label needs-human is present"),
            ("milestone gate", issue(milestone=20), "either", gate, False, False,
             "Waiting for active milestone #10"),
            ("milestone order", issue(milestone=20), "either", Queue(milestones="order"), False, True, None),
            ("milestone ignore", issue(milestone=20), "either", Queue(), False, True, None),
            ("blocker wait", issue(), "either", Queue(), True, False, "Waiting for blockers #31"),
            ("blocker ignore", issue(), "either", Queue(dependencies="ignore"), True, True, None),
            ("combined wait", issue(milestone=20), "either", gate, True, False,
             "Waiting for active milestone #10; Waiting for blockers #31"),
            ("eligible", issue(), "either", Queue(), False, True, None),
            ("PR ignores issue gates", pr(1, body="", milestone=20), "pr", gate, True, True, None),
        ]
        for name, item, kind, queue, blocked, allowed, reason in cases:
            with self.subTest(rule=name):
                worker = agent(self.root, kind=kind)
                loop = self.loop(item, worker, queue, blocked)
                matches = AgentMatches.for_item(item, (worker,))
                start = check_start(item, worker, matches, loop.config.stop_labels, queue,
                                    10, ("#31",) if blocked else ())
                self.assertEqual((start.allowed, start.reason), (allowed, reason))
                repository = [p for p in loop.plans() if p.item.number == item.number]
                scoped = list(loop.item_plans(item.number)[1])
                for path, plans in (("repository", repository), ("single item", scoped)):
                    with self.subTest(path=path):
                        self.assertEqual(any(p.state == "ready" for p in plans), allowed)
                        if plans and not allowed:
                            self.assertEqual((plans[0].state, plans[0].reason), ("parked", reason))

                # Parking needs an approval gate. Remove the maintainer start
                # after discovery, then run its real fresh-input recheck.
                loop.github.timelines[item.number] = []
                if item.kind == "pr":
                    content = loop.github.pr_content
                    loop.github.pr_content = lambda n: content(n) | {"author": {"login": "outsider"}}
                approval = loop.input_check(item)
                self.assertIsNotNone(approval.gate)
                plan = Plan(item, worker, None, "ready", "Trigger matched", 1,
                            approval_gate=approval, matches=matches)
                with patch.object(loop.coordinator.notices, "approval") as notice:
                    loop.park_approval(plan)
                self.assertEqual(notice.called, allowed)
                self.assertEqual(loop.github.writes, [])
                lease = loop.coordinator.claim(plan, loop.config.stop_labels)
                self.assertEqual(lease is not None, allowed)
                if not allowed:
                    self.assertEqual(loop.coordinator.history(item.number), [])
                    self.assertEqual(loop.github.writes, [])

                # Start with a valid lease, then change every input to this
                # rule case. Recovery must claim the pending outcome anyway.
                initial = issue() if item.kind == "issue" else pr(1, body="")
                recovery = self.loop(initial, replace(worker, kind="either"), Queue())
                now = 1000
                recovery.coordinator.clock = lambda: now
                source = recovery.coordinator.claim(recovery.plans()[0])
                recovery.coordinator.update(source, state="running", started=True)
                recovery.coordinator.report(source, "success", "Completed", outcome="done")
                now += 61
                recovery.github.items[item.number] = item
                recovery.github.dependencies[item.number] = [31] if blocked else []
                recovery.coordinator.queue = queue
                recovery_plan = replace(plan, state="recover")
                with patch("ub_agents.coordination.check_start", side_effect=AssertionError("start rules")), \
                        patch.object(recovery.github, "active_milestone", side_effect=AssertionError("milestone")), \
                        patch.object(recovery.github, "blocked_by", side_effect=AssertionError("blockers")):
                    lease = recovery.coordinator.claim(recovery_plan, recovery.config.stop_labels, recovery=True)
                self.assertIsNotNone(lease)
                self.assertEqual(lease["mode"], "recovery")

    def test_matches_and_label_unions_are_shared_without_changing_prompt_labels(self):
        workers = (agent(self.root, name="issue-worker", kind="issue", triggers=("issue-start",)),
                   agent(self.root, name="pr-worker", kind="pr", triggers=("pr-start",),
                         outcomes={"done": {"add": ("review",), "remove": ("old",)}}),
                   agent(self.root, name="either-worker", triggers=("shared-start",)))
        item = issue(labels=("issue-start", "shared-start"))
        github = FakeGitHub(item)
        loop = Loop(config(self.root, *workers), github, "operator", output=lambda _: None)
        derive = AgentMatches.for_item
        for discover in (loop.plans, lambda: list(loop.item_plans(1, "issue-worker")[1])):
            with self.subTest(discovery=discover):
                with patch.object(AgentMatches, "for_item", wraps=derive) as matches:
                    plans = discover()
                matches.assert_called_once()
                self.assertEqual(plans[0].matches.matched, (workers[0], workers[2]))
                self.assertTrue(all(p.matches is plans[0].matches for p in plans))
                self.assertEqual(plans[0].matches.trigger_labels, frozenset({"issue-start", "shared-start"}))
                prompt = loop.prompt_for(plans[0], {"outcomes": {}}, {"earlier_branches": []}, "")
                self.assertIn('["issue-start", "needs-human", "old", "pr-start", "review", "shared-start"]', prompt)
