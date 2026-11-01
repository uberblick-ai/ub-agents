from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.config import CleanupHook, LEASE_RENEW_SECONDS, LEASE_SECONDS, load_config
from ub_agents.coordination import Coordinator
from ub_agents.errors import ExecutionStopped, GitHubError, LostOwnership
from ub_agents.execution import ScratchDirectory, Workspace, group_members, supervise
from ub_agents.launch_log import launch_output
from ub_agents.loop import Loop
from ub_agents.settlement import Settlement
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import attempts, body, iso, payload, seconds
from ub_agents.renewal import LeaseRenewal
from tests.support import PollGitHub, agent, config, issue, pr, stub_refresh


class ManualRenewal(LeaseRenewal):
    """Drive the real scheduler with a controlled clock instead of a worker."""
    def claimed(self, lease):
        with patch("ub_agents.renewal.threading.Thread"):
            super().claimed(lease)


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
        with patch("ub_agents.assignment_run.supervise", side_effect=run):
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

    def test_execution_stop_terminates_process_and_discards_an_early_report(self):
        changes = ((issue, {"labels": frozenset({"needs-human", "unrelated"})}, "Stop label needs-human"),
                   (pr, {"labels": frozenset({"needs-human", "unrelated"})}, "Stop label needs-human"),
                   (issue, {"state": "closed"}, "Issue closed"),
                   (pr, {"state": "closed"}, "Pull request closed"),
                   (pr, {"state": "closed", "merged": True}, "Pull request merged"))
        for (factory, change, reason), early_report in ((case, early) for case in changes
                                                       for early in (None, "success", "blocked")):
            with self.subTest(kind=factory.__name__, change=change, early_report=early_report):
                self.setUp()
                self.github.items[1] = factory(1)
                lines = []
                self.loop.output = lines.append
                actions = []
                self.co.notices.on_action = lambda number, comment: actions.append(comment) if comment else None
                self.loop.config = replace(self.loop.config, agents=(replace(
                    self.role, command=(sys.executable, "-c", "import time; time.sleep(30)")),))
                if "state" in change:
                    self.loop.config = replace(self.loop.config, stop_labels=())
                labels = change.get("labels", self.github.items[1].labels)

                def run(command, cwd, env, directory, *args, **kwargs):
                    self.scratch = Path(env["UB_AGENTS_SCRATCH"])
                    self.assertTrue(self.scratch.is_dir())
                    if early_report:
                        self.co.report(self.loop._renewal.lease, early_report, "Early report",
                                       outcome="done" if early_report == "success" else None,
                                       action="Maintainer: decide the next step." if early_report == "blocked" else None)

                    def stop(final=False):
                        if not final:
                            self.github.change(1, **change)
                            self.advance()

                    return supervise(command, cwd, env, directory, *args,
                                     **(kwargs | {"observe_output": stop}))

                with patch("ub_agents.assignment_run.supervise", side_effect=run):
                    self.assertTrue(self.loop.tick())
                self.assertIsNone(self.loop._renewal)
                history = self.co.history(1)
                lease = history[0]
                self.assertEqual((lease["state"], lease["result"], lease["attempt_effect"]),
                                 ("released", "blocked", "unchanged"))
                self.assertIn(reason, lease["summary"])
                self.assertTrue(any(reason in line for line in lines))
                self.assertEqual(len(history), 2 if early_report else 1)
                if early_report:
                    self.assertFalse(history[1]["accepted"])
                    if early_report == "success":
                        self.assertFalse(history[1]["transition"]["started"])
                notices = [c for c in self.github.comments(1) if c["body"].startswith(ACTION_MARKER)]
                if "state" in change:
                    self.assertEqual(notices, [])
                    self.assertEqual(actions, [])
                    restarted = Loop(self.loop.config, self.github, "operator", output=lambda *_: None)
                    restarted.coordinator.clock = lambda: self.now
                    with patch("ub_agents.assignment_run.supervise") as execution:
                        self.assertFalse(restarted.tick())
                        self.assertFalse(restarted.tick())
                    execution.assert_not_called()
                    self.assertFalse(any(c["body"].startswith(ACTION_MARKER) for c in self.github.comments(1)))
                else:
                    self.assertEqual(len(notices), 1)
                    self.assertIn("ub-agents retry 1 --agent worker", notices[0]["body"])
                    self.assertEqual(len(actions), 1)
                self.assertEqual(attempts(history, self.role.name, self.now), [])
                self.assertIsNone(self.co.pending_completion(history, self.role.name, self.now))
                self.assertEqual(self.github.items[1].labels, labels)
                self.assertFalse(any(write[0] in {"add-labels", "remove-label"} for write in self.github.writes))
                self.assertFalse(self.scratch.parent.exists())
                directory = self.root / ".ub-agents/runs" / lease["run"]
                self.assertEqual(group_members(int((directory / "pid").read_text())), [])
                self.assertTrue((directory / "process.log").is_file())
                self.assertEqual(self.co._execution_stops, {})

    def test_stop_label_during_workspace_preparation_prevents_execution_and_cleans_worktree(self):
        def prepare(workspace):
            workspace.private.mkdir(parents=True)
            workspace.created = True
            self.worktree = workspace.private
            self.github.change(1, labels=frozenset({"ready", "needs-human"}))
            self.advance()
            return workspace.private

        def remove(root, *args):
            if args[:2] == ("worktree", "remove"):
                Path(args[-1]).rmdir()
            return ""

        with patch.object(Workspace, "prepare", prepare), patch("ub_agents.execution.git", side_effect=remove), \
                patch("ub_agents.assignment_run.supervise") as execution:
            self.assertTrue(self.loop.tick())
        execution.assert_not_called()
        lease, = self.co.history(1)
        self.assertEqual((lease["result"], lease["attempt_effect"]), ("blocked", "unchanged"))
        self.assertFalse(self.worktree.exists())
        self.assertFalse(ScratchDirectory(self.root, self.loop.config.repository, lease["run"]).path.parent.exists())

    def test_label_removed_before_renewal_does_not_stop_execution(self):
        def run(*args, **kwargs):
            self.github.change(1, labels=frozenset({"ready", "needs-human"}))
            self.advance(599)
            self.github.change(1, labels=frozenset({"ready"}))
            self.advance(1)
            self.assertEqual(kwargs["expires"](), self.now + LEASE_SECONDS)
            return self.finish()
        self.execute(run)

    def test_item_reopened_before_renewal_does_not_stop_execution(self):
        for factory in (issue, pr):
            with self.subTest(kind=factory.__name__):
                self.setUp()
                self.github.items[1] = factory(1)

                def run(*args, **kwargs):
                    self.github.change(1, state="closed")
                    self.advance(599)
                    self.github.change(1, state="open")
                    self.advance(1)
                    self.assertEqual(kwargs["expires"](), self.now + LEASE_SECONDS)
                    return self.finish()
                self.execute(run)

    def test_any_configured_stop_label_is_detected_on_an_item_with_an_early_report(self):
        self.loop.config = replace(self.loop.config, stop_labels=("needs-human", "paused"))

        def run(*args, **kwargs):
            self.finish()
            self.github.change(1, labels=frozenset({"ready", "paused"}))
            self.advance()
            # Supervision also checks the latched stop after a fast process exits.
            return 0

        with patch("ub_agents.assignment_run.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        lease, report = self.co.history(1)
        self.assertIn("Stop label paused", lease["summary"])
        self.assertFalse(report["accepted"])

    def test_no_configured_stop_labels_still_reads_item_and_detects_closure(self):
        lease = self.claim()
        with patch.object(self.github, "item", wraps=self.github.item) as read:
            self.assertTrue(self.co.renew(lease, self.github))
        read.assert_called_once_with(1)
        self.assertEqual(self.co.execution_deadline(lease), 2800)
        self.github.change(1, state="closed")
        self.assertTrue(self.co.renew(lease, self.github))
        with self.assertRaisesRegex(ExecutionStopped, "Issue closed"):
            self.co.execution_deadline(lease)

    def test_stop_label_read_failure_retries_without_cancelling_or_losing_ownership(self):
        lease = self.co.claim(self.co.plan(issue(), self.role, self.loop.config.stop_labels),
                              self.loop.config.stop_labels)
        with patch.object(self.github, "item", side_effect=GitHubError("GET", "issue", "unavailable")):
            self.assertFalse(self.co.renew(lease, self.github))
        self.assertEqual(self.co.execution_deadline(lease), 2800)
        self.github.change(1, labels=frozenset({"ready", "needs-human"}))
        self.assertTrue(self.co.renew(lease, self.github))
        with self.assertRaisesRegex(ExecutionStopped, "Stop label needs-human"):
            self.co.execution_deadline(lease)

    def test_item_read_finishing_after_execution_exit_cannot_cancel_completion(self):
        for change in ({"labels": frozenset({"ready", "needs-human"})}, {"state": "closed"}):
            with self.subTest(change=change):
                self.setUp()
                lease = self.co.claim(self.co.plan(issue(), self.role, self.loop.config.stop_labels),
                                      self.loop.config.stop_labels)
                read = self.github.item

                def finish(number, kind=None):
                    self.co.execution_finished(lease)
                    return replace(read(number, kind), **change)

                with patch.object(self.github, "item", side_effect=finish):
                    self.assertTrue(self.co.renew(lease, self.github))
                self.assertEqual(self.co.execution_deadline(lease), 2800)

    def test_stop_checks_end_at_process_exit_before_supervision_final_output(self):
        def run(*args, **kwargs):
            def output(final=False):
                if final:
                    self.github.change(1, state="closed")
                    with patch.object(self.github, "item", side_effect=AssertionError("execution has ended")):
                        self.advance()
            code = supervise(*args, **(kwargs | {"observe_output": output}))
            self.finish()
            return code

        # Use the controlled clock in the real process supervisor as well.
        with patch("ub_agents.execution.time.time", side_effect=lambda: self.now):
            self.execute(run)

    def test_configured_claim_and_renewal_timing(self):
        path = self.root / 'ub-agents.yaml'
        path.write_text('repository: org/project\nlimits: {lease-minutes: 5, lease-renew-minutes: 2}\n'
                        'agents:\n  worker:\n    command: [echo]\n    trigger: ready\n'
                        '    outcomes: {done: {}}\n')
        self.loop = Loop(load_config(path), self.github, "operator", output=lambda *_: None)
        self.co = self.loop.coordinator
        self.co.clock = lambda: self.now

        def run(*args, **kwargs):
            renewal = self.loop._renewal
            lease = renewal.lease
            self.assertEqual(seconds(lease['expires']), 1300)
            self.assertEqual(renewal.next_renewal, 1120)
            with patch.object(self.github, 'update_comment', wraps=self.github.update_comment) as write:
                self.advance(119)
                write.assert_not_called()
                self.advance(1)
                self.assertEqual(seconds(lease['expires']), 1420)
                self.assertEqual(renewal.next_renewal, 1240)
                self.advance(120)
                self.assertEqual(seconds(lease['expires']), 1540)
                self.assertEqual(renewal.next_renewal, 1360)
                self.assertEqual(write.call_count, 2)
            return self.finish()
        self.execute(run)

    def test_shorter_configured_renewal_never_shortens_recorded_expiry(self):
        lease = self.claim()
        # A peer with a shorter lease must still respect this claim's recorded expiry.
        self.co.lease_seconds = 300
        self.now += 120
        self.assertTrue(self.co.renew(lease, self.github))
        self.assertEqual(seconds(lease['expires']), 2800)
        observer = Coordinator(self.github, 'operator', clock=lambda: self.now, lease_seconds=300)
        self.now = 1301
        self.assertEqual(observer.plan(issue(), self.role, ()).state, 'owned')
        self.assertEqual(observer.assert_owned(lease)['expires'], iso(2800))

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
        with patch("ub_agents.assignment_run.supervise", side_effect=run), self.assertRaises(LostOwnership):
            self.loop.tick()
        self.assertEqual(self.github.writes, self.writes_at_expiry)
        self.assertIsNone(self.loop._renewal)

    def test_launch_polls_after_renewal_outage_expires_a_running_process(self):
        for operation in ("comments", "update_comment"):
            with self.subTest(operation=operation):
                self.setUp()
                self.loop.interrupt_event = threading.Event()
                # A real supervised child must stop before polling or recovery.
                self.loop.config = replace(self.loop.config, agents=(replace(
                    self.role, command=(sys.executable, "-c", "import time; time.sleep(30)")),))
                failure = GitHubError("GET" if operation == "comments" else "PATCH", operation,
                                      "GitHub unavailable", retryable=True)
                lines, waits, executions, stopped = [], [], [], []
                self.loop.output = lines.append

                def execution(*args, **kwargs):
                    executions.append(True)
                    lease = self.loop._renewal.lease
                    self.co.report(lease, "success", "Durable early outcome", outcome="done")

                    def expire(final=False):
                        if not final:
                            with patch.object(self.github, operation, side_effect=failure):
                                self.advance()
                                self.advance()
                            stopped.append(self.github.writes.copy())
                            self.now = seconds(lease["expires"])

                    try:
                        return supervise(*args, **(kwargs | {"observe_output": expire}))
                    except LostOwnership as exc:
                        self.assertIs(exc.__cause__, failure)
                        raise

                def wait(delay):
                    waits.append(delay)
                    if len(waits) == 1:
                        self.assertEqual(delay, 5)
                        self.assertEqual(self.github.writes, stopped[0])
                        self.assertIsNone(self.loop._renewal)
                        lease, reported = self.co.history(1)
                        self.assertEqual(lease["state"], "running")
                        self.assertFalse(reported["accepted"])
                        pid = int((self.root / ".ub-agents/runs" / lease["run"] / "pid").read_text())
                        self.assertEqual(group_members(pid), [])
                        self.now += delay
                    else:
                        self.assertGreater(delay, 0)
                        self.assertLessEqual(delay, self.loop.config.poll_seconds)
                        self.loop.stop_event.set()

                with patch("ub_agents.assignment_run.supervise", side_effect=execution), \
                        patch.object(self.loop.stop_event, "wait", side_effect=wait):
                    self.loop.launch()
                self.assertEqual(len(executions), 1)
                self.assertEqual(len(waits), 2)
                self.assertEqual(sum(line.startswith("Skipped GitHub poll:") for line in lines), 1)
                history = self.co.history(1)
                self.assertTrue(history[1]["accepted"])
                self.assertEqual((history[2]["mode"], history[2]["state"]), ("recovery", "released"))

    def test_expiry_retains_the_latest_renewal_error_classification(self):
        for retryable in (True, False):
            with self.subTest(retryable=retryable):
                self.setUp()
                lease = self.claim()
                for classification in (not retryable, retryable):
                    failure = GitHubError("GET", "comments", "failed renewal", retryable=classification)
                    self.now += LEASE_RENEW_SECONDS
                    with patch.object(self.github, "comments", side_effect=failure):
                        self.assertFalse(self.co.renew(lease, self.github))
                writes = self.github.writes.copy()
                self.now = seconds(lease["expires"])
                for _ in range(2):
                    with self.assertRaises(LostOwnership) as raised:
                        self.co.deadline(lease)
                    self.assertIs(raised.exception.__cause__, failure)
                    self.assertEqual(raised.exception.__cause__.retryable, retryable)
                self.assertEqual(self.github.writes, writes)

    def test_successful_renewal_clears_failure_cause_for_later_expiry(self):
        lease = self.claim()
        failure = GitHubError("GET", "comments", "unavailable", retryable=True)
        with patch.object(self.github, "comments", side_effect=failure):
            self.now += LEASE_RENEW_SECONDS
            self.assertFalse(self.co.renew(lease, self.github))
        self.now += LEASE_RENEW_SECONDS
        self.assertTrue(self.co.renew(lease, self.github))
        self.now = seconds(lease["expires"])
        with self.assertRaises(LostOwnership) as raised:
            self.co.deadline(lease)
        self.assertIsNone(raised.exception.__cause__)

    def test_renewal_failure_does_not_make_a_rival_claim_retryable(self):
        lease = self.claim()
        failure = GitHubError("GET", "comments", "unavailable", retryable=True)
        with patch.object(self.github, "comments", side_effect=failure):
            self.now += LEASE_RENEW_SECONDS
            self.assertFalse(self.co.renew(lease, self.github))
        other = payload(lease) | {"run": "rival", "mode": "recovery", "recovered_lease_id": lease["id"],
                                  "recovered_run": lease["run"]}
        self.github.create_comment(1, body(other))
        with self.assertRaises(LostOwnership) as raised:
            self.co.assert_owned(lease)
        self.assertIsNone(raised.exception.__cause__)
        with self.assertRaises(LostOwnership) as raised:
            self.co.deadline(lease)
        self.assertIsNone(raised.exception.__cause__)

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
                patch("ub_agents.assignment_run.supervise", side_effect=AssertionError("outcome recovery starts no agent")):
            self.assertTrue(other.tick())
        history = self.co.history(1)
        self.assertTrue(history[1]["accepted"])
        self.assertEqual((history[2]["mode"], history[2]["state"]), ("recovery", "released"))

    def test_renewal_during_setup_and_completion(self):
        prepare = Workspace.prepare
        finalize = Settlement.finalize
        def setup(workspace):
            for _ in range(4):
                self.advance()
            return prepare(workspace)
        def complete(*args):
            for _ in range(4):
                self.advance()
            return finalize(*args)
        with patch.object(Workspace, "prepare", setup), patch.object(Settlement, "finalize", autospec=True, side_effect=complete):
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
        finalize = Settlement.finalize
        def complete(settlement, recovery, *args):
            self.assertEqual(recovery["mode"], "recovery")
            with patch.object(self.github, "item", side_effect=AssertionError("recovery does not check for stops")):
                for _ in range(4):
                    self.advance()
            return finalize(settlement, recovery, *args)
        with patch.object(Settlement, "finalize", autospec=True, side_effect=complete):
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
