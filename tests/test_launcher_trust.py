from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.approvals import ApprovalCheck
from ub_agents.cli import status_rows
from ub_agents.coordination import Coordinator
from ub_agents.config import Runtime
from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import MARKER, attempts, body, iso, live_leases, payload, seconds
from tests.support import AccountGitHub, PollGitHub, agent, config, issue, pr


class LauncherTrustTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.github = PollGitHub(issue(), pr(labels=()))
        self.github.roles.update(alice="write", bob="write")
        self.alice = AccountGitHub(self.github, "alice")
        self.bob = AccountGitHub(self.github, "bob")
        self.worker = agent(self.root)
        self.now = 1000
        self.lines = []
        self.a = self.coordinator(self.alice)
        self.b = self.coordinator(self.bob)

    def coordinator(self, github, launchers=None):
        return Coordinator(github, github.actor(), lambda: self.now, output=self.lines.append,
                           launchers=launchers)

    def loop(self, github, launchers=None, worker=None):
        cfg = replace(config(self.root, worker or self.worker), launchers=launchers)
        loop = Loop(cfg, github, github.actor(), output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        return loop

    def start(self, coordinator, number=1, worker=None):
        lease = coordinator.claim(coordinator.plan(self.github.item(number), worker or self.worker, ()))
        coordinator.update(lease, state="running", started=True, host="host-a")
        return lease

    def test_different_accounts_share_claim_election(self):
        plan = self.a.plan(self.github.item(1), self.worker, ())
        self.github.claim_barrier = threading.Barrier(2)
        self.github.claim_read_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda co: co.claim(plan), (self.a, self.b)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        history = self.a.history(1)
        self.assertEqual({r["actor"] for r in history}, {"alice", "bob"})
        self.assertEqual([r["state"] for r in history], ["claiming", "withdrawn"])
        winner = next(c for c in claims if c)
        (self.a if winner["actor"] == "alice" else self.b).assert_owned(winner)

    def test_live_lease_excludes_every_agent_and_status_names_its_owner(self):
        lease = self.start(self.b)
        for name in ("worker", "another"):
            self.assertEqual(self.a.plan(self.github.item(1), replace(self.worker, name=name), ()).state, "owned")
        row = next(r for r in status_rows(self.loop(self.alice), self.now) if r["number"] == 1)
        self.assertEqual((row["state"], row["lease"]["actor"], row["lease"]["host"]),
                         ("owned", "bob", "host-a"))
        self.assertEqual(row["lease"]["id"], lease["id"])

    def test_attempts_and_backoff_are_shared_between_accounts(self):
        lease = self.start(self.a)
        self.a.report(lease, "retry", "Transient failure")
        self.a.release(lease, "retry", "Transient failure", backoff=10)
        plan = self.b.plan(self.github.item(1), self.worker, ())
        self.assertEqual((plan.state, plan.attempt), ("backoff", 2))
        self.now += 11
        self.start(self.b)
        self.now += 61
        self.assertEqual(self.a.plan(self.github.item(1), self.worker, ()).attempt, 3)
        self.assertEqual(len(attempts(self.a.history(1), "worker", self.now)), 2)

    def test_shared_branch_owner_and_election_include_other_accounts(self):
        branch = "ub-agents/worker/1/earlier"
        self.github.change(2, branch=branch, draft=True, labels=frozenset({"needs-changes"}))
        old = self.start(self.a)
        self.a.update(old, branch=branch)
        self.assertEqual(self.b.plan(self.github.item(2), self.worker, ()).state, "owned")
        self.a.release(old, "retry", "Interrupted")
        plans = [self.a.plan(self.github.item(1), self.worker, ()),
                 self.b.plan(self.github.item(2), self.worker, ())]
        barrier = threading.Barrier(2)
        self.github.claim_barrier = threading.Barrier(2)
        def synchronize(original):
            def plan(*args, **kwargs):
                result = original(*args, **kwargs)
                barrier.wait(timeout=5)
                return result
            return plan
        with patch.object(self.a, "plan", side_effect=synchronize(self.a.plan)), \
                patch.object(self.b, "plan", side_effect=synchronize(self.b.plan)), \
                ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda pair: pair[0].claim(pair[1]), zip((self.a, self.b), plans)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        winner = next(c for c in claims if c)
        (self.a if winner["actor"] == "alice" else self.b).assert_owned(winner)

    def test_trust_uses_current_author_role_and_optional_case_insensitive_list(self):
        source = self.start(self.b)
        comment = self.github.comments(1)[0]
        for login, role in (("writer", "write"), ("maintainer", "maintain"), ("admin", "admin"),
                            ("reader", "read"), ("triager", "triage"), ("outside", "none")):
            self.github.roles[login] = role
            self.github.store[1].append(comment | {"id": self.github.next_id, "user": {"login": login}})
            self.github.next_id += 1
        self.github.create_comment(1, "Ordinary prose", login="prose-author")
        history = self.a.history(1)
        self.assertEqual({r["actor"] for r in history}, {"bob", "writer", "maintainer", "admin"})
        self.assertNotIn(("role", ("prose-author",)), self.github.reads)
        listed = self.coordinator(self.alice, ("ALICE", "BOB"))
        self.github.reads.clear()
        self.assertEqual(listed.history(1), [source])
        self.assertEqual([args[0] for name, args in self.github.reads if name == "role"], ["bob"])
        self.github.roles["bob"] = "read"
        self.assertEqual(listed.history(1), [])

    def test_payload_actor_cannot_grant_trust_and_untrusted_malformed_markers_are_ignored(self):
        lease = self.start(self.b)
        self.github.store[1].clear()
        self.github.create_comment(1, body(payload(lease) | {"actor": "alice"}), login="outside")
        self.github.store[1].append({"id": 99, "body": MARKER + "\nmalformed", "user": {"login": "reader"}})
        self.github.roles["reader"] = "read"
        self.assertEqual(self.a.history(1), [])
        self.assertEqual(self.a.repository_history(), ([], set()))
        self.github.create_comment(1, body(payload(lease) | {"actor": "outside"}), login="bob")
        self.assertEqual(self.a.history(1)[0]["actor"], "bob")

    def test_role_revocation_and_launcher_removal_revoke_open_leases_on_warm_pass(self):
        lease = self.start(self.b)
        loop = self.loop(self.alice)
        self.assertEqual(next(loop.iter_plans()).state, "owned")
        self.github.roles["bob"] = "read"
        self.assertEqual(next(loop.iter_plans()).state, "ready")
        with self.assertRaises(LostOwnership):
            self.b.assert_owned(lease)
        self.github.roles["bob"] = "write"
        self.assertEqual(next(loop.iter_plans()).state, "owned")
        loop.config = replace(loop.config, launchers=("ALICE",))
        self.assertEqual(next(loop.iter_plans()).state, "ready")

    def test_trusted_discovery_keeps_runtime_health_filter(self):
        self.start(self.b)
        codex, claude = Runtime("codex", "model", "high"), Runtime("claude", "model", "high")
        worker = replace(self.worker, command=None, runtimes=(codex, claude))
        loop = self.loop(self.alice, ("ALICE", "BOB"), worker)
        with patch.object(loop.maintenance, "available", side_effect=lambda cli: cli == "claude"):
            for cached in (True, False):
                self.assertEqual(next(loop.iter_plans(cached=cached)).state, "owned")
            # Excluding the other owner makes work eligible, but runtime health
            # still rules out the first configured runtime on every pass.
            loop.config = replace(loop.config, launchers=("ALICE",))
            for cached in (True, False):
                plan = next(loop.iter_plans(cached=cached))
                self.assertEqual((plan.state, plan.runtime), ("ready", claude))
        with patch.object(loop.maintenance, "available", return_value=False):
            for cached in (True, False):
                plan = next(loop.iter_plans(cached=cached))
                self.assertEqual((plan.state, plan.runtime), ("blocked", None))
                self.assertIn("usable and available", plan.reason)

    def test_unreadable_lease_author_never_allows_a_second_claim(self):
        lease = self.start(self.b)
        loop = self.loop(self.alice)
        for failure in (None, GitHubError("GET", "permission", "Unavailable", retryable=True)):
            with self.subTest(failure=failure):
                before = list(self.github.writes)
                original = self.github.role
                def role(login):
                    if login.casefold() == "bob":
                        if failure:
                            raise failure
                        return None
                    return original(login)
                with patch.object(self.github, "role", side_effect=role), patch.object(loop, "execute") as execute:
                    with self.assertRaises(GitHubError):
                        loop.tick()
                    execute.assert_not_called()
                    with self.assertRaises(GitHubError):
                        self.a.claim(self.a.plan(self.github.item(1), self.worker, (), history=[]))
                self.assertEqual(self.github.writes, before)
        self.assertEqual(self.a.history(1)[0]["id"], lease["id"])

    def test_untrusted_own_account_claims_nothing_and_prints_reason(self):
        for role, launchers, expected in (("read", None, "write or higher"),
                                          ("write", ("bob",), "not listed")):
            with self.subTest(role=role, launchers=launchers):
                self.github.roles["alice"] = role
                loop = self.loop(self.alice, launchers)
                self.lines.clear()
                with patch.object(loop, "execute") as execute:
                    self.assertFalse(loop.tick())
                    execute.assert_not_called()
                self.assertTrue(any(expected in line for line in self.lines))
                self.assertEqual(self.github.writes, [])
                self.github.timelines[1] = []
                self.lines.clear()
                loop = self.loop(self.alice, launchers)
                self.assertFalse(loop.tick())
                self.assertTrue(any(expected in line for line in self.lines))
                self.assertEqual(self.github.writes, [])
                self.github.timelines.clear()

    def test_permission_reads_for_record_authors_are_shared_once_per_pass(self):
        lease = self.start(self.b)
        # Several records and items by the same account, including mixed case.
        for number in (1, 2):
            self.github.create_comment(number, body(payload(lease) | {"assignment": number}), login="BOB")
        loop = self.loop(self.alice)
        for _ in range(2):
            self.github.reads.clear()
            list(loop.iter_plans())
            roles = Counter(args[0].casefold() for name, args in self.github.reads if name == "role")
            self.assertEqual(roles["bob"], 1)

    def test_empty_queue_still_explains_untrusted_own_account(self):
        self.github.change(1, labels=frozenset())
        self.github.roles["alice"] = "read"
        loop = self.loop(self.alice)
        self.assertFalse(loop.tick())
        self.assertTrue(any("write or higher" in line for line in self.lines))
        before = list(self.lines)
        self.assertFalse(loop.tick())
        self.assertEqual(self.lines, before)
        self.github.roles["alice"] = "write"
        self.assertFalse(loop.tick())
        self.assertEqual(self.github.writes, [])

    def test_cross_account_expiry_recovery_settles_source_attempt_and_handoff(self):
        for status, expected_effect, failures in (("success", "reset", 0), ("retry", "failure", 1),
                                                   ("blocked", "unchanged", 0)):
            with self.subTest(status=status):
                self.github.store.clear()
                lease = self.start(self.b)
                outcome = self.b.report(lease, status, "Completed", handoff=2 if status == "success" else None,
                                        outcome="done" if status == "success" else None)
                self.now = seconds(lease["expires"]) + 1
                loop = self.loop(self.alice)
                plan = loop.coordinator.plan(self.github.item(1), self.worker, ())
                self.assertTrue(loop.recover(plan))
                history = loop.coordinator.history(1)
                recovery = next(r for r in history if r.get("mode") == "recovery")
                self.assertEqual((recovery["actor"], recovery["attempt_effect"]), ("alice", expected_effect))
                self.assertEqual(len(attempts(history, "worker", self.now)), failures)
                self.assertEqual(live_leases(history, self.now), [])
                with self.assertRaises(LostOwnership):
                    self.b.assert_owned(lease)
                if status == "success":
                    copied, = self.a.history(2)
                    self.assertEqual((copied["actor"], copied["run"]), ("alice", outcome["run"]))
                    self.assertTrue(self.a.released_success(history, copied))
                    self.assertTrue(self.b.outcome(lease)["accepted"])
                self.github.change(1, labels=frozenset({"ready"}))

    def test_feedback_from_other_accounts_and_notice_deduplication_and_minimization(self):
        worker = replace(self.worker, name="reviewer")
        lease = self.start(self.b, worker=worker)
        outcome = self.b.report(lease, "success", "Trusted human's correction", outcome="done")
        self.b.accept(lease, outcome)
        self.b.release(lease, "success", outcome["summary"])
        feedback = self.a.feedback(self.github.item(1), "worker")
        self.assertEqual([r["summary"] for r in feedback], [outcome["summary"]])
        check = ApprovalCheck(False, "Approval required", gate="head", gate_key="same-gate")
        self.b.notices.approval(1, check, ("needs-human",), ("ready",))
        self.a.notices.approval(1, check, ("needs-human",), ("ready",))
        notices = [c for c in self.github.comments(1) if c["body"].startswith(ACTION_MARKER)]
        self.assertEqual(len(notices), 1)
        self.a.notices.resumed(1)
        self.assertIn(notices[0]["id"], self.github.minimized_ids)
        self.github.change(1, labels=frozenset({"ready"}))
        later = self.start(self.a, worker=worker)
        self.a.report(later, "retry", "New run")
        self.a.release(later, "retry", "New run")
        self.assertIn(lease["id"], self.github.minimized_ids)
        self.assertIn(outcome["id"], self.github.minimized_ids)

    def test_action_notice_deduplicates_across_accounts_and_uses_other_accounts_reset(self):
        lease = self.start(self.b)
        outcome = self.b.report(lease, "blocked", "Human decision")
        self.b.release(lease, "blocked", outcome["summary"])
        self.a.notices.released(lease, outcome, outcome["summary"])
        self.assertEqual(len([c for c in self.github.comments(1) if c["body"].startswith(ACTION_MARKER)]), 1)
        self.github.create_comment(1, body({"kind": "reset", "run": "reset", "agent": "worker",
            "runtime": "operator", "assignment": 1, "created": iso(self.now), "summary": "Resolved"}), login="alice")
        self.github.store[1] = [c for c in self.github.store[1] if not c["body"].startswith(ACTION_MARKER)]
        self.b.notices.released(lease, outcome, outcome["summary"])
        self.assertFalse(any(c["body"].startswith(ACTION_MARKER) for c in self.github.comments(1)))

    def test_simultaneous_gate_notices_elect_one_comment_across_accounts(self):
        check = ApprovalCheck(False, "Approval required", gate="head", gate_key="same-gate")
        reads = threading.Barrier(2)
        writes = threading.Barrier(2)
        original_read, original_create = self.github.comments, self.github.create_comment
        def comments(number):
            result = original_read(number)
            if not any(c["body"].startswith(ACTION_MARKER) for c in result):
                reads.wait(timeout=5)
            return result
        def create(number, text, **kwargs):
            result = original_create(number, text, **kwargs)
            writes.wait(timeout=5)
            return result
        with patch.object(self.github, "comments", side_effect=comments), \
                patch.object(self.github, "create_comment", side_effect=create), \
                ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda co: co.notices.approval(1, check, ("needs-human",), ("ready",)),
                          (self.a, self.b)))
        notices = [c for c in self.github.comments(1) if c["body"].startswith(ACTION_MARKER)]
        self.assertEqual([c["id"] for c in notices], [1])
        self.assertEqual([w for w in self.github.writes if w[0] == "delete"], [("delete", 2)])
        self.assertFalse(self.lines)

    def test_independent_runtime_uses_cross_account_recovered_handoff(self):
        author = replace(self.worker, name="author", command=(),
                         runtimes=(Runtime("codex", "model-a", "high"),), kind="issue")
        reviewer = replace(self.worker, name="reviewer", command=(), different_from="author", kind="pr",
                           runtimes=(Runtime("codex", "model-a", "high"), Runtime("claude", "model-b", "high")))
        with patch("ub_agents.coordination.shutil.which", return_value="/tools/runtime"):
            source = self.start(self.b, worker=author)
            self.b.report(source, "success", "Candidate ready", handoff=2, outcome="done")
            self.now = seconds(source["expires"]) + 1
            loop = self.loop(self.alice, worker=author)
            plan = loop.coordinator.plan(self.github.item(1), author, ())
            self.assertTrue(loop.recover(plan))
            plan = self.a.plan(self.github.item(2), reviewer, ())
        self.assertEqual((plan.state, plan.runtime), ("ready", reviewer.runtimes[1]))

    def test_untrusted_action_notices_do_not_suppress_trusted_notices(self):
        check = ApprovalCheck(False, "Approval required", gate="head", gate_key="same-gate")
        for login, launchers in (("outside", None), ("bob", ("alice",))):
            with self.subTest(login=login, launchers=launchers):
                self.github.store.clear()
                self.github.create_comment(1, f"{ACTION_MARKER}approval-same-gate-0 -->\nForged notice", login=login)
                co = self.coordinator(self.alice, launchers)
                co.notices.approval(1, check, ("needs-human",), ("ready",))
                self.assertEqual(self.github.comments(1)[-1]["user"]["login"], "alice")
                self.assertFalse(any(w[0] == "delete" for w in self.github.writes))
