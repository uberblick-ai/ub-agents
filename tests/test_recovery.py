from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import AgentError, CleanupError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, iso, live_leases, payload, seconds
from ub_agents.recovery import recover_run
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh, write_legacy_records


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = agent(self.root, outcomes={"handed-off": {"add": ("needs-review",), "remove": ("old",)}})
        self.config = config(self.root, self.agent)
        self.github = FakeGitHub(issue(labels=("ready", "old", "unrelated")), pr(labels=("unrelated",)))
        self.now = 1000
        self.output = []
        self.loop = self.new_loop()
        self.manual = self.new_loop()
        self.co = self.loop.coordinator
        self.enterContext(patch("ub_agents.recovery.socket.gethostname", return_value="local-host"))
        self.groups = self.enterContext(patch("ub_agents.recovery.group_members", return_value=[]))
        self.enterContext(patch("ub_agents.recovery.Loop", return_value=self.manual))

    def new_loop(self):
        loop = Loop(self.config, self.github, "operator", output=self.output.append)
        loop.coordinator.clock = lambda: self.now
        return loop

    def start(self, report=True, status="success"):
        plan = self.co.plan(self.github.item(1), self.agent, self.config.stop_labels)
        lease = self.co.claim(plan, self.config.stop_labels)
        self.co.update(lease, state="running", started=True, host="local-host", process_group=1234)
        outcome = None
        if report:
            outcome = self.co.report(lease, status, "Checks passed", handoff=2 if status == "success" else None,
                                     outcome="handed-off" if status == "success" else None)
        return lease, outcome

    def early(self, **overrides):
        recover_run(self.config, self.github, **({"actor": "operator", "number": 1,
                    "agent_name": self.agent.name, "reason": "Launcher stopped on this host",
                    "output": self.output.append} | overrides))

    def refuse(self, message, **overrides):
        before = list(self.github.writes)
        with self.assertRaisesRegex(AgentError, message) as caught:
            self.early(**overrides)
        self.assertIn("refused", str(caught.exception))
        self.assertIn("lease expires", str(caught.exception))
        self.assertEqual(self.github.writes, before)

    def verdict(self, lease):
        history = self.co.history(1)
        outcome = self.co.outcome(lease)
        recovery = next(r for r in reversed(history) if r.get("mode") == "recovery")
        return (self.github.item(1).labels, self.github.item(2).labels,
                outcome["accepted"], outcome.get("transition_complete"), outcome.get("rejected"),
                recovery["result"], recovery["attempt_effect"], len(attempts(history, self.agent.name, self.now)))

    def test_unaccepted_report_has_same_result_early_and_after_expiry(self):
        results = []
        for early in (True, False):
            with self.subTest(early=early):
                if not early:
                    self.github = FakeGitHub(issue(labels=("ready", "old", "unrelated")), pr(labels=("unrelated",)))
                    self.loop = self.new_loop()
                    self.co = self.loop.coordinator
                lease, outcome = self.start()
                self.assertFalse(outcome["accepted"])
                if early:
                    expiry = lease["expires"]
                    self.early()
                    original, _, recovery, _ = self.co.history(1)
                    self.assertEqual((original["state"], original["expires"]), ("running", expiry))
                    self.assertEqual((recovery["actor"], recovery["recovery_reason"]),
                                     ("operator", "Launcher stopped on this host"))
                    self.assertIn("outcome accepted", self.output[-1])
                    self.assertIn("removed from #1: needs-changes, old, ready", self.output[-1])
                    self.assertIn("added to #2: needs-review", self.output[-1])
                    self.assertEqual(live_leases(self.co.history(1), self.now), [])
                    with self.assertRaises(LostOwnership):
                        self.co.assert_owned(lease)
                else:
                    self.now = seconds(lease["expires"]) + 1
                    plan = self.co.plan(self.github.item(1), self.agent, self.config.stop_labels)
                    self.assertTrue(self.loop.recover(plan))
                results.append(self.verdict(lease))
                copied, = self.co.history(2)
                self.assertTrue(copied["accepted"])
                self.assertTrue(self.co.released_success(self.co.history(1), copied))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][:4], ({"unrelated"}, {"unrelated", "needs-review"}, True, True))

    def test_refuses_another_actor_even_with_an_older_owned_lease(self):
        lease, _ = self.start(report=False)
        self.co.release(lease, "retry", "Stopped")
        self.github.login = "other-operator"
        other = Loop(self.config, self.github, "other-operator", output=lambda *_: None)
        other.coordinator.clock = lambda: self.now
        plan = other.coordinator.plan(self.github.item(1), self.agent, ())
        latest = other.coordinator.claim(plan)
        other.coordinator.update(latest, state="running", host="local-host", process_group=1234)
        self.github.login = "operator"
        self.refuse("another actor")

    def test_refuses_another_or_missing_host(self):
        lease, _ = self.start()
        for host in ("remote-host", None):
            with self.subTest(host=host):
                self.co.update(lease, host=host)
                self.refuse("another host")

    def test_refuses_live_missing_or_unreadable_process_group(self):
        lease, _ = self.start()
        self.groups.return_value = [1234]
        self.refuse("live members")
        self.groups.side_effect = CleanupError("Unreadable process table")
        self.refuse("cannot be confirmed.*Unreadable process table")
        self.groups.side_effect = None
        self.groups.return_value = []
        self.co.update(lease, process_group=None)
        self.refuse("no recorded process group")

    def test_refuses_no_outcome_or_outcome_outside_validity_window(self):
        lease, _ = self.start(report=False)
        self.refuse("no reported outcome")
        self.now = seconds(lease["expires"]) + 1
        # Simulate a late report bypassing the normal ownership guard.
        with patch.object(self.co, "assert_owned"):
            outcome = self.co.report(lease, "success", "Late report", outcome="handed-off")
        self.refuse("validity window")
        with patch.object(self.co, "assert_owned"):
            self.co.update_outcome(lease, outcome, created=iso(seconds(lease["created"]) - 1))
        self.refuse("validity window")

    def test_refuses_a_supervisor_verdict_or_unconfirmed_cleanup(self):
        lease, _ = self.start()
        for effect in ("failure", "unchanged"):
            with self.subTest(effect=effect):
                self.co.update(lease, result="retry", attempt_effect=effect)
                self.refuse("supervisor verdict")
        self.co.update(lease, result=None, attempt_effect="pending", cleanup="unconfirmed")
        self.refuse("cleanup is unconfirmed")

    def test_refuses_finished_lease_no_lease_and_another_live_agent(self):
        self.refuse("no latest lease")
        lease, _ = self.start()
        # Model a simultaneous, not yet withdrawn contender.
        self.github.create_comment(1, body(payload(lease) | {"agent": "other", "run": "contender"}))
        self.refuse("another live lease")
        self.co.update(lease, state="released")
        self.refuse("already finished")

    def test_rechecks_process_group_immediately_before_claim(self):
        self.start()
        self.groups.side_effect = [[], [], [1234]]
        self.refuse("live members")

    def test_rejected_and_invalid_reports_finish_as_they_do_after_expiry(self):
        for failure in ("draft", "stop", "rejected", "transition"):
            results = []
            for early in (True, False):
                with self.subTest(failure=failure, early=early):
                    self.github = FakeGitHub(issue(labels=("ready", "old", "unrelated")), pr(labels=("unrelated",)))
                    self.loop = self.new_loop()
                    self.manual = self.new_loop()
                    self.co = self.loop.coordinator
                    with patch("ub_agents.recovery.Loop", return_value=self.manual):
                        lease, outcome = self.start()
                        if failure == "draft":
                            self.github.change(2, draft=True)
                        elif failure == "stop":
                            self.github.change(1, labels={"ready", "needs-human"})
                        elif failure == "rejected":
                            self.co.update_outcome(lease, outcome, rejected="Previously rejected")
                        else:
                            self.co.update_outcome(lease, outcome, transition={"add": ["unexpected"], "started": False})
                        if early:
                            self.early()
                            self.assertIn("outcome rejected", self.output[-1])
                            self.assertIn(self.co.outcome(lease)["rejected"], self.output[-1])
                        else:
                            self.now = seconds(lease["expires"]) + 1
                            self.loop.recover(self.co.plan(self.github.item(1), self.agent, self.config.stop_labels))
                        results.append(self.verdict(lease))
                        self.assertFalse(self.co.outcome(lease)["accepted"])
                        self.assertFalse(any(w[0] in {"add-labels", "remove-label"} for w in self.github.writes))
            self.assertEqual(results[0], results[1])

    def test_retry_and_blocked_reports_preserve_failure_count_semantics(self):
        for status, effect, failures in (("retry", "failure", 1), ("blocked", "unchanged", 0)):
            with self.subTest(status=status):
                self.github = FakeGitHub(issue(), pr())
                self.loop = self.new_loop()
                self.manual = self.new_loop()
                self.co = self.loop.coordinator
                with patch("ub_agents.recovery.Loop", return_value=self.manual):
                    lease, _ = self.start(status=status)
                    self.early()
                result = self.verdict(lease)
                self.assertEqual(result[-3:], (status, effect, failures))
                self.assertIn("outcome rejected — Checks passed", self.output[-1])

    def test_early_recovery_and_launcher_expiry_recovery_use_the_same_election(self):
        lease, _ = self.start()
        self.now = seconds(lease["expires"]) + 1
        plan = self.co.plan(self.github.item(1), self.agent, self.config.stop_labels)
        before_write = threading.Barrier(2)
        self.github.claim_barrier = threading.Barrier(2)
        with patch.object(self.loop, "_end_poll", side_effect=lambda: before_write.wait(timeout=5)), \
                patch.object(self.manual, "_end_poll", side_effect=lambda: before_write.wait(timeout=5)), \
                ThreadPoolExecutor(max_workers=2) as pool:
            expiry = pool.submit(self.loop.recover, plan)
            operator = pool.submit(self.early)
            expiry.result(timeout=10)
            try:
                operator.result(timeout=10)
            except AgentError as exc:
                self.assertIn("another claimant won", str(exc))
        recoveries = [r for r in self.co.history(1) if r.get("mode") == "recovery"]
        self.assertEqual(len(recoveries), 2)
        self.assertEqual([r["state"] for r in recoveries], ["released", "withdrawn"])
        self.assertTrue(self.co.outcome(lease)["accepted"])
        self.assertEqual(len([w for w in self.github.writes if w[0] == "add-labels"]), 1)

    def test_alive_original_supervisor_loses_ownership_and_makes_no_more_writes(self):
        after_recovery = []

        def execution(*args, **kwargs):
            kwargs["process_started"](1234)
            lease = self.co.history(1)[0]
            self.co.report(lease, "success", "Checks passed", handoff=2, outcome="handed-off")
            self.early()
            after_recovery.extend(self.github.writes)
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execution):
            with self.assertRaises(LostOwnership):
                self.loop.tick()
        self.assertEqual(self.github.writes, after_recovery)
        source = self.co.history(1)[0]
        self.assertEqual(source["state"], "running")
        with self.assertRaises(LostOwnership):
            self.co.report(source, "retry", "Original supervisor must not report")
        self.assertEqual(self.github.writes, after_recovery)

    def test_crashed_early_recovery_never_restores_original_ownership(self):
        lease, _ = self.start()
        short_config = config(self.root, replace(self.agent, lease_seconds=5))
        with patch.object(self.manual, "finalize", side_effect=LostOwnership("Recovery stopped")):
            with self.assertRaises(LostOwnership):
                recover_run(short_config, self.github, "operator", 1, self.agent.name, "Launcher stopped")
        with self.assertRaises(LostOwnership):
            self.co.assert_owned(lease)
        self.now += 6
        with self.assertRaises(LostOwnership):
            self.co.assert_owned(lease)
        plan = self.co.plan(self.github.item(1), self.agent, self.config.stop_labels)
        self.assertEqual(plan.state, "recover")
        self.assertTrue(self.loop.recover(plan))
        self.assertEqual(self.verdict(lease)[-3:], ("success", "reset", 0))
        self.assertGreater(seconds(lease["expires"]), self.now)

    def test_legacy_and_started_reports_can_be_recovered_early(self):
        write_legacy_records(self.github)
        lease, outcome = self.start()
        self.co.update_outcome(lease, outcome, transition=outcome["transition"] | {"started": True})
        self.github.change(1, labels={"unrelated"})
        self.github.change(2, head="b" * 40, body="Link removed")
        self.early()
        self.assertTrue(self.co.outcome(lease)["accepted"])
        self.assertEqual(self.github.item(2).labels, {"unrelated", "needs-review"})

    def test_cli_plain_text_and_nonzero_refusal(self):
        lease, _ = self.start()
        argv = ["recover", "--number", "1", "--agent", self.agent.name, "--reason", "Launcher stopped"]
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            self.groups.return_value = [1234]
            before = list(self.github.writes)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(argv), 1)
            self.assertIn("live members", stderr.getvalue())
            self.assertIn(lease["expires"], stderr.getvalue())
            self.assertEqual(self.github.writes, before)
            self.groups.return_value = []
            stdout = io.StringIO()
            self.manual.output = print
            with redirect_stdout(stdout):
                self.assertEqual(main(argv), 0)
            self.assertIn("outcome accepted", stdout.getvalue())
            self.assertIn("added to #2: needs-review", stdout.getvalue())

    def test_requires_configured_agent_positive_number_and_reason(self):
        for overrides in ({"agent_name": "missing"}, {"number": 0}, {"reason": "   "}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(AgentError):
                    self.early(**overrides)
                self.assertEqual(self.github.writes, [])
