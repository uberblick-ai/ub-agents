from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, iso, records
from tests.support import FakeGitHub, agent, config, issue, pr


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root)
        self.github = FakeGitHub(issue(), pr())
        self.now = 1000
        self.co = Coordinator(self.github, "operator", lambda: self.now)

    def plan(self, item=None, agent=None):
        return self.co.plan(item or self.github.item(1), agent or self.agent, ("needs-human",))

    def start(self, item=None, agent=None):
        lease = self.co.claim(self.plan(item, agent))
        self.co.update(lease, state="running", started=True)
        return lease

    def test_cooperative_claim_race_only_earliest_record_wins(self):
        plan = self.plan()
        self.github.claim_barrier = threading.Barrier(2)
        self.github.claim_read_barrier = threading.Barrier(2)
        other = Coordinator(self.github, "operator", lambda: self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda co: co.claim(plan), [self.co, other]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual([r["state"] for r in self.co.history(1)], ["claiming", "withdrawn"])

    def test_renew_same_record_and_never_resurrect_expired_ownership(self):
        lease = self.start()
        self.now += 10
        self.co.renew(lease, 60)
        self.assertEqual(len(self.github.store[1]), 1)
        self.assertEqual(lease["expires"], iso(self.now + 60))
        self.now += 61
        writes = list(self.github.writes)
        with self.assertRaises(LostOwnership):
            self.co.renew(lease, 60)
        self.assertEqual(self.github.writes, writes)

    def test_renewal_read_failure_is_ownership_loss_and_no_write(self):
        lease = self.start()
        self.github.unreadable = True
        writes = list(self.github.writes)
        with self.assertRaises(LostOwnership):
            self.co.renew(lease, 60)
        self.assertEqual(self.github.writes, writes)

    def test_head_or_trigger_change_before_claim_costs_no_attempt(self):
        plan = self.plan(self.github.item(2))
        self.github.change(2, head="b" * 40)
        self.assertIsNone(self.co.claim(plan))
        self.assertEqual(self.github.store, {})
        plan = self.plan()
        self.github.change(1, labels=frozenset())
        self.assertIsNone(self.co.claim(plan))

    def test_attempts_backoff_and_expiry_reconstruct_after_restart(self):
        lease = self.start()
        self.co.report(lease, "retry", "Transient command failure")
        self.co.release(lease, "retry", "Transient command failure", 10)
        restarted = Coordinator(self.github, "operator", lambda: self.now)
        self.assertEqual(restarted.plan(self.github.item(1), self.agent, ()).state, "backoff")
        self.now += 11
        self.assertEqual(self.plan().attempt, 2)
        abandoned = self.start()
        self.now += 61
        self.assertEqual(self.plan().attempt, 3)
        self.start()
        self.now += 61
        self.assertEqual(self.plan().state, "blocked")
        self.assertEqual(len(attempts(self.co.history(1), self.agent.name, self.now)), 3)
        self.assertEqual(abandoned["state"], "running")

    def test_fenced_records_fail_closed_instead_of_parsing_prose(self):
        record = self.start()
        comment = self.github.store[1][0]
        self.assertIn("```json\n", comment["body"])
        written = json.loads(comment["body"].rsplit("```json\n", 1)[1].removesuffix("\n```\n"))
        self.assertFalse(written.keys() & {"actor", "assignment_kind", "assignment_sha", "handoff", "version"})
        parsed = records([comment])[0]
        self.assertEqual((parsed["run"], parsed["actor"]), (record["run"], "operator"))
        self.assertIsNone(parsed["assignment_sha"])
        self.assertEqual(records([{"body": "Done!", "id": 9}]), [])
        comment["body"] = "<!-- ub-agent:v1 -->\nbad json"
        with self.assertRaises(AgentError):
            records([comment])

    def test_runtime_exclusion_uses_exact_accepted_candidate_and_not_effort_or_account(self):
        codex = Runtime("codex", "model-a", "high", "openai")
        claude = Runtime("claude", "model-b", "high", "anthropic")
        source = agent(self.root, name="builder", command=(), runtimes=(codex,))
        reviewer = agent(self.root, name="checker", command=(), runtimes=(replace(codex, effort="low"), claude),
                         different_from="builder", triggers=("needs-review",), kind="pr")
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            lease = self.start(agent=source)
            outcome = self.co.report(lease, "success", "Candidate implemented", handoff=2)
            self.co.accept(lease, outcome)
            self.co.release(lease, "success", "Candidate implemented")
            candidate = replace(self.github.item(2), labels=frozenset({"needs-review"}))
            self.assertEqual(self.co.choose_runtime(candidate, reviewer, self.co.history(2)), claude)
            with self.assertRaises(AgentError):
                self.co.choose_runtime(candidate, replace(reviewer, runtimes=(replace(codex, effort="low"),)), self.co.history(2))
            with self.assertRaises(AgentError):
                self.co.choose_runtime(replace(candidate, head="b" * 40), reviewer, self.co.history(2))

    def test_completed_issue_handoff_suppresses_ready_and_second_pr_revision_keeps_budget(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        outcome = self.co.report(lease, "success", "PR opened", handoff=2)
        loop.validate_success(self.plan(), outcome)  # ready may remain after explicit PR handoff
        self.co.accept(lease, outcome)
        self.co.release(lease, "success", "PR opened")
        self.assertEqual(self.plan().state, "completed")
        revision = self.start(self.github.item(2))
        self.github.change(2, labels=frozenset({"needs-review"}), head="b" * 40)
        outcome = self.co.report(revision, "success", "Revision complete")
        self.co.accept(revision, outcome)
        self.co.release(revision, "success", "Revision complete")
        self.github.change(2, labels=frozenset({"needs-changes"}))
        new = self.plan(self.github.item(2))
        self.assertEqual((new.state, new.attempt), ("ready", 2))

    def test_restart_after_outcome_before_release_validates_without_execution(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.co.report(lease, "success", "PR opened", handoff=2)
        self.now += 61
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.plan().state, "completed")
        self.assertEqual(self.co.history(1)[0]["state"], "running")  # old ownership is not rewritten
        self.assertEqual(self.co.pending_completion(self.co.history(1), self.agent.name, self.now), None)

    def test_restart_after_acceptance_before_release_also_repairs_provenance(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        outcome = self.co.report(lease, "success", "PR opened", handoff=2)
        self.co.accept(lease, outcome)
        self.now += 61
        self.assertEqual(self.plan().state, "recover")
        self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.plan().state, "completed")

    def test_explicit_reset_does_not_erase_history(self):
        lease = self.start()
        self.co.release(lease, "blocked", "Operator action required")
        self.assertEqual(self.plan().state, "blocked")
        reset = {"kind": "reset", "run": "operator-reset", "agent": self.agent.name,
                 "actor": "operator", "runtime": "operator", "created": iso(self.now),
                 "assignment": 1, "summary": "Fixed authentication"}
        self.github.create_comment(1, body(reset))
        self.assertEqual((self.plan().state, self.plan().attempt), ("ready", 1))
        self.assertEqual(len(self.co.history(1)), 2)

    def test_unsafe_retry_checks_all_prior_branches_even_before_reset_without_execution(self):
        for result in ("expired", "retry", "blocked"):
            for reset in (False, True):
                for draft in (False, True):
                    with self.subTest(result=result, reset=reset, draft=draft):
                        self.github = FakeGitHub(issue(), pr(labels=(), draft=draft))
                        self.co = Coordinator(self.github, "operator", lambda: self.now)
                        oldest = self.start()
                        self.co.update(oldest, branch="oldest/checkpoint")
                        self.co.release(oldest, "retry", "Interrupted")
                        newer = self.start()
                        self.co.update(newer, branch="newer/no-pr")
                        if result == "expired":
                            self.now += 61
                        else:
                            self.co.release(newer, result, "Interrupted")
                        if reset:
                            self.github.create_comment(1, body({
                                "kind": "reset", "run": "operator-reset", "agent": self.agent.name,
                                "actor": "operator", "runtime": "operator", "created": iso(self.now),
                                "assignment": 1, "summary": "Inspect retry after interruption"}))
                        # The PR is on the oldest branch, not the latest lease's,
                        # and its body does not link the issue: discovery uses head.
                        self.github.change(2, branch="oldest/checkpoint", body="Checkpoint")
                        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
                        loop.coordinator = self.co
                        writes = list(self.github.writes)
                        plan = loop.plans()[0]
                        self.assertEqual(plan.state, "blocked")
                        self.assertIn("#2", plan.reason)
                        self.assertIn("continue the existing PR manually", plan.reason)
                        self.assertIn("close it before ub-agent retry --number 1 --agent worker", plan.reason)
                        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not duplicate PR")):
                            self.assertFalse(loop.tick())
                        self.assertIsNone(self.co.claim(plan))
                        self.assertEqual(self.github.writes, writes)
                        self.assertEqual(set(self.github.items), {1, 2})

    def test_retry_resumes_historical_draft_with_normal_budget_backoff_and_reset_gates(self):
        for result in ("expired", "retry", "blocked"):
            for reset in (False, True):
                with self.subTest(result=result, reset=reset):
                    self.github = FakeGitHub(issue(), pr(labels=(), draft=True))
                    self.co = Coordinator(self.github, "operator", lambda: self.now)
                    worker = replace(self.agent, worktree=True)
                    old = self.start(agent=worker)
                    self.co.update(old, branch="feature/test")
                    self.co.release(old, "retry", "Interrupted")
                    # Discover an older checkpoint even after a newer run/reset.
                    self.github.change(2, state="closed")
                    newer = self.start(agent=worker)
                    self.co.update(newer, branch="newer/no-pr")
                    if result == "expired":
                        self.now += 61
                    else:
                        self.co.release(newer, result, "Interrupted", 10 if result == "retry" else 0)
                    self.github.change(2, state="open")
                    if reset:
                        self.github.create_comment(1, body({
                            "kind": "reset", "run": "reset", "agent": worker.name,
                            "actor": "operator", "runtime": "operator", "created": iso(self.now),
                            "assignment": 1, "summary": "Decision settled"}))
                    plan = self.plan(agent=worker)
                    expected = "ready" if reset or result == "expired" else (
                        "blocked" if result == "blocked" else "backoff")
                    self.assertEqual(plan.state, expected)
                    self.assertEqual(plan.attempt, 1 if reset else 3)
                    if expected != "ready":
                        self.assertIsNone(self.co.claim(plan))
                        continue
                    self.assertEqual(plan.resume_pr.number, 2)
                    lease = self.co.claim(plan)
                    self.assertEqual((lease["assignment"], lease["resume_pr"], lease["branch"], lease["resume_sha"]),
                                     (1, 2, "feature/test", "a" * 40))
                    self.assertIsNone(lease["assignment_sha"])
                    self.assertEqual(self.plan(self.github.item(2)).state, "blocked")
                    self.co.assert_owned(lease)
                    self.assertEqual(set(self.github.items), {1, 2})

    def test_resume_blocks_unsafe_prs_and_rechecks_changed_head_before_claim(self):
        worker = replace(self.agent, worktree=True)
        old = self.start(agent=worker)
        self.co.update(old, branch="feature/test")
        self.co.release(old, "retry", "Interrupted")
        self.github.change(2, labels=frozenset(), draft=True)
        ready = self.plan(agent=worker)
        self.assertEqual(ready.state, "ready")
        for change in ({"body": "No link"}, {"head_repository": "fork/project"},
                       {"head_repository": None}, {"draft": False}, {"merged": True}):
            with self.subTest(change=change):
                self.github.items[2] = replace(ready.resume_pr, **change)
                blocked = self.plan(agent=worker)
                self.assertEqual(blocked.state, "blocked")
                self.assertIn("#2", blocked.reason)
                self.assertIsNone(self.co.claim(ready))
        self.github.items[2] = replace(ready.resume_pr, head="b" * 40)
        writes = list(self.github.writes)
        self.assertIsNone(self.co.claim(ready))
        self.assertEqual(self.github.writes, writes)
        self.assertEqual(self.plan(agent=worker).resume_pr.head, "b" * 40)
        self.github.items[3] = replace(ready.resume_pr, number=3)
        self.assertIn("multiple open PRs", self.plan(agent=worker).reason)

    def test_resume_excludes_active_pr_run_and_pending_recovery_or_cleanup(self):
        worker = replace(self.agent, worktree=True)
        old = self.start(agent=worker)
        self.co.update(old, branch="feature/test")
        self.co.release(old, "retry", "Interrupted")
        self.github.change(2, draft=True)
        revision = self.start(self.github.item(2))
        self.assertIn("conflicting live ownership", self.plan(agent=worker).reason)
        self.co.report(revision, "blocked", "Parked")
        self.now += 61
        self.assertIn("awaiting recovery", self.plan(agent=worker).reason)
        self.co.update(revision, cleanup="unconfirmed")
        self.assertIn("cleanup is unconfirmed", self.plan(agent=worker).reason)

    def test_pr_and_resume_claim_race_elects_one_checkpoint_owner(self):
        worker = replace(self.agent, worktree=True)
        old = self.start(agent=worker)
        self.co.update(old, branch="feature/test")
        self.co.release(old, "retry", "Interrupted")
        self.github.change(2, draft=True)
        issue_plan = self.plan(agent=worker)
        pr_plan = self.plan(self.github.item(2))
        self.github.claim_barrier = threading.Barrier(2)
        # Both plans reobserve before either publishes its tentative claim.
        barrier = threading.Barrier(2)
        original = self.co.plan
        def synchronized(*args, **kwargs):
            plan = original(*args, **kwargs)
            barrier.wait(timeout=5)
            return plan
        with patch.object(self.co, "plan", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.co.claim, [issue_plan, pr_plan]))
        self.assertEqual(sum(r is not None for r in results), 1)
        winner = next(r for r in results if r)
        self.co.assert_owned(winner)

    def test_reuse_does_not_bypass_attempt_limit_or_failed_ownership_reads(self):
        worker = replace(self.agent, worktree=True, max_attempts=1)
        old = self.start(agent=worker)
        self.co.update(old, branch="feature/test")
        self.co.release(old, "retry", "Interrupted")
        self.github.change(2, draft=True)
        self.assertIn("Attempt limit", self.plan(agent=worker).reason)
        with patch.object(self.github, "repository_comments", side_effect=AgentError("GitHub unavailable")), \
                self.assertRaisesRegex(AgentError, "GitHub unavailable"):
            self.plan(agent=worker)

    def test_existing_pr_guard_is_fresh_at_claim_and_clears_when_pr_closed(self):
        lease = self.start()
        self.co.update(lease, branch="feature/test")
        self.co.release(lease, "retry", "Interrupted")
        self.github.change(2, state="closed")
        ready = self.plan()
        self.assertEqual(ready.state, "ready")
        self.github.change(2, state="open", draft=True)
        writes = list(self.github.writes)
        self.assertIsNone(self.co.claim(ready))
        self.assertEqual(self.github.writes, writes)
        self.github.change(2, state="closed")
        self.assertIsNotNone(self.co.claim(self.plan()))

    def test_prior_branches_are_scoped_to_same_issue_agent_and_reads_fail_closed(self):
        other = self.start(agent=replace(self.agent, name="preparer"))
        self.co.update(other, branch="feature/test")
        self.co.release(other, "success", "Prepared")
        self.assertEqual(self.plan().state, "ready")
        lease = self.start()
        self.co.update(lease, branch="feature/test")
        self.co.release(lease, "retry", "Interrupted")
        with patch.object(self.github, "prs_for_branch", side_effect=AgentError("GitHub unavailable")), \
                self.assertRaisesRegex(AgentError, "GitHub unavailable"):
            self.plan()

    def test_checkpoint_publication_keeps_issue_owned_without_outcome(self):
        lease = self.start()
        self.co.update(lease, branch="feature/test")
        self.github.change(2, labels=frozenset(), draft=True)
        self.assertEqual(self.plan().state, "owned")
        self.assertIsNone(self.co.outcome(lease))
        self.assertEqual(self.github.item(1).labels, frozenset({"ready"}))
        self.now += 10
        self.co.renew(lease, 60)
        self.assertEqual(self.plan().state, "owned")

    def test_draft_pr_labels_still_govern_pr_kind_pickup(self):
        self.github.change(2, draft=True)
        self.assertEqual(self.plan(self.github.item(2)).state, "ready")

    def test_relative_command_is_checked_in_its_configured_cwd(self):
        script = self.root / "script"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        configured = replace(self.agent, command=("./script",))
        self.assertEqual(self.plan(agent=configured).state, "ready")

    def test_recovered_blocked_outcome_stays_blocked_without_reexecution(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.co.report(lease, "blocked", "Needs human decision")
        self.now += 61
        self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.plan().state, "blocked")

    def test_normal_queue_recovers_completion_even_after_trigger_removed_and_item_closed(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.github.change(1, labels=frozenset(), state="closed")
        self.co.report(lease, "success", "Requirements resolved")
        self.now += 61
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.tick())
        self.assertTrue(self.co.history(1)[1]["accepted"])
        # The unrelated PR is still ready for a normal assignment; isolate recovery.
        self.github.change(2, labels=frozenset())
        self.assertFalse(loop.tick())

    def test_released_lease_leaves_a_repeated_summary_to_its_outcome(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.co.report(lease, "blocked", "Need a decision")
        self.co.release(lease, "blocked", "Need a decision")
        comment = self.github.store[1][0]["body"]
        written = json.loads(comment.rsplit("```json\n", 1)[1].removesuffix("\n```\n"))
        self.assertNotIn("summary", written)
        self.assertIn("Result: blocked; reported in the outcome.", comment)
        self.github.change(1, labels=frozenset())
        self.assertIn("Last run blocked: Need a decision", loop.plans()[0].reason)

    def test_expired_unlabelled_run_without_outcome_is_visible_for_operator_attention(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        self.start()
        self.github.change(1, labels=frozenset())
        self.now += 61
        plan = loop.plans()[0]
        self.assertEqual(plan.state, "blocked")
        self.assertIn("no outcome", plan.reason)

    def test_recovery_by_another_actor_preserves_source_authorship_in_mirrored_provenance(self):
        lease = self.start()
        self.co.report(lease, "success", "PR opened", handoff=2)
        self.now += 61
        self.github.login = "review-operator"
        loop = Loop(config(self.root, self.agent), self.github, "review-operator", output=lambda *_: None)
        loop.coordinator = Coordinator(self.github, "review-operator", lambda: self.now, trusted_actors=("operator",))
        self.assertTrue(loop.recover(loop.plans()[0]))
        mirrored = loop.coordinator.history(2)[0]
        self.assertEqual(mirrored["actor"], "operator")
        self.assertEqual(mirrored["recorded_by"], "review-operator")
