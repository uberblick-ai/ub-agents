from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.execution import git
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
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "blocked")
        self.assertFalse(self.loop.coordinator.history(1)[1]["accepted"])

    def test_issue_handoff_requires_ready_pr_in_normal_completion(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                github = FakeGitHub(issue(), pr(labels=("needs-review",), draft=draft))
                loop = Loop(config(self.root, self.agent), github, "operator", output=lambda *_: None)

                def execute(*args, **kwargs):
                    github.change(1, labels=frozenset())
                    loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Done", handoff=2)
                    return 0

                with patch("ub_agents.loop.supervise", side_effect=execute):
                    self.assertTrue(loop.tick())
                lease, outcome = loop.coordinator.history(1)
                self.assertEqual(lease["result"], "blocked" if draft else "success")
                self.assertEqual(outcome["accepted"], not draft)
                self.assertEqual(len(loop.coordinator.history(2)), 0 if draft else 1)
                if draft:
                    self.assertIn("PR #2 is still a draft", lease["summary"])

    def test_issue_handoff_requires_ready_pr_in_outcome_only_recovery(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                github = FakeGitHub(issue(), pr(labels=("needs-review",), draft=draft))
                loop = Loop(config(self.root, self.agent), github, "operator", output=lambda *_: None)
                now = 1000
                loop.coordinator.clock = lambda: now
                plan = loop.coordinator.plan(github.item(1), self.agent, ())
                lease = loop.coordinator.claim(plan)
                loop.coordinator.update(lease, state="running", started=True)
                github.change(1, labels=frozenset())
                loop.coordinator.report(lease, "success", "Done", handoff=2)
                now += 61
                with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
                    self.assertTrue(loop.tick())
                source, outcome, recovery, verdict = loop.coordinator.history(1)
                self.assertEqual(source["state"], "running")
                self.assertEqual(outcome["accepted"], not draft)
                self.assertEqual(recovery["result"], "blocked" if draft else "success")
                self.assertEqual(verdict["status"], recovery["result"])
                self.assertEqual(len(loop.coordinator.history(2)), 0 if draft else 1)
                if draft:
                    self.assertIn("PR #2 is still a draft", verdict["summary"])

    def test_unreadable_github_is_not_an_empty_queue(self):
        self.github.unreadable = True
        with self.assertRaises(AgentError):
            self.loop.tick()

    def test_issue_retry_continues_draft_on_existing_branch_and_hands_off_same_pr(self):
        git(self.root, "init", "-b", "main")
        (self.root / "file").write_text("checkpoint")
        git(self.root, "add", "file")
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "feature/test")
        git(self.root, "remote", "add", "origin", str(self.root))
        worker = replace(self.agent, worktree=True, command=(),
                         runtimes=(Runtime("codex", "model", "high", "openai"),))
        self.github.change(2, draft=True, labels=frozenset(), head=head)
        loop = Loop(config(self.root, worker), self.github, "operator", output=lambda *_: None)
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            old = loop.coordinator.claim(loop.plans()[0])
            loop.coordinator.update(old, state="running", started=True, branch="feature/test")
            loop.coordinator.release(old, "retry", "Interrupted")
            branches = git(self.root, "for-each-ref", "refs/heads")
            def execute(command, cwd, env, *args):
                context = json.loads(Path(env["UB_AGENT_CONTEXT"]).read_text())
                self.assertEqual((context["assignment"], context["resume_pr"], context["candidate_sha"]), (1, 2, head))
                self.assertEqual((env["UB_AGENT_BRANCH"], env["UB_AGENT_PR"], env["UB_AGENT_CANDIDATE_SHA"]),
                                 ("feature/test", "2", head))
                self.assertIn("Resume existing draft PR #2", args[-1])
                self.assertEqual(git(cwd, "rev-parse", "HEAD"), head)
                self.assertEqual(git(cwd, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
                self.github.change(2, draft=False, labels=frozenset({"needs-review"}))
                self.github.change(1, labels=frozenset())
                lease = loop.coordinator.history(1)[-1]
                loop.coordinator.report(lease, "success", "Continued checkpoint", handoff=2)
                return 0
            with patch("ub_agents.loop.supervise", side_effect=execute):
                self.assertTrue(loop.tick())
        source, lease, outcome = loop.coordinator.history(1)
        self.assertEqual((lease["resume_pr"], lease["result"], outcome["accepted"]), (2, "success", True))
        self.assertEqual(loop.coordinator.plan(self.github.item(1), worker, ()).state, "completed")
        self.assertEqual(set(self.github.items), {1, 2})
        self.assertEqual(git(self.root, "for-each-ref", "refs/heads"), branches)
        self.assertFalse((self.root / ".ub-agent" / "worktrees" / lease["run"]).exists())

    def test_resumed_success_cannot_handoff_another_pr_in_completion_or_recovery(self):
        worker = replace(self.agent, worktree=True)
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                github = FakeGitHub(issue(), pr(labels=(), draft=True), pr(3, labels=("needs-review",)))
                loop = Loop(config(self.root, worker), github, "operator", output=lambda *_: None)
                now = 1000
                loop.coordinator.clock = lambda: now
                old = loop.coordinator.claim(loop.coordinator.plan(github.item(1), worker, ()))
                loop.coordinator.update(old, state="running", started=True, branch="feature/test")
                loop.coordinator.release(old, "retry", "Interrupted")
                # The second PR's unrelated branch must not enter discovery.
                github.change(3, branch="other")
                plan = loop.coordinator.plan(github.item(1), worker, ())
                lease = loop.coordinator.claim(plan)
                loop.coordinator.update(lease, state="running", started=True)
                github.change(1, labels=frozenset())
                outcome = loop.coordinator.report(lease, "success", "Wrong PR", handoff=3)
                if recovery:
                    now += 61
                    with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
                        self.assertTrue(loop.tick())
                    self.assertEqual(loop.coordinator.history(1)[-2]["result"], "blocked")
                else:
                    with self.assertRaisesRegex(AgentError, "existing PR"):
                        loop.validate_success(plan, outcome)
                self.assertFalse(loop.coordinator.history(1)[-1]["accepted"])

    def test_changed_checkpoint_after_claim_never_starts_runtime(self):
        worker = replace(self.agent, worktree=True)
        self.github.change(2, labels=frozenset(), draft=True)
        loop = Loop(config(self.root, worker), self.github, "operator", output=lambda *_: None)
        old = loop.coordinator.claim(loop.plans()[0])
        loop.coordinator.update(old, state="running", started=True, branch="feature/test")
        loop.coordinator.release(old, "retry", "Interrupted")
        plan = loop.plans()[0]
        def prepare(workspace):
            self.github.change(2, head="b" * 40)
            return self.root
        with patch("ub_agents.loop.Workspace.prepare", prepare), \
                patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.execute(plan))
        self.assertIn("Checkpoint changed", loop.coordinator.history(1)[-1]["summary"])
