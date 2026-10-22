"""Run with: python -m spikes.parallel_gates_dry_run --scratch DIRECTORY.

Real Loop planning, claims, reporting, transitions, release and expiry recovery;
FakeGitHub durable storage and a recording executor. No GitHub or model calls.
Completion order is controlled, not concurrent. The claim race uses two threads.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import ValidationError
from ub_agents.loop import Loop
from ub_agents.records import body, iso, live_leases, timestamp
from tests.support import FakeGitHub, agent, config, edit_lease, issue, pr, stub_refresh


SCRATCH = None


def transition(add=(), remove=()):
    return {"add": tuple(add), "remove": tuple(remove)}


class ParallelGatesDryRun(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH, prefix="parallel-gates-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.visual = agent(self.root, name="visual", kind="pr", parallel=True,
                            triggers=("needs-visual",), outcomes={
                                "passed": transition(("visual-ok",)),
                                "changes": transition(("needs-changes",), ("needs-security", "security-ok"))})
        self.security = replace(self.visual, name="security", triggers=("needs-security",), outcomes={
            "passed": transition(("security-ok",)),
            "changes": transition(("needs-changes",), ("needs-visual", "visual-ok"))})
        self.open = agent(self.root, name="open", kind="pr", triggers=("open",), outcomes={
            "done": transition(("needs-visual", "needs-security"), ("visual-ok", "security-ok"))})
        self.integrator = agent(self.root, name="integrator", kind="pr", trigger_all=True,
                                triggers=("visual-ok", "security-ok"), outcomes={"done": transition()})
        self.roles = (self.open, self.visual, self.security, self.integrator)
        self.github = FakeGitHub(pr(labels=frozenset({"open"})))
        self.serial = 0

    def loop(self):
        self.serial += 1
        root = self.root / str(self.serial)
        root.mkdir()
        return Loop(config(root, *self.roles), self.github, "operator", output=lambda *_: None)

    def plan(self, loop, role):
        return loop.coordinator.plan(self.github.item(2), role, ())

    def start(self, role):
        loop = self.loop()
        plan = self.plan(loop, role)
        self.assertEqual(plan.state, "ready", plan.reason)
        lease = loop.coordinator.claim(plan)
        self.assertIsNotNone(lease)
        loop.coordinator.update(lease, state="running", started=True)
        return loop, plan, lease

    def finish(self, run, name="passed"):
        loop, plan, lease = run
        outcome = loop.coordinator.report(lease, "success", name, outcome=name)
        result, summary, effect = loop.reported_verdict(lease, plan, outcome, lease, "dry run")
        loop.settle(plan, lease, lease["attempt"], result, summary, effect, outcome, completed=True)
        return outcome

    def fan_out(self):
        self.finish(self.start(self.open), "done")

    def assert_integrator_starts(self):
        loop = self.loop()
        started = []

        def execute(*args, **kwargs):
            lease = live_leases(loop.coordinator.history(2), timestamp())[0]
            started.append(lease["agent"])
            loop.coordinator.report(lease, "success", "Integrated", outcome="done")
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execute):
            self.assertTrue(loop.tick_item(2, "integrator"))
        self.assertEqual(started, ["integrator"])

    def test_both_pass_and_integrator_starts(self):
        self.fan_out()
        visual, security = self.start(self.visual), self.start(self.security)
        self.assertEqual(len(live_leases(self.loop().coordinator.history(2), timestamp())), 2)
        self.assertEqual(self.plan(self.loop(), self.open).state, "owned")
        self.assertIsNone(visual[0].coordinator.shared_branch_owner(self.github.item(2), self.visual, []))
        self.finish(visual)
        self.assertNotEqual(self.plan(self.loop(), self.integrator).state, "ready")
        self.finish(security)
        self.assert_integrator_starts()
        print("both pass: two live leases -> visual-ok + security-ok -> integrator executed")

    def test_changes_while_sibling_running(self):
        self.fan_out()
        visual, security = self.start(self.visual), self.start(self.security)
        self.finish(visual, "changes")
        self.assertEqual(security[2]["state"], "running")
        self.assertNotIn("needs-security", self.github.item(2).labels)
        outcome = self.finish(security)
        self.assertTrue(outcome["cancelled"])
        self.assertFalse(outcome["accepted"])
        self.assertEqual(security[2]["result"], "cancelled")
        self.assertEqual(security[2]["attempt_effect"], "unchanged")
        self.assertEqual(self.github.item(2).labels, frozenset({"needs-changes"}))
        print("changes: running sibling cancelled at completion; no security-ok or integrator")

    def test_head_moves_between_passes(self):
        self.fan_out()
        visual = self.start(self.visual)
        self.finish(visual)
        old_security = self.start(self.security)
        self.github.change(2, head="b" * 40)
        # report() observes the new head; assignment SHA still fences old work.
        outcome = old_security[0].coordinator.report(old_security[2], "success", "old work", outcome="passed")
        with self.assertRaises(ValidationError):
            old_security[0].finalize(old_security[2], old_security[1], outcome, "stale")
        old_security[0].settle(old_security[1], old_security[2], 1, "blocked", "stale", "failure", outcome)
        # A report made after the head moved pins the blocked SHA to the new
        # head, so use the normal operator-reset record before rerunning it.
        self.github.create_comment(2, body({"kind": "reset", "run": "operator-reset", "agent": "security",
                                           "assignment": 2, "runtime": "direct", "actor": "operator",
                                           "created": iso(timestamp()), "summary": "Rerun on new head"}))
        self.finish(self.start(self.security))
        self.assertEqual(self.github.item(2).labels, frozenset({"visual-ok", "security-ok"}))
        self.assertEqual(self.plan(self.loop(), self.integrator).state, "waiting")
        print("head move: stale in-flight work rejected; mixed-head labels leave join waiting")

    def test_two_gates_in_one_flow(self):
        # Second fan-out has its own trigger/pass labels, opened by the first join.
        second_visual = replace(self.visual, name="visual-two", triggers=("needs-visual-two",),
                                outcomes={"passed": transition(("visual-two-ok",))})
        second_security = replace(self.security, name="security-two", triggers=("needs-security-two",),
                                  outcomes={"passed": transition(("security-two-ok",))})
        between = replace(self.integrator, name="between", outcomes={"done": transition(
            ("needs-visual-two", "needs-security-two"), ("visual-two-ok", "security-two-ok"))})
        final = replace(self.integrator, triggers=("visual-two-ok", "security-two-ok"))
        self.roles = (*self.roles[:-1], between, second_visual, second_security, final)
        self.fan_out()
        first_visual, first_security = self.start(self.visual), self.start(self.security)
        self.finish(first_visual)
        self.finish(first_security)
        self.assertNotEqual(self.plan(self.loop(), final).state, "ready")
        self.finish(self.start(between), "done")
        visual, security = self.start(second_visual), self.start(second_security)
        self.finish(visual)
        self.assertNotEqual(self.plan(self.loop(), final).state, "ready")
        self.finish(security)
        self.assert_integrator_starts()
        print("two gates: first pair -> intermediate join/fan-out -> second pair -> integrator")

    def test_reopening_same_head_invalidates_previous_passes(self):
        self.fan_out()
        visual, security = self.start(self.visual), self.start(self.security)
        self.finish(visual)
        self.finish(security)
        self.github.add_labels(2, ["open"])
        self.fan_out()
        # Stray/manual pass labels cannot resurrect evidence from an old opening.
        self.github.add_labels(2, ["visual-ok", "security-ok"])
        self.assertEqual(self.plan(self.loop(), self.integrator).state, "waiting")
        print("same-head reopen: previous accepted passes cannot satisfy the new opening")

    def test_recovery_alongside_sibling(self):
        self.fan_out()
        visual, security = self.start(self.visual), self.start(self.security)
        visual[0].coordinator.report(visual[2], "success", "durable pass", outcome="passed")
        edit_lease(self.github, security[2], expires=iso(timestamp() + 300))
        recovery = self.loop()
        # Advance just this synthetic observer beyond visual's lease, retaining
        # an outcome created within its original ownership interval.
        recovery.coordinator.clock = lambda: timestamp() + 61
        plan = self.plan(recovery, self.visual)
        self.assertEqual(plan.state, "recover")
        with patch("ub_agents.loop.supervise") as execute:
            self.assertTrue(recovery.recover(plan))
        execute.assert_not_called()
        self.finish(security)
        self.assert_integrator_starts()
        print("recovery: expired visual outcome replayed while security stayed live; no reexecution")

    def test_claim_election_keeps_compatible_winners(self):
        self.fan_out()
        left, right = self.loop(), self.loop()
        plans = self.plan(left, self.visual), self.plan(right, self.security)
        self.github.claim_read_barrier = threading.Barrier(2)
        # Existing history defeats claim_read_barrier's empty-store hook, so
        # synchronize the tentative lease writes instead.
        self.github.claim_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(loop.coordinator.claim, plan) for loop, plan in zip((left, right), plans)]
            leases = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(leases))
        self.github.claim_barrier = self.github.claim_read_barrier = None
        for loop, lease in zip((left, right), leases):
            loop.coordinator.assert_owned(lease)
        self.assertEqual(self.plan(self.loop(), self.visual).state, "owned")
        self.assertEqual(self.plan(self.loop(), self.integrator).state, "owned")
        print("claim race: distinct parallel agents both own; duplicate and exclusive agents wait")

    def test_same_role_claim_race_has_one_winner(self):
        self.fan_out()
        loops = (self.loop(), self.loop())
        plans = [self.plan(loop, self.visual) for loop in loops]
        before_write = threading.Barrier(2)
        self.github.claim_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(loop.coordinator.claim, plan,
                                       before_write=lambda: before_write.wait(timeout=5))
                       for loop, plan in zip(loops, plans)]
            leases = [future.result(timeout=10) for future in futures]
        self.github.claim_barrier = None
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        print("duplicate claim race: one winner, losing tentative lease withdrawn")

    def test_issue_branch_writer_still_excludes_both_gates(self):
        self.fan_out()
        self.github.items[1] = issue()
        self.github.change(2, branch="ub-agents/builder/1/seed")
        builder = agent(self.root, name="builder", kind="issue")
        loop = self.loop()
        lease = loop.coordinator.claim(loop.coordinator.plan(self.github.item(1), builder, ()))
        self.assertIsNotNone(lease)
        for role in (self.visual, self.security):
            plan = self.plan(self.loop(), role)
            self.assertEqual(plan.state, "owned")
            self.assertIn("branch", plan.reason)
        loop.coordinator.release(lease, "success", "Writer finished", attempt_effect="unchanged")
        self.assertEqual(self.plan(self.loop(), self.visual).state, "ready")
        self.assertEqual(self.plan(self.loop(), self.security).state, "ready")
        print("shared branch: linked issue writer excludes both PR gates until release")

    def test_handoff_remove_still_targets_source_issue(self):
        self.github.items[1] = issue(labels=("ready", "visual-ok", "security-ok"))
        self.github.change(2, labels=frozenset({"visual-ok", "security-ok"}))
        implementer = agent(self.root, name="implementer", kind="issue", triggers=("ready",),
                            outcomes={"handed-off": self.open.outcomes["done"]})
        loop = self.loop()
        plan = loop.coordinator.plan(self.github.item(1), implementer, ())
        lease = loop.coordinator.claim(plan)
        loop.coordinator.update(lease, state="running", started=True)
        report = loop.coordinator.report(lease, "success", "fan out", handoff=2, outcome="handed-off")
        verdict = loop.reported_verdict(lease, plan, report, lease, "handoff")
        loop.settle(plan, lease, 1, *verdict, report, completed=True)
        self.assertEqual(self.github.item(1).labels, frozenset())
        self.assertEqual(self.github.item(2).labels,
                         frozenset({"needs-visual", "needs-security", "visual-ok", "security-ok"}))
        self.assertNotEqual(self.plan(self.loop(), self.integrator).state, "ready")
        print("issue handoff: existing remove clears source issue; destination passes need a new target-remove operation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    SCRATCH = args.scratch
    unittest.main(argv=["parallel_gates_dry_run"], verbosity=2)
