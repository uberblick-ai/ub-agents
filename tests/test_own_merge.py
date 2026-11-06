"""Run-owned merges survive renewal; unfinished work survives launcher failure."""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import LEASE_RENEW_SECONDS, LEASE_SECONDS
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.execution import Workspace, group_members, supervise
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import seconds, timestamp
from ub_agents.settlement import Settlement
from tests.support import PollGitHub, agent, config, pr, stub_refresh
from tests.test_renewal import ManualRenewal


class OwnMergeTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.now = 1000
        self.github = PollGitHub(pr(1))
        self.role = agent(self.root, lease_seconds=LEASE_SECONDS, timeout_seconds=7200,
                          outcomes={"merged": {"add": ("completed",), "remove": ()}})
        self.cfg = config(self.root, self.role)
        self.loop = self.restart()
        self.co = self.loop.coordinator
        replacement = patch("ub_agents.loop.LeaseRenewal", ManualRenewal)
        replacement.start()
        self.addCleanup(replacement.stop)

    def restart(self):
        loop = Loop(self.cfg, self.github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        return loop

    def advance(self):
        self.now += LEASE_RENEW_SECONDS
        self.loop._renewal.tick()

    def merge(self, **changes):
        self.github.change(1, state="closed", merged=True, merged_by="OPERATOR", **changes)

    def notices(self):
        return [c for c in self.github.comments(1) if c["body"].startswith(ACTION_MARKER)]

    def test_own_merge_at_assigned_head_or_base_merges_keeps_working_then_settles(self):
        assigned = self.github.item(1).head
        for head, parents in ((assigned, {}), ("b" * 40, {"b" * 40: [assigned, "base"]}),
                              ("c" * 40, {"c" * 40: ["b" * 40, "base2"], "b" * 40: [assigned, "base"]})):
            with self.subTest(head=head):
                self.setUp()

                def run(*args, **kwargs):
                    lease = self.loop._renewal.lease
                    self.merge(head=head)
                    self.advance()
                    self.assertEqual(kwargs["expires"](), self.now + LEASE_SECONDS)
                    stored, = self.co.history(1)
                    self.assertEqual(stored["own_merge_head"], head)
                    self.assertTrue(stored["action_needed"])
                    self.assertNotIn("result", stored)
                    self.advance()
                    kwargs["expires"]()
                    self.co.report(lease, "success", "Post-merge work completed", outcome="merged")
                    return 0

                with patch.object(self.github, "commit_parents", side_effect=lambda sha: parents.get(sha, [])), \
                        patch("ub_agents.assignment_run.supervise", side_effect=run):
                    self.assertTrue(self.loop.tick())
                lease, outcome = self.co.history(1)
                self.assertEqual((lease["state"], lease["result"]), ("released", "success"))
                self.assertTrue(outcome["accepted"])
                self.assertTrue(outcome["transition_complete"])
                self.assertEqual(self.github.item(1).labels, frozenset({"completed"}))
                self.assertEqual(self.notices(), [])

    def test_other_account_or_unattributable_head_still_stops_without_notice(self):
        assigned = self.github.item(1).head
        cases = (("other", assigned, []), ("operator", "b" * 40, [assigned]),
                 ("operator", "b" * 40, [assigned, "base", "third"]),
                 ("operator", "b" * 40, ["unrelated", assigned]))
        for actor, head, parents in cases:
            with self.subTest(actor=actor, head=head, parents=parents):
                self.setUp()

                def run(*args, **kwargs):
                    self.github.change(1, state="closed", merged=True, merged_by=actor, head=head)
                    self.advance()
                    kwargs["expires"]()
                    self.fail("An external merge must stop execution")

                with patch.object(self.github, "commit_parents", side_effect=lambda sha: parents if sha == head else []), \
                        patch("ub_agents.assignment_run.supervise", side_effect=run):
                    self.assertTrue(self.loop.tick())
                lease, = self.co.history(1)
                self.assertEqual((lease["result"], lease["action_needed"]), ("blocked", False))
                self.assertNotIn("own_merge_head", lease)
                self.assertEqual(self.notices(), [])

    def test_stop_label_after_own_merge_discards_early_report(self):
        def run(*args, **kwargs):
            self.merge()
            self.advance()
            self.co.report(self.loop._renewal.lease, "success", "Early report", outcome="merged")
            self.github.change(1, labels=frozenset({"ready", "needs-human"}))
            self.advance()
            kwargs["expires"]()

        with patch("ub_agents.assignment_run.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        lease, outcome = self.co.history(1)
        self.assertEqual(lease["result"], "blocked")
        self.assertTrue(lease["unreported"])
        self.assertFalse(outcome["accepted"])
        self.assertFalse(outcome["transition"]["started"])
        self.assertEqual(self.github.item(1).labels, frozenset({"ready", "needs-human"}))

    def test_failed_own_merge_records_notice_before_cleanup_and_keeps_it_after_restart(self):
        for failure in (None, AgentError("Agent command timed out after 7200s"), RuntimeError("Agent crashed")):
            for renew in (False, True):
                with self.subTest(failure=failure, renew=renew):
                    self.setUp()

                    def run(*args, **kwargs):
                        self.merge()
                        if renew:
                            self.advance()
                        if failure:
                            raise failure
                        return 1

                    cleanup = Workspace.cleanup

                    def check_cleanup(workspace, *args, **kwargs):
                        stored = self.co.history(1)[0]
                        self.assertIn("own_merge_head", stored)
                        self.assertTrue(stored["action_needed"])
                        return cleanup(workspace, *args, **kwargs)

                    with patch("ub_agents.assignment_run.supervise", side_effect=run), \
                            patch.object(Workspace, "cleanup", check_cleanup):
                        self.assertTrue(self.loop.tick())
                    lease = self.co.history(1)[0]
                    self.assertEqual((lease["state"], lease["result"]), ("released", "blocked"))
                    self.assertIn("Post-merge work may be unfinished", lease["summary"])
                    self.assertEqual(len(self.notices()), 1)
                    self.assertIn("Post-merge work may be unfinished", self.notices()[0]["body"])
                    self.assertNotIn("nothing else is needed", self.notices()[0]["body"])
                    restarted = self.restart()
                    with patch("ub_agents.assignment_run.supervise") as execution:
                        self.assertFalse(restarted.tick())
                    execution.assert_not_called()
                    self.assertEqual(len(self.notices()), 1)

    def test_restart_recovers_own_merge_with_or_without_report_or_prior_renewal(self):
        for report in (False, True):
            for renew in (False, True):
                with self.subTest(report=report, renew=renew):
                    self.setUp()

                    def run(*args, **kwargs):
                        self.merge()
                        if renew:
                            self.advance()
                        if report:
                            self.co.report(self.loop._renewal.lease, "success", "Work completed", outcome="merged")
                        raise LostOwnership("Launcher lost ownership")

                    with patch("ub_agents.assignment_run.supervise", side_effect=run), self.assertRaises(LostOwnership):
                        self.loop.tick()
                    source = self.co.history(1)[0]
                    self.now = seconds(source["expires"])
                    restarted = self.restart()
                    with patch("ub_agents.assignment_run.supervise") as execution:
                        self.assertTrue(restarted.tick())
                    execution.assert_not_called()
                    history = restarted.coordinator.history(1)
                    recovery = next(r for r in history if r.get("mode") == "recovery")
                    self.assertEqual((recovery["state"], recovery["result"]),
                                     ("released", "success" if report else "blocked"))
                    self.assertEqual(len(self.notices()), 0 if report else 1)
                    if report:
                        self.assertTrue(history[1]["accepted"])
                        self.assertIn("completed", self.github.item(1).labels)

    def test_real_timeout_after_own_merge_terminates_process_group(self):
        self.now = timestamp()
        self.loop.config = replace(self.cfg, agents=(replace(
            self.role, timeout_seconds=1, command=(sys.executable, "-c", "import time; time.sleep(30)")),))

        def run(*args, **kwargs):
            def merge(final=False):
                if not final and not self.github.item(1).merged:
                    self.merge()
                    self.advance()
            return supervise(*args, **(kwargs | {"observe_output": merge}))

        with patch("ub_agents.assignment_run.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        lease = self.co.history(1)[0]
        directory = self.root / ".ub-agents/runs" / lease["run"]
        self.assertEqual(group_members(int((directory / "pid").read_text())), [])
        self.assertEqual((lease["state"], lease["result"]), ("released", "blocked"))
        self.assertIn("timed out", lease["summary"])
        self.assertEqual(len(self.notices()), 1)

    def test_early_report_cannot_hide_timeout_or_rejection_or_interrupt(self):
        for failure in ("timeout", "rejected", "interrupt"):
            with self.subTest(failure=failure):
                self.setUp()

                def run(*args, **kwargs):
                    self.merge()
                    self.advance()
                    lease = self.loop._renewal.lease
                    outcome = self.co.report(lease, "success", "Early report", outcome="merged")
                    if failure == "timeout":
                        raise AgentError("Agent timed out")
                    if failure == "interrupt":
                        raise KeyboardInterrupt
                    self.co.update_outcome(lease, outcome, candidate_sha="wrong-head")
                    return 0

                with patch("ub_agents.assignment_run.supervise", side_effect=run):
                    if failure == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            self.loop.tick()
                    else:
                        self.assertTrue(self.loop.tick())
                lease, outcome = self.co.history(1)
                self.assertEqual((lease["state"], lease["result"]), ("released", "blocked"))
                self.assertFalse(outcome["accepted"])
                self.assertNotIn("completed", self.github.item(1).labels)
                self.assertEqual(len(self.notices()), 1)

    def test_interrupted_cleanup_keeps_failed_verdict_and_rebuilds_missing_notice(self):
        def run(*args, **kwargs):
            self.merge()
            self.advance()
            self.co.report(self.loop._renewal.lease, "success", "Early report", outcome="merged")
            raise AgentError("Agent timed out")

        def interrupted_cleanup(*args, **kwargs):
            stored = self.co.history(1)[0]
            self.assertEqual(stored["result"], "blocked")
            self.assertTrue(stored["action_needed"])
            self.assertIn("Post-merge work may be unfinished", stored["summary"])
            raise KeyboardInterrupt

        with patch("ub_agents.assignment_run.supervise", side_effect=run), \
                patch.object(Workspace, "cleanup", side_effect=interrupted_cleanup), self.assertRaises(KeyboardInterrupt):
            self.loop.tick()
        self.now = seconds(self.co.history(1)[0]["expires"])
        restarted = self.restart()
        with patch("ub_agents.assignment_run.supervise") as execution:
            self.assertFalse(restarted.tick())
        execution.assert_not_called()
        self.assertFalse(self.co.history(1)[1]["accepted"])
        self.assertEqual(len(self.notices()), 1)

    def test_restart_preserves_own_merge_receipt_from_interrupted_recovery(self):
        def run(*args, **kwargs):
            self.merge()
            raise LostOwnership("Launcher stopped before observing merge")

        with patch("ub_agents.assignment_run.supervise", side_effect=run), self.assertRaises(LostOwnership):
            self.loop.tick()
        self.now = seconds(self.co.history(1)[0]["expires"])
        restarted = self.restart()
        with patch.object(Settlement, "settle", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            restarted.tick()
        recovery = self.co.history(1)[-1]
        self.assertIn("own_merge_head", recovery)
        self.now = seconds(recovery["expires"])
        self.github.change(1, head="unrelated-later-head")
        restarted = self.restart()
        with patch("ub_agents.assignment_run.supervise") as execution:
            self.assertTrue(restarted.tick())
        execution.assert_not_called()
        self.assertEqual(self.co.history(1)[-2]["result"], "blocked")
        self.assertEqual(len(self.notices()), 1)
