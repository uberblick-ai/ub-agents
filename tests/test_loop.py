from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, iso, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root)
        self.github = FakeGitHub(issue(), pr())
        self.loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)

    def test_complete_vertical_slice_observes_durable_outcome_before_release(self):
        def execute(*args, **kwargs):
            self.github.change(1, labels=frozenset())
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.report(lease, "success", "Requirements investigated")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            self.assertTrue(self.loop.tick())
        history = self.loop.coordinator.history(1)
        self.assertEqual(history[0]["state"], "released")
        self.assertEqual(history[0]["result"], "success")
        self.assertTrue(history[1]["accepted"])
        self.assertTrue((self.root / ".ub-agent" / "runs" / history[0]["run"] / "events.jsonl").is_file())

    def test_exit_zero_without_outcome_is_retry_not_completion(self):
        with patch("ub_agents.loop.supervise", return_value=0):
            self.loop.tick()
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual((lease["result"], outcome["status"], outcome["accepted"]), ("retry", "retry", False))

    def test_nonzero_without_explicit_retry_stops_for_operator_attention(self):
        with patch("ub_agents.loop.supervise", return_value=1):
            self.loop.tick()
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "blocked")
        self.assertEqual(self.loop.plans()[0].state, "blocked")

    def test_loss_of_ownership_causes_no_release_report_or_acceptance_writes(self):
        def lose(*args, **kwargs):
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.update(lease, state="released", expires=iso(timestamp()), result="blocked")
            self.last_writes = list(self.github.writes)
            raise LostOwnership("Owner replaced")
        with patch("ub_agents.loop.supervise", side_effect=lose), self.assertRaises(LostOwnership):
            self.loop.tick()
        self.assertEqual(self.github.writes, self.last_writes)

    def test_stale_independent_review_cannot_be_accepted(self):
        reviewer = replace(self.agent, different_from="builder", kind="pr")
        candidate = self.github.item(2)
        plan = self.loop.coordinator.plan(candidate, self.agent, ())
        lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        outcome = self.loop.coordinator.report(lease, "success", "Reviewed candidate")
        self.github.change(2, labels=frozenset({"ready-to-merge"}), head="b" * 40)
        with self.assertRaises(AgentError):
            self.loop.validate_success(replace(plan, agent=reviewer), outcome)

    def test_success_without_label_handoff_is_not_accepted(self):
        def execute(*args, **kwargs):
            self.loop.coordinator.report(self.loop.coordinator.history(1)[0], "success", "Done")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            self.loop.tick()
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "retry")
        self.assertFalse(self.loop.coordinator.history(1)[1]["accepted"])

    def test_unreadable_github_is_not_an_empty_queue(self):
        self.github.unreadable = True
        with self.assertRaises(AgentError):
            self.loop.tick()
