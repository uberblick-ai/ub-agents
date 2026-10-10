from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.config import CleanupHook, LEASE_RENEW_SECONDS, LEASE_SECONDS
from ub_agents.coordination import Coordinator
from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.execution import Workspace
from ub_agents.launch_log import launch_output
from ub_agents.loop import Loop
from ub_agents.records import body, iso, payload, seconds
from ub_agents.renewal import LeaseRenewal
from tests.support import PollGitHub, agent, config, issue, stub_refresh


class ManualRenewal(LeaseRenewal):
    """Drive the real scheduler with a controlled clock instead of a worker."""
    def claimed(self, lease):
        self.lease = lease
        self.next_renewal = self.coordinator.clock() + LEASE_RENEW_SECONDS


class RenewalTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.now = 1000
        self.github = PollGitHub(issue())
        self.role = agent(self.root, lease_seconds=LEASE_SECONDS, timeout_seconds=7200)
        self.loop = Loop(config(self.root, self.role), self.github, "operator", output=lambda *_: None)
        self.co = self.loop.coordinator
        self.co.clock = lambda: self.now
        replacement = patch("ub_agents.loop.LeaseRenewal", ManualRenewal)
        replacement.start()
        self.addCleanup(replacement.stop)

    def claim(self):
        return self.co.claim(self.co.plan(issue(), self.role, ()))

    def advance(self, seconds=LEASE_RENEW_SECONDS):
        self.now += seconds
        self.loop._renewal.tick()

    def finish(self):
        lease = self.loop._renewal.lease
        self.co.report(lease, "success", "Completed", outcome="done")
        return 0

    def execute(self, run):
        with patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        self.assertIsNone(self.loop._renewal)
        self.assertIsNone(self.loop.github.lease)
        lease, outcome = self.co.history(1)
        self.assertEqual((lease["state"], lease["result"], outcome["accepted"]), ("released", "success", True))

    def test_long_execution_renews_the_same_comment_every_ten_minutes(self):
        def run(*args, **kwargs):
            lease = self.loop._renewal.lease
            self.assertEqual(seconds(lease["expires"]), 2800)
            for _ in range(6):
                before = payload(lease)
                self.advance()
                self.assertEqual(payload(self.co.history(1)[0]), before | {"expires": iso(self.now + 1800)})
                self.assertEqual(kwargs["expires"](), self.now + 1800)
            return self.finish()
        self.execute(run)

    def test_one_failed_renewal_then_success_leaves_execution_unaffected(self):
        def run(*args, **kwargs):
            lease = self.loop._renewal.lease
            failure = GitHubError("PATCH", "lease", "transient write failure", retryable=True)
            with patch.object(self.github, "update_comment", side_effect=failure) as write:
                self.advance()
                self.assertEqual(seconds(lease["expires"]), 2800)
                self.co.assert_owned(lease)
                self.now += 1
                self.loop._renewal.tick()
                write.assert_called_once()
            self.advance(599)
            self.assertEqual(seconds(lease["expires"]), 4000)
            self.advance()
            self.advance()
            return self.finish()
        self.execute(run)

    def test_failed_renewals_until_expiry_stop_without_a_late_write(self):
        def run(*args, **kwargs):
            failure = GitHubError("PATCH", "lease", "unavailable", retryable=True)
            with patch.object(self.github, "update_comment", side_effect=failure) as write:
                self.advance()
                self.advance()
                with self.assertRaises(LostOwnership):
                    self.advance()
                self.assertEqual(write.call_count, 2)
                with self.assertRaises(LostOwnership):
                    self.loop._renewal.tick()
                self.assertEqual(write.call_count, 2)
                self.writes_at_expiry = self.github.writes.copy()
                kwargs["expires"]()
        with patch("ub_agents.loop.supervise", side_effect=run), self.assertRaises(LostOwnership):
            self.loop.tick()
        self.assertEqual(self.github.writes, self.writes_at_expiry)
        self.assertIsNone(self.loop._renewal)

    def test_read_failure_does_not_lose_ownership_or_wait_inside_renewal(self):
        lease = self.claim()
        self.now += 600
        failure = GitHubError("GET", "comments", "rate limit", rate_limited=True, reset_at=self.now + 3600)
        self.github.read_results["comments"] = [failure]
        with patch.object(self.loop, "wait_rate_limit", side_effect=AssertionError("renewal must not wait")):
            self.assertFalse(self.co.renew(lease, self.github))
        self.co.assert_owned(lease)
        self.now += 600
        self.assertTrue(self.co.renew(lease, self.github))

    def test_dead_launcher_is_recovered_on_another_host_after_thirty_minutes(self):
        source = self.claim()
        self.co.report(source, "success", "Durable outcome", outcome="done")
        other = Loop(config(self.root, self.role), self.github, "operator", output=lambda *_: None)
        other.coordinator.clock = lambda: self.now
        self.now += 1799
        self.assertEqual(other.plans()[0].state, "owned")
        self.now += 1
        with patch("ub_agents.loop.socket.gethostname", return_value="another-host"), \
                patch("ub_agents.loop.supervise", side_effect=AssertionError("outcome recovery starts no agent")):
            self.assertTrue(other.tick())
        history = self.co.history(1)
        self.assertTrue(history[1]["accepted"])
        self.assertEqual((history[2]["mode"], history[2]["state"]), ("recovery", "released"))

    def test_renewal_during_setup_and_completion(self):
        prepare = Workspace.prepare
        finalize = self.loop.finalize
        def setup(workspace):
            for _ in range(4):
                self.advance()
            return prepare(workspace)
        def complete(*args):
            for _ in range(4):
                self.advance()
            return finalize(*args)
        with patch.object(Workspace, "prepare", setup), patch.object(self.loop, "finalize", side_effect=complete):
            self.execute(lambda *args, **kwargs: self.finish())

    def test_renewal_reads_fresh_ownership_without_ending_finalization_read_sharing(self):
        lease = self.claim()
        self.loop.github.begin_finalization()
        self.addCleanup(self.loop.github.end_finalization)
        with patch.object(self.github, "comments", wraps=self.github.comments) as read:
            self.co.assert_owned(lease)
            for _ in range(4):
                self.now += 600
                self.assertTrue(self.co.renew(lease, self.github))
                self.co.assert_owned(lease)
            # The original shared lease expiry has passed, but each renewal
            # independently checked ownership and confirmed its extension.
            self.assertEqual(read.call_count, 5)
            self.assertEqual(self.co.deadline(lease), self.now + LEASE_SECONDS)

    def test_cleanup_hook_uses_the_renewed_deadline(self):
        self.loop.config = replace(self.loop.config, cleanup=CleanupHook(("echo",), 3600))
        def prepare(workspace):
            workspace.private.mkdir(parents=True)
            workspace.created = True
            return workspace.private
        def hook(*args, **kwargs):
            for _ in range(4):
                self.advance()
                self.assertEqual(kwargs["expires"](), self.now + 1800)
            return 0
        with patch.object(Workspace, "prepare", prepare), \
                patch("ub_agents.execution.git", return_value=""), patch("ub_agents.hooks.supervise", side_effect=hook):
            self.execute(lambda *args, **kwargs: self.finish())

    def test_rate_limit_reset_after_current_expiry_waits_while_renewing(self):
        def run(*args, **kwargs):
            lease = self.loop._renewal.lease
            failure = GitHubError("GET", "item", "rate limit", rate_limited=True, reset_at=self.now + 2400)
            self.github.read_results["item"] = [failure]
            self.co.assert_owned(lease)
            self.loop.github.item(1)
            self.assertEqual(self.now, 3400)
            self.assertEqual(kwargs["expires"](), 5200)
            return self.finish()
        def wait(delay):
            self.assertLessEqual(delay, 60)
            self.advance(delay)
        with patch.object(self.loop.interrupt_event, "wait", side_effect=wait):
            self.execute(run)

    def test_recovery_claim_renews_during_a_long_transition(self):
        source = self.claim()
        self.co.report(source, "success", "Durable outcome", outcome="done")
        self.now += 1800
        finalize = self.loop.finalize
        def complete(recovery, *args):
            self.assertEqual(recovery["mode"], "recovery")
            for _ in range(4):
                self.advance()
            return finalize(recovery, *args)
        with patch.object(self.loop, "finalize", side_effect=complete):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.co.history(1)[2]["state"], "released")

    def test_late_renewal_and_slow_ownership_read_do_not_start_a_write(self):
        lease = self.claim()
        for remaining in (21, 20, 0, -1):
            self.now = 2800 - remaining
            with self.subTest(remaining=remaining), patch.object(self.github, "update_comment") as write:
                if remaining <= 0:
                    with self.assertRaises(LostOwnership):
                        self.co.renew(lease, self.github)
                else:
                    self.assertFalse(self.co.renew(lease, self.github))
                write.assert_not_called()
        self.setUp()
        lease = self.claim()
        self.now = 2700
        comments = self.github.comments
        def slow_read(number):
            self.now = 2780
            return comments(number)
        with patch.object(self.github, "comments", side_effect=slow_read), patch.object(self.github, "update_comment") as write:
            self.assertFalse(self.co.renew(lease, self.github))
            write.assert_not_called()

    def test_recorded_expiry_remains_authoritative(self):
        lease = self.claim()
        self.co.update(lease, expires=iso(1000 + 11700))
        observer = Coordinator(self.github, "operator", clock=lambda: self.now)
        self.now += 1801
        self.assertEqual(observer.plan(issue(), self.role, ()).state, "owned")
        self.assertEqual(observer.assert_owned(lease)["expires"], iso(12700))
        self.now = 12699
        self.assertEqual(observer.plan(issue(), self.role, ()).state, "owned")
        self.now = 12700
        self.assertEqual(observer.plan(issue(), self.role, ()).state, "ready")

    def test_release_withdrawal_and_state_edits_serialize_with_renewal(self):
        for change in ({"cleanup": "unconfirmed"}, {"state": "running", "started": True},
                       {"state": "released", "result": "blocked"}, {"state": "withdrawn"}):
            with self.subTest(change=change):
                self.setUp()
                lease = self.claim()
                self.now += 600
                write_started, allow_write = threading.Event(), threading.Event()
                update = self.github.update_comment
                def paused_write(*args):
                    write_started.set()
                    self.assertTrue(allow_write.wait(5))
                    return update(*args)
                errors = []
                def renew():
                    try:
                        self.co.renew(lease, self.github)
                    except Exception as exc:
                        errors.append(exc)
                with patch.object(self.github, "update_comment", side_effect=paused_write):
                    worker = threading.Thread(target=renew)
                    worker.start()
                    self.assertTrue(write_started.wait(5))
                    edit = threading.Thread(target=lambda: self.co.update(lease, **change))
                    edit.start()
                    allow_write.set()
                    worker.join(5)
                    edit.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(edit.is_alive())
                self.assertEqual(errors, [])
                stored = self.co.history(1)[0]
                for key, value in change.items():
                    self.assertEqual(stored[key], value)
                if change.get("state") in {"released", "withdrawn"}:
                    self.assertEqual(stored["expires"], iso(self.now))
                    writes = self.github.writes.copy()
                    with self.assertRaises(LostOwnership):
                        self.co.renew(lease, self.github)
                    self.assertEqual(self.github.writes, writes)
                else:
                    self.assertEqual(stored["expires"], iso(self.now + 1800))
                    self.now += 600
                    self.co.renew(lease, self.github)
                    for key, value in change.items():
                        self.assertEqual(self.co.history(1)[0][key], value)

    def test_an_edit_during_a_renewal_read_cannot_be_overwritten(self):
        for changes in ({"state": "withdrawn"}, {"state": "released", "result": "blocked"},
                        {"cleanup": "unconfirmed"}, {"state": "running", "started": True}):
            with self.subTest(changes=changes):
                self.setUp()
                lease = self.claim()
                self.now += 600
                comments = self.github.comments
                def edit_during_read(number):
                    snapshot = comments(number)
                    self.co.update(lease, **changes)
                    return snapshot
                with patch.object(self.github, "comments", side_effect=edit_during_read):
                    if changes.get("state") in {"released", "withdrawn"}:
                        with self.assertRaises(LostOwnership):
                            self.co.renew(lease, self.github)
                    else:
                        self.assertFalse(self.co.renew(lease, self.github))
                stored = self.co.history(1)[0]
                self.assertEqual(payload(stored), payload(lease))
                self.assertEqual(len(self.github.writes), 2)  # claim and state edit only

    def test_renewal_reads_assignment_before_a_concurrent_update_clears_the_lease(self):
        read_started, read_finished = threading.Event(), threading.Event()
        lease_cleared, finish_update = threading.Event(), threading.Event()
        class ReplacingLease(dict):
            def clear(self):
                super().clear()
                lease_cleared.set()
                if not finish_update.wait(5):
                    raise AssertionError("Lease replacement was not resumed")

        lease = ReplacingLease(self.claim())
        self.now += 600
        trusted = self.co.trust.observation()
        comments = self.github.comments
        errors, renewed = [], []
        def observation():
            read_started.set()
            self.assertTrue(lease_cleared.wait(5))
            return trusted
        def read(number):
            self.assertEqual(lease, {})
            self.assertEqual(number, 1)
            try:
                return comments(number)
            finally:
                read_finished.set()
        def renew():
            try:
                renewed.append(self.co.renew(lease, self.github))
            except Exception as exc:
                errors.append(exc)
            finally:
                read_finished.set()
        def update():
            try:
                self.co.update(lease, state="running", started=True)
            except Exception as exc:
                errors.append(exc)
        with patch("ub_agents.coordination.LauncherTrust.observation", side_effect=observation), \
                patch.object(self.github, "comments", side_effect=read):
            worker = threading.Thread(target=renew)
            edit = threading.Thread(target=update)
            worker.start()
            try:
                self.assertTrue(read_started.wait(5))
                edit.start()
                self.assertTrue(read_finished.wait(5))
            finally:
                finish_update.set()
                worker.join(5)
                if edit.ident is not None:
                    edit.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(edit.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(renewed, [False])  # The concurrent edit invalidates the read.
        stored = self.co.history(1)[0]
        self.assertEqual(payload(stored), payload(lease))
        self.assertEqual((stored["state"], stored["started"], stored["expires"]), ("running", True, iso(2800)))
        self.assertEqual(len(self.github.writes), 2)
        self.assertTrue(self.co.renew(lease, self.github))
        self.assertEqual(seconds(lease["expires"]), 3400)

    def test_slow_renewal_read_does_not_block_local_expiry_checks(self):
        lease = self.claim()
        self.now += 600
        read_started, finish_read = threading.Event(), threading.Event()
        errors = []
        comments = self.github.comments
        def slow_read(number):
            snapshot = comments(number)
            read_started.set()
            self.assertTrue(finish_read.wait(5))
            return snapshot
        def renew():
            try:
                self.co.renew(lease, self.github)
            except LostOwnership as exc:
                errors.append(exc)
        with patch.object(self.github, "comments", side_effect=slow_read), patch.object(self.github, "update_comment") as write:
            worker = threading.Thread(target=renew)
            worker.start()
            try:
                self.assertTrue(read_started.wait(5))
                self.now = 2800
                with self.assertRaises(LostOwnership):
                    self.co.deadline(lease)
            finally:
                finish_read.set()
                worker.join(5)
            write.assert_not_called()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)

    def test_pending_branch_and_stale_ownership_read_do_not_roll_back_renewal(self):
        lease = self.claim()
        old_history = self.co.history(1)
        lease["branch"] = "ub-agents/worker/1/staged"
        self.now += 600
        self.co.renew(lease, self.github)
        with patch.object(self.co, "history", return_value=old_history):
            self.co.assert_owned(lease)
        self.co.update(lease, branch=lease["branch"])
        self.co.update(lease, expires=old_history[0]["expires"])
        stored = self.co.history(1)[0]
        self.assertEqual((stored["expires"], stored["branch"]), (iso(3400), "ub-agents/worker/1/staged"))

    def test_another_claim_revokes_ownership_without_a_renewal_write(self):
        lease = self.claim()
        self.now += 600
        recovery = payload(lease) | {"run": "other", "mode": "recovery", "recovered_lease_id": lease["id"],
                                    "recovered_run": lease["run"]}
        self.github.create_comment(1, body(recovery))
        with patch.object(self.github, "update_comment") as write, self.assertRaises(LostOwnership):
            self.co.renew(lease, self.github)
        write.assert_not_called()
        with self.assertRaises(LostOwnership):
            self.co.deadline(lease)

    def test_background_worker_is_started_at_claim_and_joined_on_completion(self):
        with patch("ub_agents.loop.LeaseRenewal", LeaseRenewal):
            def run(*args, **kwargs):
                self.worker = self.loop._renewal.thread
                self.assertTrue(self.worker.is_alive())
                return self.finish()
            self.execute(run)
        self.assertFalse(self.worker.is_alive())

    def test_unexpected_worker_failures_reach_launcher_diagnostics_without_extending_expiry(self):
        for operation, failure in (("renew", KeyError("unexpected renewal failure")),
                                   ("wait", RuntimeError("unexpected wait failure"))):
            with self.subTest(operation=operation):
                self.setUp()
                lease = self.claim()
                renewal = LeaseRenewal(self.co, self.github)
                self.co.output = print
                before, writes = payload(lease), self.github.writes.copy()
                if operation == "renew":
                    self.now += 600
                target = self.co if operation == "renew" else renewal.stop
                with redirect_stdout(io.StringIO()) as terminal, launch_output(self.root), \
                        patch.object(target, operation, side_effect=failure):
                    renewal.claimed(lease)
                    try:
                        renewal.thread.join(5)
                        self.assertFalse(renewal.thread.is_alive())
                    finally:
                        renewal.close()
                message = f"Lease renewal worker stopped unexpectedly: {type(failure).__name__}: {failure}"
                self.assertIn(message, terminal.getvalue())
                log = (self.root / ".ub-agents" / "launch.log").read_text()
                self.assertIn(message, log)
                self.assertIn("last confirmed expiry", log)
                self.assertEqual(payload(lease), before)
                self.assertEqual(self.github.writes, writes)
                self.assertEqual(self.co.deadline(lease), 2800)
                self.now = 2800
                with self.assertRaises(LostOwnership):
                    self.co.deadline(lease)
                self.now = 2799
                with patch.object(self.github, "update_comment") as write, self.assertRaises(LostOwnership):
                    self.co.renew(lease, self.github)
                write.assert_not_called()

    def test_worker_ownership_loss_remains_visible_to_supervision(self):
        lease = self.claim()
        self.now = seconds(lease["expires"])
        messages = []
        self.co.output = messages.append
        renewal = LeaseRenewal(self.co, self.github)
        writes = self.github.writes.copy()
        renewal.claimed(lease)
        try:
            renewal.thread.join(5)
            self.assertFalse(renewal.thread.is_alive())
        finally:
            renewal.close()
        self.assertEqual(messages, [])
        self.assertEqual(self.github.writes, writes)
        with self.assertRaises(LostOwnership):
            self.co.deadline(lease)
