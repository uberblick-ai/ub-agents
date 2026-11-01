"""Assignment settlement retains the coordinator's write-invalidated reads."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.loop import Loop
from ub_agents.settlement import Settlement
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class SettlementTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_execution_conflict_and_recovery_share_the_coordinators_reads(self):
        for mode in ("execution", "conflict", "recovery"):
            with self.subTest(mode=mode):
                role = agent(self.root, on_conflict="done" if mode == "conflict" else None)
                candidate = replace(pr(labels=("ready",)), mergeable=False) if mode == "conflict" else issue()
                github = FakeGitHub(candidate)
                loop = Loop(config(self.root, role), github, "operator", output=lambda *_: None)
                now = 1000
                loop.coordinator.clock = lambda: now
                if mode == "recovery":
                    lease = loop.coordinator.claim(loop.plans()[0])
                    loop.coordinator.update(lease, state="running", started=True)
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    now += 61
                instances = []

                def create(*args):
                    component = Settlement(*args)
                    instances.append(component)
                    self.assertIs(component.config, loop.config)
                    self.assertIs(component.github, loop.github)
                    self.assertIs(component.github, loop.coordinator.github)
                    self.assertIs(component.coordinator, loop.coordinator)
                    self.assertEqual(component.approvals, loop.approvals)
                    return component

                def execute(*args, **kwargs):
                    self.assertEqual(mode, "execution")
                    lease = loop.coordinator.history(candidate.number)[0]
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    return 0

                with patch("ub_agents.assignment_run.Settlement" if mode == "execution" else "ub_agents.loop.Settlement",
                           side_effect=create), \
                        patch("ub_agents.assignment_run.supervise", side_effect=execute):
                    self.assertTrue(loop.tick())
                self.assertEqual(len(instances), 1)
                history = loop.coordinator.history(candidate.number)
                self.assertTrue(next(record for record in history if record["kind"] == "outcome")["accepted"])
                self.assertEqual([record for record in history if record["kind"] == "lease"][-1]["result"], "success")
                self.assertIsNone(loop.github._finalization)
