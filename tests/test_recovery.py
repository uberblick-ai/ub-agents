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

    def test_recovers_another_trusted_actor_with_an_older_owned_lease(self):
        lease, _ = self.start(report=False)
        self.co.release(lease, "retry", "Stopped")
        self.github.login = "other-operator"
        self.github.roles["other-operator"] = "write"
        other = Loop(self.config, self.github, "other-operator", output=lambda *_: None)
        other.coordinator.clock = lambda: self.now
        plan = other.coordinator.plan(self.github.item(1), self.agent, ())
        latest = other.coordinator.claim(plan)
        other.coordinator.update(latest, state="running", host="local-host", process_group=1234)
        outcome = other.coordinator.report(latest, "success", "Other account completed", handoff=2,
                                           outcome="handed-off")
        self.github.login = "operator"
        self.early()
        self.assertTrue(other.coordinator.outcome(latest)["accepted"])
        self.assertEqual(other.coordinator.outcome(latest)["actor"], "other-operator")
        copied, = self.co.history(2)
        self.assertEqual((copied["actor"], copied["run"]), ("operator", outcome["run"]))
        self.assertTrue(self.co.released_success(self.co.history(1), copied))
        self.assertEqual(live_leases(self.co.history(1), self.now), [])
        self.assertEqual(attempts(self.co.history(1), self.agent.name, self.now), [])
        with self.assertRaises(LostOwnership):
            other.coordinator.assert_owned(latest)

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

    def retry_after_failure(self):
        self.agent = replace(self.agent, backoff_seconds=10, max_backoff_seconds=100)
        self.config = config(self.root, self.agent)
        self.loop = self.new_loop()
        self.manual = self.new_loop()
        self.co = self.loop.coordinator
        previous, _ = self.start(report=False)
        self.co.release(previous, "retry", "Earlier failure", attempt_effect="failure")
        return self.start(status="retry")[0]

    def test_early_retry_backoff_matches_expiry_with_prior_failures(self):
        for early in (True, False):
            with self.subTest(early=early):
                self.github = FakeGitHub(issue(), pr())
                lease = self.retry_after_failure()
                with patch("ub_agents.recovery.Loop", return_value=self.manual):
                    if early:
                        self.early()
                    else:
                        self.now = seconds(lease["expires"]) + 1
                        self.loop.recover(self.co.plan(self.github.item(1), self.agent, self.config.stop_labels))
                recovery = next(r for r in reversed(self.co.history(1)) if r.get("mode") == "recovery")
                self.assertEqual(seconds(recovery["retry_after"]) - seconds(recovery["expires"]), 20)
                self.assertEqual(len(attempts(self.co.history(1), self.agent.name, self.now)), 2)

    def test_early_retry_verdict_and_backoff_survive_a_crash_before_release(self):
        lease = self.retry_after_failure()
        short_config = config(self.root, replace(self.agent, lease_seconds=5))
        with patch("ub_agents.recovery.Loop", return_value=self.manual), \
                patch.object(self.manual.coordinator, "report", side_effect=LostOwnership("Recovery stopped")):
            with self.assertRaises(LostOwnership):
                recover_run(short_config, self.github, "operator", 1, self.agent.name, "Launcher stopped")
        recovery = next(r for r in reversed(self.co.history(1)) if r.get("mode") == "recovery")
        self.assertEqual((recovery["state"], recovery["result"], recovery["attempt_effect"]),
                         ("claiming", "retry", "failure"))
        self.assertEqual(seconds(recovery["retry_after"]) - self.now, 20)
        self.now += 6
        plan = self.co.plan(self.github.item(1), self.agent, self.config.stop_labels)
        self.assertEqual((plan.state, plan.attempt), ("backoff", 3))
        with self.assertRaises(LostOwnership):
            self.co.assert_owned(lease)

    def test_only_matching_nonwithdrawn_recovery_claims_revoke_ownership(self):
        source, _ = self.start()
        recovery = payload(source) | {"id": 99, "run": "recoverer", "mode": "recovery",
                                     "recovered_lease_id": source["id"], "recovered_run": source["run"]}
        for changes in ({"state": "withdrawn"}, {"recovered_run": "unrelated"},
                        {"recovered_lease_id": 999}, {"assignment": 2}, {"agent": "other"}):
            with self.subTest(changes=changes):
                self.assertIn(source, live_leases([source, recovery | changes], self.now))
        self.assertNotIn(source, live_leases([source, recovery | {"actor": "other-operator"}], self.now))
        for state, expires in (("claiming", self.now + 5), ("running", self.now + 5),
                               ("released", self.now - 1), ("claiming", self.now - 1)):
            with self.subTest(state=state, expires=expires):
                self.assertNotIn(source, live_leases([source, recovery | {"state": state, "expires": iso(expires)}], self.now))

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
        argv = ["recover", "1", "--agent", self.agent.name, "--reason", "Launcher stopped"]
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
                self.assertEqual(main(["recover", "1", "--reason", "Launcher stopped"]), 0)
            self.assertTrue(stdout.getvalue().startswith("Using agent worker for issue #1.\n"))
            self.assertIn("outcome accepted", stdout.getvalue())
            self.assertIn("added to #2: needs-review", stdout.getvalue())

    def test_requires_configured_agent_positive_number_and_reason(self):
        for overrides in ({"agent_name": "missing"}, {"number": 0}, {"reason": "   "}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(AgentError):
                    self.early(**overrides)
                self.assertEqual(self.github.writes, [])


class RecoveryAgentSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_default_agent_uses_first_applicable_kind_and_prints_before_recovery(self):
        for factory in (issue, pr):
            item = factory(42)
            wrong = agent(self.root, name="wrong", kind="pr" if item.kind == "issue" else "issue")
            first = agent(self.root, name="first", kind=item.kind)
            either = agent(self.root, name="either", kind="either")
            for agents, chosen in (((wrong, first), "first"),
                                   ((wrong, first, either), "first"),
                                   ((wrong, either, first), "either")):
                with self.subTest(kind=item.kind, agents=[a.name for a in agents]):
                    cfg = config(self.root, *agents)
                    github = FakeGitHub(item)
                    output = io.StringIO()

                    def recover(*args):
                        self.assertEqual(output.getvalue(), f"Using agent {chosen} for {item.kind} #42.\n")

                    with patch("ub_agents.cli.load_config", return_value=cfg), \
                            patch("ub_agents.cli.GitHub", return_value=github), \
                            patch("ub_agents.recovery.recover_run", side_effect=recover) as run, \
                            redirect_stdout(output):
                        self.assertEqual(main(["recover", "42", "--reason", "Launcher stopped"]), 0)
                    run.assert_called_once_with(cfg, github, "operator", 42, chosen, "Launcher stopped")

    def test_no_applicable_agent_refuses_without_recovery_or_records(self):
        for factory, configured_kind in ((issue, "pr"), (pr, "issue")):
            with self.subTest(kind=factory.__name__):
                item = factory(42)
                cfg = config(self.root, agent(self.root, kind=configured_kind))
                github = FakeGitHub(item)
                output, errors = io.StringIO(), io.StringIO()
                with patch("ub_agents.cli.load_config", return_value=cfg), \
                        patch("ub_agents.cli.GitHub", return_value=github), \
                        patch("ub_agents.recovery.recover_run") as run, \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(["recover", "42", "--reason", "Launcher stopped"]), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(f"No configured agent applies to {item.kind} #42", errors.getvalue())
                run.assert_not_called()
                self.assertEqual(github.writes, [])
