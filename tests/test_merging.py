from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import body, payload, seconds, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class MergeTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reset()

    def reset(self):
        self.agent = agent(self.root, kind="pr", triggers=("ready-to-merge",), on_conflict="changes-requested",
                           outcomes={"merged": {"add": (), "remove": (), "merge": "squash"},
                                     "changes-requested": {"add": ("needs-changes",), "remove": ()}})
        self.github = FakeGitHub(issue(), pr(labels=("ready-to-merge",)))
        self.github.change(2, mergeable=True)
        self.now = timestamp()
        self.lines, self.waits = [], []
        self.loop = self.new_loop()

    def new_loop(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        return loop

    def run_agent(self, after_report=None):
        def supervise(*args, **kwargs):
            lease = self.loop.coordinator.history(2)[0]
            outcome = self.loop.coordinator.report(lease, "success", "Policy allows this merge", outcome="merged")
            if after_report:
                after_report(outcome)
            return 0
        with patch("ub_agents.loop.supervise", side_effect=supervise) as process:
            self.assertTrue(self.loop.tick_item(2))
        self.assertEqual(process.call_count, 1)
        history = self.loop.coordinator.history(2)
        return history[0], history[1]

    def merge_calls(self):
        return [w for w in self.github.writes if w[0] == "merge"]

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return False

    def assert_blocked(self, reason, callback=None):
        lease, outcome = self.run_agent(callback)
        self.assertEqual(lease["result"], "blocked")
        self.assertFalse(outcome["accepted"])
        self.assertIn(reason, outcome["rejected"])
        self.assertEqual(self.merge_calls(), [])
        self.assertFalse(self.github.item(2).merged)
        self.assertEqual(self.github.item(1).state, "open")

    def test_each_method_merges_exact_head_accepts_commit_then_closes_issues(self):
        for method in ("squash", "merge", "rebase"):
            with self.subTest(method=method):
                self.reset()
                self.agent.outcomes["merged"]["merge"] = method
                close_issue = self.github.close_issue
                def close(number, repository):
                    self.assertTrue(self.loop.coordinator.history(2)[1]["accepted"])
                    close_issue(number, repository)
                with patch.object(self.github, "close_issue", side_effect=close):
                    lease, outcome = self.run_agent()
                self.assertEqual(lease["result"], "success")
                self.assertTrue(outcome["accepted"])
                self.assertEqual(outcome["candidate_sha"], "a" * 40)
                self.assertEqual(outcome["merge_commit_sha"], "f" * 40)
                self.assertIn("a" * 40, outcome["summary"])
                self.assertIn("f" * 40, outcome["summary"])
                self.assertEqual(self.merge_calls(), [("merge", 2, method, "a" * 40)])
                self.assertEqual(self.github.item(1).state, "closed")

    def test_pending_then_passing_checks_and_unknown_then_known_mergeability(self):
        checks = [[], [("build", "pending")], [("build", "pass"), ("lint", "pass")]]
        original = self.github.item
        def item(number, kind=None):
            current = original(number, kind)
            if number == 2 and len(self.waits) >= 2:
                return replace(current, mergeable=len(self.waits) >= 3)
            return current
        def changing_item(number, kind=None):
            current = item(number, kind)
            return replace(current, mergeable=None) if number == 2 and len(self.waits) == 2 else current
        with patch.object(self.github, "commit_checks", side_effect=checks) as read, \
                patch.object(self.github, "item", side_effect=changing_item), \
                patch.object(self.loop.interrupt_event, "wait", side_effect=self.wait):
            self.agent = replace(self.agent, timeout_seconds=100)
            self.loop.config = config(self.root, self.agent)
            lease, outcome = self.run_agent()
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])
        self.assertEqual(self.waits, [10, 10, 2])
        self.assertEqual([call.args for call in read.call_args_list], [("a" * 40,)] * 3)

    def test_absent_or_pending_checks_block_at_original_execution_deadline(self):
        for checks, text in (([], "checks to be reported"), ([("build", "pending")], "pending checks: build")):
            with self.subTest(checks=checks):
                self.reset()
                def elapsed(outcome):
                    self.now += 7
                with patch.object(self.github, "commit_checks", return_value=checks), \
                        patch.object(self.loop.interrupt_event, "wait", side_effect=self.wait):
                    self.assert_blocked(text, elapsed)
                self.assertEqual(self.waits, [3])
                self.assertIn("agent-timeout deadline", self.loop.coordinator.history(2)[1]["rejected"])

    def test_head_moved_stop_label_closed_pr_and_unrelated_head_refuse(self):
        cases = (("head moved", lambda _: self.github.change(2, head="b" * 40)),
                 ("stop label", lambda _: self.github.change(2, labels=frozenset({"needs-human", "ready-to-merge"}))),
                 ("not open", lambda _: self.github.change(2, state="closed")),
                 ("trigger disappeared", lambda _: self.github.change(2, labels=frozenset())))
        for reason, callback in cases:
            with self.subTest(reason=reason):
                self.reset()
                self.assert_blocked(reason, callback)

    def test_only_a_merge_with_candidate_as_first_parent_keeps_authority(self):
        for parents, allowed in (([], False), (["a" * 40], False),
                                 (["c" * 40, "a" * 40], False), (["a" * 40, "c" * 40], True)):
            with self.subTest(parents=parents):
                self.reset()
                def moved_before_report(*args, **kwargs):
                    self.github.change(2, head="b" * 40)
                    lease = self.loop.coordinator.history(2)[0]
                    self.loop.coordinator.report(lease, "success", "Own base merge", outcome="merged")
                    return 0
                with patch.object(self.github, "commit_parents", return_value=parents), \
                        patch("ub_agents.loop.supervise", side_effect=moved_before_report):
                    self.assertTrue(self.loop.tick_item(2))
                report = self.loop.coordinator.history(2)[1]
                self.assertEqual(report["accepted"], allowed)
                self.assertEqual(bool(self.merge_calls()), allowed)

    def test_failed_checks_and_conflicts_route_to_on_conflict(self):
        for conflict, routed in ((False, True), (True, True), (False, False), (True, False)):
            with self.subTest(conflict=conflict, routed=routed):
                self.reset()
                if not routed:
                    self.agent = replace(self.agent, on_conflict=None)
                    self.loop = self.new_loop()
                def conflict_after_report(outcome):
                    if conflict:
                        self.github.change(2, mergeable=False)
                checks = [("tests", "pass")] if conflict else [("lint", "fail"), ("build", "fail")]
                with patch.object(self.github, "commit_checks", return_value=checks):
                    lease, outcome = self.run_agent(conflict_after_report)
                self.assertEqual(lease["result"], "success" if routed else "blocked")
                self.assertEqual(outcome["accepted"], routed)
                reason = "conflicts" if conflict else "failing checks: lint, build"
                self.assertIn(reason, outcome["summary"] if routed else outcome["rejected"])
                if routed:
                    self.assertEqual(outcome["outcome"], "changes-requested")
                    self.assertEqual(self.github.item(2).labels, {"needs-changes"})
                self.assertFalse(self.github.item(2).merged)
                self.assertEqual(self.merge_calls(), [])

    def test_mergeability_unknown_is_retried_a_few_times_then_blocks(self):
        def unknown(outcome):
            self.github.change(2, mergeable=None)
        with patch.object(self.loop.interrupt_event, "wait", side_effect=self.wait):
            self.assert_blocked("mergeability is still unknown", unknown)
        self.assertEqual(self.waits, [2, 2, 2])

    def test_stop_or_head_change_during_wait_prevents_merge(self):
        for change, reason in (({"labels": frozenset({"needs-human"})}, "stop label"),
                               ({"head": "b" * 40}, "head moved")):
            with self.subTest(change=change):
                self.reset()
                def wait(seconds):
                    self.github.change(2, **change)
                    return self.wait(seconds)
                with patch.object(self.github, "commit_checks", return_value=[("build", "pending")]), \
                        patch.object(self.loop.interrupt_event, "wait", side_effect=wait):
                    self.assert_blocked(reason)

    def test_merge_error_is_confirmed_without_replaying_the_mutation(self):
        original = self.github.merge_pr
        def landed(number, method, sha):
            original(number, method, sha)
            raise GitHubError("merge", str(number), "response lost")
        with patch.object(self.github, "merge_pr", side_effect=landed) as merge:
            lease, outcome = self.run_agent()
        self.assertEqual(merge.call_count, 1)
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])

    def test_unconfirmed_merge_error_or_success_blocks(self):
        for error in (None, GitHubError("merge", "2", "permission denied")):
            with self.subTest(error=error):
                self.reset()
                with patch.object(self.github, "merge_pr", side_effect=error) as merge:
                    self.assert_blocked("merge was not confirmed")
                self.assertEqual(merge.call_count, 1)

    def test_merge_commit_must_be_on_base(self):
        with patch.object(self.github, "commit_on_base", return_value=False):
            lease, outcome = self.run_agent()
        self.assertEqual(lease["result"], "blocked")
        self.assertFalse(outcome["accepted"])
        self.assertIn("on the base branch", outcome["rejected"])

    def test_issue_close_failure_does_not_change_acceptance(self):
        for operation in ("close_issue", "closing_issue_references"):
            with self.subTest(operation=operation):
                self.reset()
                with patch.object(self.github, operation, side_effect=GitHubError("PATCH", "issue", "denied")):
                    lease, outcome = self.run_agent()
                self.assertEqual(lease["result"], "success")
                self.assertTrue(outcome["accepted"])
                self.assertTrue(any("could not" in line for line in self.lines))

    def test_recovery_before_and_after_merge_never_merges(self):
        for merged, started in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(merged=merged, started=started):
                self.reset()
                plan = next(p for p in self.loop.plans() if p.item.number == 2)
                source = self.loop.coordinator.claim(plan)
                self.loop.coordinator.update(source, state="running", started=True)
                report = self.loop.coordinator.report(source, "success", "Policy passed", outcome="merged")
                if started:
                    self.github.update_comment(report["id"], body(payload(report) |
                        {"transition": report["transition"] | {"started": True}}))
                if merged:
                    self.github.merge_pr(2, "squash", "a" * 40)
                self.now += 61
                self.loop = self.new_loop()
                with patch.object(self.github, "merge_pr", side_effect=AssertionError("recovery never merges")), \
                        patch("ub_agents.loop.supervise", side_effect=AssertionError("recovery never runs agent")):
                    self.assertTrue(self.loop.tick_item(2))
                history = self.loop.coordinator.history(2)
                outcome = history[1]
                recovery = next(r for r in history if r.get("mode") == "recovery")
                self.assertEqual(outcome["accepted"], merged)
                self.assertEqual(recovery["result"], "success" if merged else "retry")
                if not merged:
                    self.assertEqual(next(iter(self.loop.item_plans(2)[1])).state, "ready")

    def test_projects_without_merge_keep_agent_managed_behavior(self):
        self.agent.outcomes["merged"].pop("merge")
        lease, outcome = self.run_agent()
        self.assertTrue(outcome["accepted"])
        self.assertEqual(self.merge_calls(), [])
        self.assertFalse(self.github.item(2).merged)

    def test_lease_renews_while_waiting_without_execution(self):
        self.agent = replace(self.agent, lease_seconds=90, timeout_seconds=200)
        self.loop = self.new_loop()
        self.loop.coordinator.lease_seconds = 90
        self.loop.coordinator.lease_renew_seconds = 10
        expiries = []
        def wait(delay):
            self.now += delay
            self.loop._renewal.tick()
            expiries.append(seconds(self.loop._renewal.lease["expires"]))
            return False
        with patch.object(self.github, "commit_checks", side_effect=[[("build", "pending")]] * 10 + [[("build", "pass")]]), \
                patch.object(self.loop.interrupt_event, "wait", side_effect=wait):
            lease, outcome = self.run_agent()
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])
        self.assertGreater(expiries[-1], expiries[0] + 80)

    def test_lost_ownership_before_merge_prevents_the_mutation(self):
        def revoke(sha):
            lease = self.loop.coordinator.history(2)[0]
            self.github.update_comment(lease["id"], body(payload(lease) | {"state": "withdrawn"}))
            return [("build", "pass")]
        with patch.object(self.github, "commit_checks", side_effect=revoke), self.assertRaises(LostOwnership):
            self.run_agent()
        self.assertEqual(self.merge_calls(), [])
        self.assertFalse(self.loop.coordinator.history(2)[1]["accepted"])

    def test_interruption_around_merge_recovers_by_reading_only(self):
        for landed in (False, True):
            with self.subTest(landed=landed):
                self.reset()
                original = self.github.merge_pr
                def interrupt(number, method, sha):
                    if landed:
                        original(number, method, sha)
                    raise KeyboardInterrupt
                with patch.object(self.github, "merge_pr", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                    self.run_agent()
                self.now += 61
                self.loop = self.new_loop()
                with patch.object(self.github, "merge_pr", side_effect=AssertionError("recovery never merges")):
                    self.assertTrue(self.loop.tick_item(2))
                report = self.loop.coordinator.history(2)[1]
                self.assertEqual(report["accepted"], landed)

    def test_conflict_routing_preserves_round_limit(self):
        self.agent.outcomes["changes-requested"]["max-rounds"] = 1
        with patch.object(self.github, "commit_checks", return_value=[("build", "fail")]):
            self.run_agent()
            self.github.change(2, labels=frozenset({"ready-to-merge"}))
            self.loop = self.new_loop()
            # The second run reads the newest source lease.
            def supervise(*args, **kwargs):
                lease = next(r for r in reversed(self.loop.coordinator.history(2)) if r["kind"] == "lease")
                self.loop.coordinator.report(lease, "success", "Policy allows merge", outcome="merged")
                return 0
            with patch("ub_agents.loop.supervise", side_effect=supervise):
                self.assertTrue(self.loop.tick_item(2))
        report = [r for r in self.loop.coordinator.history(2) if r["kind"] == "outcome"][-1]
        self.assertTrue(report["accepted"])
        self.assertEqual(report["outcome"], "changes-requested")
        self.assertEqual(self.github.item(2).labels, {"needs-human"})
        self.assertEqual(self.merge_calls(), [])
