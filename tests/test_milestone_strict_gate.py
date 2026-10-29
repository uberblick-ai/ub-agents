from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main, status_rows
from ub_agents.config import Priority, Queue
from ub_agents.errors import AgentError
from ub_agents.github import Dependency
from ub_agents.loop import Loop
from ub_agents.records import iso, timestamp
from ub_agents.view_data import plan_group
from tests.support import PollGitHub, agent, config, issue, pr, stub_refresh


class StrictMilestoneGateTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.github = PollGitHub()
        self.github.milestones = [dict(number=20, title="v0.5.10", state="open", created_at=iso(1)),
                                  dict(number=10, title="v0.5.9 — Minor improvements",
                                       state="open", created_at=iso(2))]
        self.queue = Queue("strict-gate", Priority(("high", "medium", "low"), "medium"))
        self.loop = self.make_loop()

    def make_loop(self):
        return Loop(config(self.root, self.worker, queue=self.queue), self.github, "operator", output=lambda *_: None)

    def add(self, *items):
        self.github.items.update({item.number: item for item in items})

    def ready(self):
        return [p.item.number for p in self.loop.plans() if p.state == "ready"]

    def choice(self):
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.loop.tick()
        return execute.call_args.args[0].item.number if execute.called else None

    def status(self, *args):
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status", *args]), 0)
        return output.getvalue()

    def test_empty_first_milestone_waits_and_status_names_it_until_closed(self):
        self.add(issue(1, milestone=20))
        self.assertEqual(self.ready(), [])
        self.assertIsNone(self.choice())
        expected = "Waiting for milestone #10 (v0.5.9 — Minor improvements) to close: no eligible issues"
        self.assertEqual(self.loop.milestone_wait, expected)
        self.assertEqual(self.loop.milestone_skips, ())
        row = status_rows(self.loop)[0]
        self.assertEqual((row["state"], row["reason"]), ("parked", "Waiting for active milestone #10"))
        self.assertNotEqual(plan_group(row), "Eligible")
        text = self.status()
        self.assertIn(expected, text)
        self.assertIn("milestone #20", text)
        data = json.loads(self.status("--json"))
        self.assertEqual(data["milestone_wait"], expected)
        self.assertNotIn("milestone_skips", data)
        self.github.milestones[1]["state"] = "closed"
        self.assertEqual(self.choice(), 1)
        self.assertEqual(self.ready(), [1])
        self.assertIsNone(self.loop.milestone_wait)
        self.assertNotIn("milestone_wait", json.loads(self.status("--json")))

    def test_wait_is_shown_even_without_any_configured_candidates(self):
        self.assertEqual(self.ready(), [])
        self.assertIn("Waiting for milestone #10 (v0.5.9 — Minor improvements)", self.status())
        self.assertIn("milestone_wait", json.loads(self.status("--json")))
        self.add(issue(1, (), milestone=10))
        self.assertEqual(self.ready(), [])
        self.assertIn("milestone_wait", json.loads(self.status("--json")))

    def test_release_prep_waits_before_and_after_its_last_pr_merges(self):
        self.add(issue(1, ("ready", "needs-human"), milestone=10), issue(2, milestone=20),
                 pr(3, body="Closes #1", milestone=10))
        self.assertEqual(self.ready(), [3])
        self.assertEqual(self.choice(), 3)
        self.assertIn("milestone_wait", json.loads(self.status("--json")))
        self.github.change(1, state="closed")
        self.github.change(3, state="closed")
        self.assertEqual(self.ready(), [])
        self.assertIsNone(self.choice())
        self.assertIn("milestone_wait", json.loads(self.status("--json")))
        self.github.milestones[1]["state"] = "closed"
        self.assertEqual(self.choice(), 2)

    def test_higher_priority_closed_later_and_unmilestoned_issues_are_parked(self):
        self.github.milestones.append(dict(number=5, title="v0.1", state="closed", created_at=iso(1)))
        self.add(issue(1, ("ready", "low"), milestone=10), issue(2, ("ready", "high"), milestone=20),
                 issue(3, ("ready", "high"), milestone=5), issue(4, ("ready", "high")),
                 issue(5, ("ready", "medium"), milestone=10))
        self.assertEqual(self.ready(), [5, 1])
        self.assertEqual(self.choice(), 5)
        rows = {row["number"]: row for row in status_rows(self.loop)}
        for number in (2, 3, 4):
            self.assertEqual((rows[number]["state"], rows[number]["reason"]),
                             ("parked", "Waiting for active milestone #10"))
        self.assertNotIn("milestone_wait", json.loads(self.status("--json")))

    def test_no_open_milestone_runs_in_priority_order(self):
        for milestone in self.github.milestones:
            milestone["state"] = "closed"
        self.add(issue(1, ("ready", "low"), milestone=10), issue(2, ("ready", "high")),
                 issue(3, ("ready", "medium"), milestone=20))
        self.assertEqual(self.ready(), [2, 3, 1])
        self.assertEqual(self.choice(), 2)
        self.assertNotIn("milestone_wait", json.loads(self.status("--json")))

    def test_blockers_inherit_current_milestone_transitively_without_priority_labels(self):
        self.queue = Queue("strict-gate", Priority())
        self.loop = self.make_loop()
        self.add(issue(1), issue(2, milestone=20), issue(3, (), milestone=10), issue(4, milestone=20))
        self.github.dependencies = {3: [2], 2: [1]}
        self.assertEqual(self.ready(), [1])
        plan = next(p for p in self.loop.plans() if p.state == "ready")
        self.assertEqual((plan.milestone, plan.milestone_source), (10, 3))
        self.assertEqual(self.choice(), 1)
        self.github.change(1, state="closed")
        self.assertEqual(self.choice(), 2)

    def test_dependency_ignore_does_not_inherit_current_milestone(self):
        self.queue = Queue("strict-gate", dependencies="ignore")
        self.loop = self.make_loop()
        self.add(issue(1), issue(2, (), milestone=10))
        self.github.dependencies = {2: [1]}
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("no dependency read")):
            self.assertEqual(self.ready(), [])
            self.assertIsNone(self.choice())

    def test_current_milestone_with_only_blocked_or_approval_parked_work_waits(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20))
        for reason in ("blocker", "approval", "human"):
            with self.subTest(reason=reason):
                self.github.dependencies = ({1: [Dependency("elsewhere/project", 8, "open")]}
                                            if reason == "blocker" else {})
                self.github.timelines[1] = [] if reason == "approval" else None
                self.github.change(1, labels=frozenset({"ready", "needs-human"} if reason == "human" else {"ready"}))
                self.loop = self.make_loop()
                self.assertEqual(self.ready(), [])
                with patch.object(self.loop, "park_approval"):
                    self.assertIsNone(self.choice())
                self.assertIsNotNone(self.loop.milestone_wait)
                self.assertEqual(self.loop.milestone_skips, ())

    def test_claiming_reads_approval_only_for_current_milestone(self):
        self.add(issue(1, milestone=10), issue(2, milestone=10),
                 issue(3, ("ready", "high"), milestone=20), issue(4, ("ready", "high")))
        self.github.timelines[1] = []
        with patch.object(self.loop, "park_approval"):
            self.assertEqual(self.choice(), 2)
        self.assertEqual([args[0] for name, args in self.github.reads if name == "issue_content"], [1, 2])

    def test_existing_owned_runs_and_recovery_ignore_milestones(self):
        self.add(issue(1, ("ready", "high"), milestone=20), issue(2, ("ready", "low"), milestone=10))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        lease = co.claim(co.plan(self.github.item(1), self.worker, ()))
        co.update(lease, state="running", started=True)
        co.report(lease, "success", "Completed", outcome="done")
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()], [(1, "owned"), (2, "ready")])
        now += 61
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()], [(1, "recover"), (2, "ready")])

    def test_unreadable_milestones_stop_selection(self):
        self.add(issue(1, milestone=10), pr(2, body="Unrelated"))
        with patch.object(self.github, "open_milestones", side_effect=AgentError("Unreadable milestones")), \
                patch.object(self.loop, "execute") as execute:
            with self.assertRaisesRegex(AgentError, "Unreadable milestones"):
                self.loop.tick()
            with self.assertRaisesRegex(AgentError, "Unreadable milestones"):
                self.loop.plans()
        execute.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_named_launch_and_status_bypass_milestone_selection(self):
        self.add(issue(1, milestone=20))
        self.ready()
        with patch.object(self.github, "open_milestones", side_effect=AssertionError("no milestone read")), \
                patch.object(self.github, "milestone_order", side_effect=AssertionError("no milestone read")):
            self.assertNotIn("milestone_wait", json.loads(self.status("1", "--json")))
            item, plans = self.loop.item_plans(1)
            self.assertEqual(list(plans)[0].state, "ready")
            self.assertIsNone(self.loop.milestone_wait)
            with patch.object(self.loop, "execute", return_value=True) as execute:
                self.assertTrue(self.loop.tick_item(1))
            self.assertEqual(execute.call_args.args[0].item, item)
