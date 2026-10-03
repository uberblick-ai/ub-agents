from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, fingerprint, latest_leases, live_leases, payload, seconds
from ub_agents.recovery import apply, inspect
from ub_agents import shutdown
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class EarlyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root, backoff_seconds=10, max_backoff_seconds=100)
        self.config = config(self.root, self.agent)
        self.github = FakeGitHub(issue(), pr(labels=()))
        self.now = 1000
        self.host = {"machine": "machine-1", "boot": "boot-1"}
        self.supervisor = {"pid": 800, "birth": "original-birth"}
        self.identity = {"host": "hostname", "host_identity": self.host, "supervisor": self.supervisor}
        self.processes, self.groups = {}, {}
        for name, options in (
            ("ub_agents.shutdown.host_identity", {"side_effect": lambda: self.host}),
            ("ub_agents.shutdown.process_identity", {"side_effect": lambda pid: self.processes.get(pid)}),
            ("ub_agents.shutdown.group_members", {"side_effect": lambda group: self.groups.get(group, [])}),
            ("ub_agents.coordination.identity_fields", {"side_effect": lambda: deepcopy(self.identity)}),
            ("ub_agents.shutdown.identity_fields", {"side_effect": lambda: deepcopy(self.identity)}),
        ):
            mock = patch(name, **options)
            mock.start()
            self.addCleanup(mock.stop)
        self.lines = []
        self.loop = self.restart()

    def restart(self):
        loop = Loop(self.config, self.github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        return loop

    def start(self, report=None, number=1, **changes):
        co = self.loop.coordinator
        lease = co.claim(co.plan(self.github.item(number), self.agent, self.config.stop_labels), self.config.stop_labels)
        co.update(lease, state="running", started=True, process_group=810, **changes)
        shutdown.confirm_cleanup(self.config, lease)
        if report:
            co.report(lease, report, "Completed work", outcome="done" if report == "success" else None)
        self.identity = self.identity | {"supervisor": {"pid": 900, "birth": "recoverer-birth"}}
        return lease

    def history(self):
        return self.loop.coordinator.history(1)

    def count(self):
        return len(attempts(self.history(), self.agent.name, self.now))

    def latest(self):
        return latest_leases(self.history())[(1, self.agent.name)]

    def recover(self):
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")), \
                patch("ub_agents.loop.run_hook", side_effect=AssertionError("must not run cleanup hook")), \
                patch("ub_agents.loop.Workspace.cleanup", side_effect=AssertionError("must preserve worktrees")):
            return apply(self.loop, 1, self.agent)

    def test_preview_is_read_only_and_completed_report_is_validated_and_accepted(self):
        source = self.start("success")
        original = fingerprint(source)
        writes = list(self.github.writes)
        preview = inspect(self.loop, 1, self.agent)
        self.assertTrue(preview.eligible)
        self.assertFalse(preview.applied)
        self.assertEqual(self.github.writes, writes)
        self.assertIn("accept only if valid", preview.proposed)
        result = self.recover()
        self.assertTrue(result.applied, result.reason)
        self.assertEqual(result.result, "recovered completion (accepted)")
        history = self.history()
        self.assertTrue(next(r for r in history if r["kind"] == "outcome" and r["lease_id"] == source["id"])["accepted"])
        self.assertEqual(fingerprint(history[0]), original)
        self.assertEqual(live_leases(history, self.now), [])
        self.assertEqual((self.count(), self.latest()["attempt_effect"]), (0, "reset"))
        self.assertEqual(self.latest()["recovery"]["entry_point"], "cli")
        self.assertEqual(self.latest()["recovery"]["actor"], "operator")
        self.assertTrue(self.latest()["recovery"]["evidence"])
        with self.assertRaises(LostOwnership):
            self.loop.coordinator.assert_owned(source)
        with self.assertRaises(LostOwnership):
            self.loop.coordinator.report(source, "retry", "Still alive")

    def test_unreported_interruption_counts_once_and_repeat_is_idempotent(self):
        self.start()
        result = self.recover()
        self.assertTrue(result.applied, result.reason)
        self.assertEqual(result.result, "interrupted work released for normal retry")
        self.assertEqual(self.count(), 1)
        self.assertEqual(seconds(self.latest()["retry_after"]), self.now + 10)
        writes = list(self.github.writes)
        repeated = self.recover()
        self.assertTrue(repeated.applied)
        self.assertIn("Already recovered", repeated.reason)
        self.assertEqual(self.github.writes, writes)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.loop.coordinator.plan(self.github.item(1), self.agent, ()).state, "backoff")

    def test_persisted_supervisor_verdict_overrides_early_reports_and_retains_count_effect(self):
        for report in (None, "success", "blocked"):
            for result, effect, count in (("retry", "unchanged", 0), ("retry", "failure", 1), ("blocked", "failure", 1)):
                with self.subTest(report=report, result=result, effect=effect):
                    self.setUp()
                    source = self.start(report, result=result, attempt_effect=effect, summary="Supervisor verdict")
                    if effect == "failure" and result == "retry":
                        from ub_agents.records import iso
                        self.loop.coordinator.update(source, retry_after=iso(self.now + 17))
                    decision = self.recover()
                    self.assertTrue(decision.applied, decision.reason)
                    self.assertEqual((self.latest()["result"], self.latest()["attempt_effect"], self.count()), (result, effect, count))
                    self.assertFalse(any(r.get("accepted") for r in self.history()))
                    if effect == "unchanged":
                        self.assertNotIn("retry_after", self.latest())
                    elif result == "retry":
                        self.assertEqual(seconds(self.latest()["retry_after"]), self.now + 17)

    def test_invalid_rejected_and_paused_outcomes_use_normal_finalization(self):
        for change in ("invalid", "rejected", "stop", "trigger", "blocked"):
            with self.subTest(change=change):
                self.setUp()
                self.github.items[1] = pr(1)
                source = self.start("blocked" if change == "blocked" else "success")
                report = self.loop.coordinator.outcome(source)
                if change == "invalid":
                    self.github.change(1, head="b" * 40)
                if change == "rejected":
                    self.loop.coordinator.update_outcome(source, report, rejected="Invalid candidate", attempt_effect="failure")
                if change in {"stop", "trigger"}:
                    self.github.change(1, labels=frozenset({"needs-human"}) if change == "stop" else frozenset())
                result = self.recover()
                self.assertTrue(result.applied, result.reason)
                self.assertIn("recovered outcome rejected", result.result)
                self.assertEqual(self.latest()["result"], "blocked")
                self.assertEqual(self.count(), 1 if change in {"invalid", "rejected"} else 0)
                self.assertFalse(self.loop.coordinator.outcome(source)["accepted"])
                if change != "blocked":
                    self.assertTrue(self.loop.coordinator.outcome(source).get("rejected"))

    def test_all_shutdown_requirements_fail_closed(self):
        cases = ("live-supervisor", "reused-pid", "agent-group", "hook-group", "hook-unconfirmed",
                 "lease-unconfirmed", "diagnostic-unconfirmed", "missing-confirmation", "legacy",
                 "hostname-only", "other-machine", "other-boot", "unreadable-process")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                source = self.start("success")
                directory = shutdown.run_directory(self.config, source)
                if case == "live-supervisor":
                    self.processes[800] = self.supervisor
                elif case == "reused-pid":
                    self.processes[800] = {"pid": 800, "birth": "new-birth"}
                elif case == "agent-group":
                    self.groups[810] = [811]
                elif case.startswith("hook-"):
                    hook = directory / "cleanup" / "hook"
                    hook.mkdir(parents=True)
                    (hook / "pid").write_text("820")
                    if case == "hook-group":
                        (hook / "stopped").write_text("confirmed\n")
                        self.groups[820] = [821]
                elif case == "lease-unconfirmed":
                    self.loop.coordinator.update(source, cleanup="unconfirmed")
                elif case == "diagnostic-unconfirmed":
                    (directory / "events.jsonl").write_text(json.dumps({"event": "cleanup-unconfirmed"}) + "\n")
                elif case == "missing-confirmation":
                    (directory / "cleanup-confirmed.json").unlink()
                elif case in {"legacy", "hostname-only"}:
                    legacy = payload(source)
                    legacy.pop("host_identity")
                    if case == "legacy":
                        legacy.pop("supervisor")
                    self.github.update_comment(source["id"], body(legacy))
                elif case.startswith("other-"):
                    self.host = self.host | {"machine" if case == "other-machine" else "boot": "other"}
                elif case == "unreadable-process":
                    mock = patch("ub_agents.shutdown.process_identity", side_effect=PermissionError("cannot inspect"))
                    mock.start()
                    self.addCleanup(mock.stop)
                writes = list(self.github.writes)
                decision = self.recover()
                self.assertFalse(decision.applied)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.result, "refused or waiting")
                self.assertIn(source["expires"], decision.line())
                self.assertEqual(self.github.writes, writes)
                self.assertEqual(live_leases(self.history(), self.now)[0]["id"], source["id"])

    def test_another_actor_is_shown_and_refused_with_expiry(self):
        source = self.start()
        self.loop.coordinator.actor = "someone-else"
        decision = inspect(self.loop, 1, self.agent)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.lease["actor"], "operator")
        self.assertTrue(any(c["check"] == "actor" and not c["ok"] for c in decision.evidence))
        self.assertIn(source["expires"], decision.line())

    def test_shared_branch_owner_refuses_recovery(self):
        source = self.start()
        branch = f"ub-agent/worker/1/{source['run']}"
        self.loop.coordinator.update(source, branch=branch)
        self.github.change(2, branch=branch, labels=frozenset({"needs-changes"}))
        # Create the competing branch lease directly to model overlapping older launchers.
        other = payload(source) | {"assignment": 2, "assignment_sha": self.github.item(2).head, "run": "other-run"}
        self.github.create_comment(2, body(other))
        writes = list(self.github.writes)
        result = self.recover()
        self.assertFalse(result.applied)
        self.assertIn("shared branch", result.reason)
        self.assertEqual(self.github.writes, writes)

    def test_changed_released_or_replaced_lease_before_claim_is_refused(self):
        for case in ("updated", "released", "replaced"):
            with self.subTest(case=case):
                self.setUp()
                source = self.start()
                writes = len(self.github.writes)

                def change():
                    if case == "updated":
                        self.loop.coordinator.update(source, summary="Changed lease")
                    elif case == "released":
                        self.loop.coordinator.release(source, "retry", "Owner completed")
                    else:
                        self.github.create_comment(1, body(payload(source) | {"run": "new-run"}))

                result = apply(self.loop, 1, self.agent, before_write=change)
                self.assertFalse(result.applied)
                self.assertFalse(any(r.get("supersedes_lease_id") for r in self.history()))
                self.assertGreater(len(self.github.writes), writes)

    def test_competing_recoverers_elect_lowest_comment_and_loser_never_finalizes(self):
        self.start("success")
        loops = [self.loop, self.restart()]
        self.github.claim_barrier = threading.Barrier(2)
        before_create = threading.Barrier(2)
        create = self.github.create_comment

        def synchronized(number, text):
            before_create.wait(timeout=5)
            return create(number, text)

        with patch.object(self.github, "create_comment", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as pool:
            # Only recovery claims synchronize; the winner's outcome must not wait.
            def claim_only(number, text):
                if '"kind": "lease"' in text:
                    return synchronized(number, text)
                return create(number, text)
            self.github.create_comment.side_effect = claim_only
            results = list(pool.map(lambda loop: apply(loop, 1, self.agent), loops))
        self.github.claim_barrier = None
        self.assertEqual(sum(result.applied for result in results), 1, [r.reason for r in results])
        recoveries = [r for r in self.history() if r.get("supersedes_lease_id")]
        self.assertEqual(len(recoveries), 2)
        self.assertEqual(self.latest()["id"], min(r["id"] for r in recoveries))
        self.assertEqual(sum(r.get("result") == "success" for r in recoveries), 1)
        self.assertEqual(live_leases(self.history(), self.now), [])

    def test_crash_in_transition_repeats_without_execution_or_duplicate_handoff(self):
        source = self.start()
        self.loop.coordinator.report(source, "success", "Handoff", handoff=2, outcome="done")
        with patch.object(self.loop.coordinator, "accept", side_effect=AgentError("crash at acceptance")):
            result = self.recover()
        self.assertFalse(result.applied)
        recovery = self.latest()
        self.assertEqual(recovery["state"], "claiming")
        self.assertEqual(self.count(), 0)
        self.processes[900] = self.identity["supervisor"]
        self.assertFalse(self.recover().applied)
        self.processes.clear()
        self.identity = self.identity | {"supervisor": {"pid": 901, "birth": "next-recoverer"}}
        self.loop = self.restart()
        result = self.recover()
        self.assertTrue(result.applied, result.reason)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.latest()["recovered_lease_id"], source["id"])
        self.assertEqual(self.latest()["supersedes_lease_id"], recovery["id"])
        copies = [r for r in self.loop.coordinator.history(2) if r["kind"] == "outcome"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0]["accepted"])

    def test_crash_before_release_preserves_failure_and_backoff_without_double_count(self):
        self.start()
        with patch.object(self.loop.coordinator, "release", side_effect=AgentError("release crash")):
            first = self.recover()
        self.assertFalse(first.applied)
        self.assertEqual(self.count(), 1)
        retry_after = self.latest()["retry_after"]
        self.now += 2
        second = self.recover()
        self.assertTrue(second.applied, second.reason)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.latest()["retry_after"], retry_after)

    def test_recovery_claim_without_local_confirmation_waits_for_its_own_expiry(self):
        self.start()
        with patch("ub_agents.shutdown.confirm_cleanup", side_effect=OSError("disk unavailable")):
            self.assertFalse(self.recover().applied)
        self.assertFalse(self.recover().applied)
        self.now = seconds(self.latest()["expires"]) + 1
        self.loop = self.restart()
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.count(), 1)

    def test_cli_and_launcher_apply_identical_decisions_and_preview_output(self):
        for refused in (False, True):
            snapshots = []
            for entry in ("cli", "launcher"):
                with self.subTest(refused=refused, entry=entry):
                    self.setUp()
                    self.now = 100000000000  # Keep CLI's real clock inside the lease.
                    self.start("success")
                    if refused:
                        self.processes[800] = self.supervisor
                    if entry == "cli":
                        stream = io.StringIO()
                        with patch("ub_agents.cli.load_config", return_value=self.config), \
                                patch("ub_agents.cli.GitHub", return_value=self.github), \
                                patch("ub_agents.cli.repository_checks", return_value=[]), redirect_stdout(stream):
                            self.assertEqual(main(["recover", "--number", "1", "--agent", "worker"]), 0)
                            self.assertIn("Lease comment", stream.getvalue())
                            self.assertIn("source supervisor exited", stream.getvalue())
                            self.assertEqual(main(["recover", "--number", "1", "--agent", "worker", "--apply"]), 1 if refused else 0)
                    else:
                        self.assertEqual(self.loop.tick(), not refused)
                        self.assertIn("refused or waiting" if refused else "recovered completion (accepted)", self.lines[-1])
                    latest = self.latest()
                    snapshots.append((latest["state"], latest.get("result"), latest.get("attempt_effect"), self.count()))
            self.assertEqual(snapshots[0], snapshots[1])

    def test_supervisor_writes_durable_cleanup_before_failed_acceptance(self):
        stub_refresh(self)

        def supervise(*args, **kwargs):
            lease = self.latest()
            self.loop.coordinator.report(lease, "success", "Finished", outcome="done")
            return 0

        with patch("ub_agents.loop.supervise", side_effect=supervise), \
                patch.object(self.loop.coordinator, "accept", side_effect=AgentError("acceptance unavailable")):
            with self.assertRaises(LostOwnership):
                self.loop.tick()
        lease = self.latest()
        shutdown.cleanup_confirmed(self.config, lease)
        self.assertTrue(inspect(self.loop, 1, self.agent).eligible)
