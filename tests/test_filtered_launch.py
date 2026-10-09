from copy import deepcopy
from dataclasses import replace
import io
import threading
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Priority, Queue, Runtime
from ub_agents.github import Dependency
from ub_agents.loop import Loop
from tests.support import AccountGitHub, PollGitHub, agent, config, issue
from tests import test_launch


class FilteredLaunchTests(unittest.TestCase):
    # Reuse the recording CLI fixture, including real claim and completion paths.
    launch = test_launch.TargetedLaunchTests.launch
    coordinator = test_launch.TargetedLaunchTests.coordinator
    report_success = test_launch.TargetedLaunchTests.report_success

    def setUp(self):
        test_launch.TargetedLaunchTests.setUp(self)
        self.selected = agent(self.root, name="triage", kind="issue", triggers=("prepare", "revise"))
        self.other = agent(self.root, name="builder", triggers=("ready",))
        self.config = config(self.root, self.other, self.selected)
        self.github = PollGitHub(issue(1), issue(11, labels=("prepare",)))

    def test_once_claims_only_selected_agent_and_limits_post_run_observations(self):
        self.github.change(11, labels=frozenset({"prepare", "ready"}))
        before = self.github.items[1]
        code, stdout, _, run = self.launch("--once", "--agent", "triage", "--no-ui")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertIn("Serving queue for agent triage", stdout)
        self.assertEqual(self.github.items[1], before)
        self.assertNotIn(1, self.github.store)
        history = self.coordinator().history(11)
        self.assertEqual({r["agent"] for r in history}, {"triage"})
        snapshot = self.loop.observer.publisher.snapshots[-1]
        self.assertEqual(snapshot["queue_agent"], "triage")
        self.assertTrue(all(row["agent"] == "triage" for row in snapshot["latest_pass"]["rows"]))
        self.assertEqual(self.github.items[11].labels, frozenset({"ready"}))

    def test_unknown_agent_lists_names_before_github_is_constructed(self):
        stderr = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub") as github, redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            main(self.argv + ["--agent", "missing"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("configured agents: builder, triage", stderr.getvalue())
        github.assert_not_called()

    def test_empty_filtered_once_names_only_selected_trigger_labels(self):
        self.github.change(11, labels=frozenset())
        code, stdout, _, run = self.launch("--once", "--agent", "triage")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("Agent triage: no open issue or PR has a trigger label (prepare, revise)", stdout)
        self.assertNotIn("prepared", stdout)
        self.assertEqual(self.github.writes, [])

    def test_waiting_once_counts_each_reason_without_claiming_other_work(self):
        self.github = PollGitHub(issue(1), issue(11, labels=("prepare", "needs-human")),
                                 issue(12, labels=("prepare", "needs-human")),
                                 issue(13, labels=("prepare",)))
        self.github.dependencies[13] = [Dependency("org/project", 1, "open")]
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("2 parked — Stop label needs-human is present", stdout)
        self.assertIn("1 parked — Waiting for blockers #1", stdout)
        self.assertEqual(self.github.writes, [])

    def test_queue_priority_and_milestone_policies_apply_to_selected_work(self):
        for policy, expected in (("ignore", 12), ("order", 12), ("prefer", 11), ("gate", None)):
            with self.subTest(policy=policy):
                self.github = PollGitHub(issue(1, milestone=3),
                                         issue(11, labels=("prepare",), milestone=4),
                                         issue(12, labels=("prepare", "urgent"), milestone=5))
                self.github.milestones = [
                    {"number": n, "state": "open", "created_at": f"2026-01-0{n}T00:00:00Z"}
                    for n in (3, 4, 5)]
                self.config = config(self.root, self.other, self.selected,
                                     queue=Queue(priority=Priority(("urgent",)), milestones=policy))
                _, stdout, _, run = self.launch("--once", "--agent", "triage")
                if expected is None:
                    run.assert_not_called()
                    self.assertIn("Waiting for active milestone #3", stdout)
                    self.assertEqual(self.github.writes, [])
                else:
                    run.assert_called_once()
                    self.assertEqual(int(run.call_args.args[2]["UB_AGENTS_ASSIGNMENT"]), expected)
                self.assertNotIn(1, self.github.store)

    def test_filtered_plans_keep_approval_and_ownership_gates(self):
        self.github = PollGitHub(issue(11, labels=("prepare",)), issue(12, labels=("prepare",)),
                                 issue(13, labels=("prepare", "needs-human")), issue(1))
        self.github.timelines[11] = []
        peer = self.coordinator()
        lease = peer.claim(peer.plan(self.github.items[12], self.selected, ()))
        peer.update(lease, state="running", started=True)
        baseline = self.github.writes[:]
        plain = Loop(self.config, self.github, "operator", output=lambda *_: None)
        filtered = Loop(self.config, self.github, "operator", output=lambda *_: None)
        filtered._launch_agent = "triage"
        plain.coordinator.clock = filtered.coordinator.clock = lambda: self.now
        with patch.object(plain.maintenance, "available", return_value=True), \
                patch.object(filtered.maintenance, "available", return_value=True):
            expected = [p for p in plain.plans() if p.agent.name == "triage"]
            actual = filtered.plans()
        self.assertEqual(actual, expected)
        self.assertEqual({p.item.number: p.state for p in actual}, {11: "parked", 12: "owned", 13: "parked"})
        self.assertEqual(self.github.writes, baseline)
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("1 owned", stdout)
        self.assertIn("No maintainer", stdout)
        self.assertNotIn(1, self.github.store)

    def test_attempt_limit_and_backoff_are_preserved(self):
        self.selected = replace(self.selected, backoff_seconds=120, max_backoff_seconds=120)
        self.config = config(self.root, self.other, self.selected)
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.selected, ()))
        co.update(lease, state="running", started=True)
        co.release(lease, "retry", "Try again later", backoff=120)
        baseline = self.github.writes[:]
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("1 backoff — Durable retry backoff has not elapsed", stdout)
        self.assertEqual(self.github.writes, baseline)
        self.selected = replace(self.selected, max_attempts=1)
        self.config = config(self.root, self.other, self.selected)
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("1 blocked — Attempt limit exhausted", stdout)
        self.assertNotIn(1, self.github.store)

    def test_recovers_selected_outcome_and_leaves_other_closed_recovery_untouched(self):
        co = self.coordinator()
        leases = []
        for number, role in ((1, self.other), (11, self.selected)):
            lease = co.claim(co.plan(self.github.items[number], role, ()))
            co.update(lease, state="running", started=True)
            co.report(lease, "retry", "Completed before crash")
            leases.append(lease)
            self.github.change(number, state="closed", labels=frozenset())
        self.now += 61
        other_history = deepcopy(self.github.store[1])
        self.github.reads.clear()
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("#11 triage: recovered durable outcome", stdout)
        self.assertEqual(self.github.store[1], other_history)
        self.assertFalse(any(name in {"item", "comments"} and args[0] == 1
                             for name, args in self.github.reads))
        history = co.history(11)
        self.assertEqual(next(r for r in history if r["kind"] == "outcome")["status"], "retry")
        self.assertTrue(any(r.get("recovered_lease_id") == leases[1]["id"] and r["state"] == "released"
                            for r in history))

    def test_other_agents_missing_blocked_notice_is_not_reconciled_on_shared_item(self):
        self.github.change(11, labels=frozenset({"prepare", "ready"}))
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.other, ()))
        co.update(lease, state="running", started=True)
        co.report(lease, "blocked", "Needs a person", action=["Maintainer: decide the approach."])
        co.release(lease, "blocked", "Needs a person")
        self.github.change(11, labels=frozenset({"prepare", "needs-human"}))
        baseline = self.github.writes[:]
        _, _, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertEqual(self.github.writes, baseline)

    def test_pending_success_recovers_without_reexecution(self):
        co = self.coordinator()
        lease = co.claim(co.plan(self.github.items[11], self.selected, ()))
        co.update(lease, state="running", started=True)
        co.report(lease, "success", "Completed before crash", outcome="done")
        self.now += 61
        _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("#11 triage: recovered durable outcome", stdout)
        self.assertTrue(next(r for r in co.history(11) if r["kind"] == "outcome")["accepted"])

    def test_fresh_replan_does_not_bypass_peer_ownership(self):
        def refresh(*_, on_fetch=None):
            peer = self.coordinator()
            lease = peer.claim(peer.plan(self.github.items[11], self.selected, ()))
            peer.update(lease, state="running", started=True)
        _, stdout, _, run = self.launch("--once", "--agent", "triage", refresh=refresh)
        run.assert_not_called()
        self.assertIn("owned", stdout)
        self.assertNotIn(1, self.github.store)

    def test_claim_election_preserves_peer_winner_without_starting_execution(self):
        self.github.roles["peer"] = "write"
        create_comment = self.github.create_comment
        def create(number, content, *, login=None):
            if login is None:
                peer = self.coordinator(AccountGitHub(self.github, "peer"))
                lease = peer.claim(peer.plan(self.github.items[11], self.selected, ()))
                peer.update(lease, state="running", started=True)
            return create_comment(number, content, login=login)
        with patch.object(self.github, "create_comment", side_effect=create):
            _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("Claim election lost", stdout)
        history = self.coordinator().history(11)
        self.assertEqual({r["agent"] for r in history}, {"triage"})
        self.assertEqual([r["actor"] for r in history if r["state"] == "running"], ["peer"])
        self.assertNotIn(1, self.github.store)

    def test_runtime_availability_pause_and_health_checks_still_gate_selected_work(self):
        self.selected = replace(self.selected, command=(), runtimes=(Runtime("codex", "model", "high"),))
        self.config = config(self.root, self.other, self.selected)
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", side_effect=lambda cli: cli == "gh"):
            _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("1 blocked — No eligible runtime executable", stdout)
        self.assertEqual(self.github.writes, [])
        pause = {"reason": "usage limit reached", "ends_at": "2026-10-10T00:00:00Z"}
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", return_value=True), \
                patch("ub_agents.runtime_usage.RuntimeUsage.paused", return_value=pause):
            _, stdout, _, run = self.launch("--once", "--agent", "triage")
        run.assert_not_called()
        self.assertIn("1 waiting — Waiting for codex: usage limit reached", stdout)
        self.assertEqual(self.github.writes, [])
        self.selected = agent(self.root, name="triage", triggers=("prepare",), health_check=("check-health",))
        self.config = config(self.root, self.other, self.selected)
        with patch("ub_agents.agent_health.run_check", return_value="Unavailable") as health:
            _, stdout, _, run = self.launch("--once", "--agent", "triage")
        health.assert_called_once()
        run.assert_not_called()
        self.assertIn("1 waiting — Health check check-health: Unavailable", stdout)
        self.assertEqual(self.github.writes, [])

    def test_reload_removing_selected_agent_claims_nothing_and_explains_it(self):
        def refresh(*_, on_fetch=None):
            self.config = config(self.root, self.other)
        _, stdout, _, run = self.launch("--once", "--agent", "triage", refresh=refresh)
        run.assert_not_called()
        self.assertIn("Agent triage is no longer configured; claiming no work", stdout)
        self.assertEqual(self.github.writes, [])

    def test_continuous_launch_keeps_polling_after_selected_work_finishes(self):
        lines = []
        self.loop = Loop(self.config, self.github, "operator", output=lines.append,
                         interrupt_event=threading.Event())
        waits = []
        def wait(*_):
            waits.append(True)
            if len(waits) == 2:
                self.loop.stop_event.set()
        with patch("ub_agents.loop.supervise", side_effect=self.report_success) as run, \
                patch("ub_agents.loop.refresh_instructions", return_value="Instructions"), \
                patch("ub_agents.loop.monotonic", return_value=0), \
                patch.object(self.loop, "_wait", side_effect=wait):
            self.loop.launch(agent_name="triage")
        run.assert_called_once()
        self.assertEqual(len(waits), 2)
        self.assertTrue(any("no open issue or PR has a trigger label (prepare, revise)" in line for line in lines))
        self.assertNotIn(1, self.github.store)

    def test_filtered_foreground_stop_drains_supervised_run_and_closes_renewal(self):
        def finish(*args, **kwargs):
            self.loop.stop_gracefully()
            return self.report_success(*args, **kwargs)
        code, _, _, run = self.launch("--agent", "triage", execute=finish)
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertIsNone(self.loop._renewal)
        self.assertEqual(self.loop._planning_workers, [])
        lease = next(r for r in self.coordinator().history(11) if r["kind"] == "lease")
        self.assertEqual((lease["state"], lease["result"]), ("released", "success"))
