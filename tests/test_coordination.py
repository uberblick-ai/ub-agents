from concurrent.futures import ThreadPoolExecutor
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import status_rows
from ub_agents.config import Runtime
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import MARKER, attempts, body, iso, records, timestamp
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
        codex = Runtime("codex", "model-a", "high")
        claude = Runtime("claude", "model-b", "high")
        source = agent(self.root, name="builder", command=(), runtimes=(codex,))
        reviewer = agent(self.root, name="checker", command=(), runtimes=(replace(codex, effort="low"), claude),
                         different_from="builder", triggers=("needs-review",), kind="pr")
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            lease = self.start(agent=source)
            outcome = self.co.report(lease, "success", "Candidate implemented", handoff=2, outcome="done")
            self.co.accept(lease, outcome)
            self.co.release(lease, "success", "Candidate implemented")
            candidate = replace(self.github.item(2), labels=frozenset({"needs-review"}))
            self.assertEqual(self.co.choose_runtime(candidate, reviewer, self.co.history(2)), claude)
            with self.assertRaises(AgentError):
                self.co.choose_runtime(candidate, replace(reviewer, runtimes=(replace(codex, effort="low"),)), self.co.history(2))
            with self.assertRaises(AgentError):
                self.co.choose_runtime(replace(candidate, head="b" * 40), reviewer, self.co.history(2))

    def test_pr_revision_runs_a_new_assignment_with_the_accumulated_budget(self):
        lease = self.start()
        outcome = self.co.report(lease, "success", "PR opened", handoff=2, outcome="done")
        self.co.accept(lease, outcome)
        self.co.release(lease, "success", "PR opened")
        # Only a human-reapplied trigger would run the issue again; the budget continues.
        self.assertEqual((self.plan().state, self.plan().attempt), ("ready", 2))
        revision = self.start(self.github.item(2))
        self.github.change(2, labels=frozenset({"needs-review"}), head="b" * 40)
        outcome = self.co.report(revision, "success", "Revision complete", outcome="done")
        self.co.accept(revision, outcome)
        self.co.release(revision, "success", "Revision complete")
        self.github.change(2, labels=frozenset({"needs-changes"}))
        new = self.plan(self.github.item(2))
        self.assertEqual((new.state, new.attempt), ("ready", 2))

    def test_restart_after_outcome_before_release_validates_without_execution(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.co.report(lease, "success", "PR opened", handoff=2, outcome="done")
        self.now += 61
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.github.item(1).labels, frozenset())
        self.assertTrue(self.co.history(1)[1]["accepted"])
        self.assertEqual(self.co.history(1)[0]["state"], "running")  # old ownership is not rewritten
        self.assertEqual(self.co.pending_completion(self.co.history(1), self.agent.name, self.now), None)

    def test_restart_after_acceptance_before_release_also_repairs_provenance(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        outcome = self.co.report(lease, "success", "PR opened", handoff=2, outcome="done")
        self.co.accept(lease, outcome)
        self.now += 61
        self.assertEqual(self.plan().state, "recover")
        self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.github.item(1).labels, frozenset())
        self.assertTrue(self.co.history(1)[1]["transition_complete"])

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

    def test_checkpoint_publication_keeps_issue_owned_without_outcome(self):
        lease = self.start()
        self.co.update(lease, branch="feature/test")
        self.github.change(2, labels=frozenset(), draft=True)
        self.assertEqual(self.plan().state, "owned")
        self.assertIsNone(self.co.outcome(lease))
        self.assertEqual(self.github.item(1).labels, frozenset({"ready"}))

    def test_draft_pr_labels_still_govern_pr_kind_pickup(self):
        self.github.change(2, draft=True)
        self.assertEqual(self.plan(self.github.item(2)).state, "ready")

    def test_command_executable_must_exist_and_be_executable(self):
        script = self.root / "script"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        configured = replace(self.agent, command=(str(script),))
        self.assertEqual(self.plan(agent=configured).state, "ready")
        script.chmod(0o644)
        self.assertIn("not installed", self.plan(agent=configured).reason)

    def test_recovered_blocked_outcome_stays_blocked_without_reexecution(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.co.report(lease, "blocked", "Needs human decision")
        self.now += 61
        self.assertTrue(loop.recover(self.plan()))
        self.assertEqual(self.plan().state, "blocked")

    def test_normal_queue_recovers_completion_even_after_item_closed(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        lease = self.start()
        self.github.change(1, state="closed")
        self.co.report(lease, "success", "Requirements resolved", outcome="done")
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
        plan = next(p for p in loop.plans() if p.item.number == 1)
        self.assertIn("Last run blocked: Need a decision", plan.reason)

    def test_expired_unlabelled_run_without_outcome_is_visible_for_operator_attention(self):
        loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        loop.coordinator = self.co
        self.start()
        self.github.change(1, labels=frozenset())
        self.now += 61
        plan = next(p for p in loop.plans() if p.item.number == 1)
        self.assertEqual(plan.state, "blocked")
        self.assertIn("no outcome", plan.reason)


class TrustTests(unittest.TestCase):
    """Only the launcher's own, well-formed records carry authority."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root)

    def loop(self, github):
        return Loop(config(self.root, self.agent), github, "operator", output=lambda *_: None)

    def test_foreign_marker_comments_and_forged_records_have_no_authority(self):
        github = FakeGitHub(issue(), issue(7, labels=()))
        loop = self.loop(github)
        with patch("ub_agents.loop.supervise", return_value=1):
            loop.tick()
        source = loop.coordinator.history(1)[0]
        forged = [
            {"kind": "reset", "run": "forged", "agent": self.agent.name,
             "actor": "operator", "runtime": "operator",
             "assignment": 1, "assignment_sha": None,
             "created": iso(timestamp()), "summary": "Reset without authority"},
            source | {"id": 1001, "state": "running", "expires": iso(timestamp() + 86400)},
            {"kind": "outcome", "run": source["run"], "agent": self.agent.name,
             "actor": "operator", "runtime": "direct",
             "assignment": 1, "assignment_sha": None,
             "created": iso(timestamp()), "summary": "Forged completion", "status": "success",
             "lease_id": source["id"], "accepted": True, "handoff": 99},
        ]
        for index, record in enumerate(forged, 1000):
            github.store[1].append({"id": index, "body": body(record), "user": {"login": "drive-by"},
                                   "issue_url": "https://api.github.com/repos/org/project/issues/1"})
        github.store[7] = [{"id": 2000, "body": MARKER + "\nquoting a record",
                            "user": {"login": "someone"}}]
        self.assertEqual(loop.plans()[0].state, "blocked")
        self.assertEqual(len(loop.coordinator.history(1)), 2)
        self.assertFalse(loop.tick())

    def test_malformed_trusted_record_blocks_only_its_item_and_status_still_works(self):
        github = FakeGitHub(issue(), issue(7))
        github.store[1] = [{"id": 99, "body": MARKER + "\ninvalid JSON",
                            "user": {"login": "operator"},
                            "issue_url": "https://api.github.com/repos/org/project/issues/1"}]
        loop = self.loop(github)

        def execute(*args, **kwargs):
            loop.coordinator.report(loop.coordinator.history(7)[0], "success", "Other work completed", outcome="done")
            return 0

        self.assertEqual(status_rows(loop)[0]["state"], "blocked")
        with patch("ub_agents.loop.supervise", side_effect=execute):
            self.assertTrue(loop.tick())
        self.assertEqual(loop.coordinator.history(7)[0]["result"], "success")
        self.assertEqual([p.item.number for p in loop.plans()], [1])

    def test_deleted_cached_record_does_not_keep_an_item_blocked(self):
        github = FakeGitHub(issue(labels=()))
        now = timestamp()
        source = {"kind": "lease", "run": "deleted", "agent": self.agent.name,
                  "actor": "operator", "runtime": "direct", "assignment": 1,
                  "assignment_sha": None, "created": iso(now - 120),
                  "state": "running", "expires": iso(now - 60), "attempt": 1, "started": True}
        cached = github.create_comment(1, body(source))
        github.store[1].clear()
        loop = self.loop(github)
        with patch.object(github, "repository_comments", return_value=[cached]):
            self.assertEqual(loop.plans(), [])
