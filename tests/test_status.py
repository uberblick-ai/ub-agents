from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Priority, Queue, Runtime
from ub_agents.coordination import Coordinator
from ub_agents.errors import CleanupError, GitHubError
from ub_agents.loop import Loop
from ub_agents.records import body, iso, seconds
from tests.support import FakeGitHub, PollGitHub, agent, config, edit_lease, issue, pr


class StatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = agent(self.root, kind="issue")
        self.github = FakeGitHub(issue())
        self.loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        self.now = seconds("2026-10-03T09:50:00Z")
        self.loop.coordinator.clock = lambda: self.now
        self.enterContext(patch("ub_agents.cli.socket.gethostname", return_value="local-host"))
        self.groups = self.enterContext(patch("ub_agents.status.group_members", return_value=[]))

    def claim(self, agent_name=None):
        plan = next(p for p in self.loop.plans() if p.agent.name == (agent_name or self.agent.name))
        lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        return lease

    def status(self):
        writes = list(self.github.writes)
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.timestamp", return_value=self.now):
            structured, plain = io.StringIO(), io.StringIO()
            with redirect_stdout(structured):
                self.assertEqual(main(["status", "--json"]), 0)
            with redirect_stdout(plain):
                self.assertEqual(main(["status"]), 0)
        self.assertEqual(self.github.writes, writes)
        return json.loads(structured.getvalue())["assignments"], plain.getvalue()

    def local_lease(self, **changes):
        lease = self.claim()
        edit_lease(self.github, lease, host="local-host", process_group=1234,
                   log_dir="/runs/current", **changes)
        return lease

    def test_running_lease_summary_and_backward_compatible_json(self):
        lease = self.local_lease(runtime="codex:gpt-6.1-sol:xhigh",
                                 created="2026-10-03T09:16:28.166823Z",
                                 expires="2026-10-03T12:31:28.166823Z")
        self.groups.return_value = [1235]
        rows, plain = self.status()
        row = rows[0]
        self.assertEqual({key: value for key, value in row.items()
                          if key not in {"process", "process_reason"}}, {
            "number": 1, "kind": "issue", "agent": "worker", "priority": None,
            "priority_inherited_from": None, "priority_from_issue": None, "open_blockers": [],
            "milestone": None, "milestone_inherited_from": None,
            "state": "owned", "reason": "An unexpired assignment owns this work item", "attempts": 0,
            "runtime": None, "candidate_sha": None, "result": None, "lease": lease, "outcome": None})
        self.assertEqual(row["process"], "running")
        self.assertIn("#1 worker: running", plain)
        self.assertIn("claimed by @operator on local-host at 09:16Z · codex:gpt-6.1-sol:xhigh · "
                      "lease ends 12:31Z (in 2h 41m)", plain)
        self.assertIn("Agent running; log: /runs/current/process.log", plain)
        self.assertNotIn("2026-10-03T", plain)
        self.assertEqual(self.groups.call_count, 2)
        self.groups.assert_called_with(1234)

    def test_exited_without_report_explains_launcher_completion_and_expiry(self):
        self.local_lease()
        rows, plain = self.status()
        self.assertEqual((rows[0]["state"], rows[0]["process"]), ("owned", "exited"))
        self.assertIn("Agent process has exited. Its launcher finishes the run, or another "
                      "launcher recovers it after the lease ends at 09:51Z.", plain)
        self.assertNotIn("ub-agents recover", plain)

    def test_exited_with_report_explains_acceptance_after_expiry(self):
        lease = self.local_lease()
        report = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "exited")
        self.assertEqual(rows[0]["outcome"]["id"], report["id"])
        self.assertIn("reported: success (unaccepted)", plain)
        self.assertIn("Agent process has exited. A launcher accepts the reported outcome when it "
                      "recovers the lease after 09:51Z.", plain)

    def test_latest_outcomes_denials_in_plain_and_json_status(self):
        lease = self.claim()
        report = self.loop.coordinator.report(lease, "retry", "Completed")
        self.loop.coordinator.release(lease, "retry", "Completed")
        for count, omitted in ((0, None), (2, None), (10, 2)):
            with self.subTest(count=count, omitted=omitted):
                fields = {"denials": [{"tool": "Bash", "command": "test"}] * count,
                          "denials_omitted": omitted}
                # Fixture writes model the launcher's update before release.
                self.github.update_comment(report["id"], body(report | fields))
                rows, plain = self.status()
                self.assertEqual(rows[0]["outcome"]["denials"], fields["denials"])
                if count:
                    self.assertIn(f" · {count + (omitted or 0)} denied\n", plain)
                else:
                    self.assertNotIn("denied", plain)
        self.github.change(1, labels=frozenset({"ready"}))
        self.claim()
        _, plain = self.status()
        self.assertNotIn("denied", plain)

    def test_malformed_denials_do_not_invalidate_outcome_or_break_status(self):
        lease = self.claim()
        report = self.loop.coordinator.report(lease, "retry", "Completed")
        for value in (None, "bad", {}, [None], [{"tool": "Bash", "command": 123}]):
            with self.subTest(value=value):
                self.loop.coordinator.update_outcome(lease, report, denials=value, denials_omitted=2)
                rows, plain = self.status()
                self.assertEqual(rows[0]["outcome"]["id"], report["id"])
                self.assertEqual(rows[0]["outcome"].get("denials"), value)
                self.assertIn("reported: retry", plain)
                self.assertNotIn("denied", plain)

    def test_local_lease_without_process_group_is_starting(self):
        lease = self.claim()
        self.loop.coordinator.update(lease, host="local-host")
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "starting")
        self.assertIn("Run is starting; no process group recorded yet.", plain)
        self.groups.assert_not_called()

    def test_claim_without_host_is_in_progress(self):
        lease = self.loop.coordinator.claim(self.loop.plans()[0])
        rows, plain = self.status()
        self.assertEqual(rows[0]["lease"], lease)
        self.assertEqual(rows[0]["process"], "claiming")
        self.assertIn("claimed by @operator on unknown host at 09:50Z · direct · "
                      "lease ends 09:51Z (in 1m)", plain)
        self.assertIn("Claim in progress; host not recorded yet.", plain)
        self.groups.assert_not_called()

    def test_running_record_without_host_has_unknown_process_state(self):
        self.claim()
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "unknown")
        self.assertIn("Process state unknown: host not recorded.", plain)
        self.groups.assert_not_called()

    def test_other_host_is_never_inspected(self):
        lease = self.local_lease()
        self.loop.coordinator.update(lease, host="remote-host")
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "other-host")
        self.assertIn("claimed by @operator on remote-host", plain)
        self.assertIn("Process can't be checked from here", plain)
        self.groups.assert_not_called()

    def test_failed_process_check_does_not_fail_status(self):
        self.local_lease()
        self.groups.side_effect = CleanupError("Cannot inspect owned process group: ps unavailable")
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "unknown")
        self.assertIn("Process state unknown: Cannot inspect owned process group: ps unavailable", plain)
        self.assertIn("#1 worker: owned", plain)

    def test_dates_are_included_only_outside_the_current_utc_day(self):
        self.local_lease(created="2026-10-02T23:59:59.123456Z",
                         expires="2026-10-04T00:00:59.123456Z")
        _, plain = self.status()
        self.assertIn("at 2026-10-02 23:59Z", plain)
        self.assertIn("lease ends 2026-10-04 00:00Z (in 14h 10m)", plain)
        self.assertIn("lease ends at 2026-10-04 00:00Z", plain)
        self.assertNotIn(".123456", plain)

    def test_less_than_one_minute_remaining(self):
        self.local_lease(expires=iso(self.now + 30))
        _, plain = self.status()
        self.assertIn("lease ends 09:50Z (in <1m)", plain)

    def test_no_live_lease_keeps_existing_text_and_skips_process_check(self):
        rows, plain = self.status()
        self.assertIsNone(rows[0]["process"])
        self.assertIsNone(rows[0]["process_reason"])
        self.assertEqual(plain, "#1 worker: ready · priority none · attempts 0\n  Trigger matched\n")
        self.groups.assert_not_called()

    def test_shared_branch_owner_keeps_existing_text(self):
        lease = self.local_lease()
        self.github.items[2] = replace(pr(), branch=f"ub-agents/worker/1/{lease['run']}")
        reviewer = agent(self.root, name="reviewer", kind="pr")
        self.loop.config = config(self.root, self.agent, reviewer)
        self.groups.return_value = [1235]
        rows, plain = self.status()
        row = next(row for row in rows if row["number"] == 2)
        self.assertIsNone(row["lease"])
        self.assertIsNone(row["process"])
        self.assertIn("#2 reviewer: owned · priority none · attempts 0\n"
                      "  A live run on #1 owns this item's branch\n", plain)
        self.assertEqual(self.groups.call_count, 2)  # Only the owning item's two invocations.

    def test_process_check_adds_no_github_reads(self):
        self.github = PollGitHub(issue())
        self.loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        self.loop.coordinator.clock = lambda: self.now
        self.local_lease()
        self.github.reads.clear()
        self.status()
        self.assertEqual(self.github.reads, [
            ("observe", ()), ("repository_comments", (606600,)), ("role", ("operator",)),
            ("blocked_by", (1,)), ("comments", (1,)),
            ("observe", ()), ("repository_comments", (606600,)), ("role", ("operator",)),
            ("blocked_by", (1,)), ("comments", (1,))])

    def test_recovered_success_is_not_a_later_live_runs_report(self):
        source = self.claim()
        reported = self.loop.coordinator.report(source, "success", "Run A completed", outcome="done")
        self.now += 61
        self.assertTrue(self.loop.recover(self.loop.plans()[0]))
        history = self.loop.coordinator.history(1)
        self.assertTrue(history[1]["accepted"])
        self.assertEqual(history[2]["recovered_run"], source["run"])
        self.assertEqual(history[2]["result"], "success")
        self.assertEqual(history[1]["id"], reported["id"])
        self.github.change(1, labels=frozenset({"ready"}))

        later = self.claim()
        self.loop.coordinator.update(later, host="local-host", process_group=1234)
        rows, plain = self.status()
        self.assertEqual((rows[0]["state"], rows[0]["lease"]["run"]), ("owned", later["run"]))
        self.assertIsNone(rows[0]["outcome"])
        self.assertNotIn("reported:", plain)
        self.assertEqual(rows[0]["process"], "exited")
        self.assertNotIn("ub-agents recover", plain)

        # Expiry and release still select B's lease rather than A's report.
        self.now += 61
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertIsNone(rows[0]["outcome"])
        self.assertNotIn("reported:", plain)
        # Model an already released remote record; an expired local supervisor
        # is no longer allowed to write it.
        edit_lease(self.github, later, state="released", result="retry", attempt_effect="failure", expires=iso(self.now))
        rows, plain = self.status()
        self.assertEqual(rows[0]["result"], "retry")
        self.assertIsNone(rows[0]["outcome"])
        self.assertIn("last result: retry", plain)
        self.assertNotIn("reported:", plain)

    def test_owning_runs_report_shows_its_acceptance_then_latest_result(self):
        lease = self.claim()
        reported = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        rows, plain = self.status()
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertFalse(rows[0]["outcome"]["accepted"])
        self.assertIn("reported: success (unaccepted)", plain)

        self.loop.coordinator.accept(lease, reported)
        rows, plain = self.status()
        self.assertTrue(rows[0]["outcome"]["accepted"])
        self.assertIn("reported: success", plain)
        self.assertNotIn("(unaccepted)", plain)

        self.loop.coordinator.release(lease, "success", "Completed")
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertEqual(rows[0]["outcome"]["run"], lease["run"])
        self.assertEqual(rows[0]["result"], "success")
        self.assertIn("last result: success", plain)
        self.assertIn("reported: success", plain)

    def test_live_and_released_recovery_show_the_original_runs_report(self):
        source = self.claim()
        reported = self.loop.coordinator.report(source, "success", "Completed", outcome="done")
        self.now += 61
        recovery = self.loop.coordinator.claim(self.loop.plans()[0], recovery=True)
        self.loop.coordinator.update(recovery, host="local-host", process_group=1234)
        rows, plain = self.status()
        self.assertEqual(rows[0]["lease"]["run"], recovery["run"])
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertIn("reported: success (unaccepted)", plain)
        self.assertEqual(rows[0]["process"], "recovery")
        self.assertIn("Outcome recovery in progress; no agent process to check.", plain)
        self.groups.assert_not_called()

        self.loop.coordinator.accept(recovery, reported)
        self.loop.coordinator.report(recovery, "success", "Recovered completion")
        self.loop.coordinator.release(recovery, "success", "Recovered completion")
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertTrue(rows[0]["outcome"]["accepted"])
        self.assertNotIn("(unaccepted)", plain)

    def test_outcome_must_match_lease_id_and_run_provenance(self):
        lease = self.local_lease()
        reported = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        for changes in ({"lease_id": lease["id"] + 100}, {"lease_id": lease["id"], "run": "other-run"}):
            with self.subTest(changes=changes):
                self.loop.coordinator.update_outcome(lease, reported, **changes)
                rows, plain = self.status()
                self.assertIsNone(rows[0]["outcome"])
                self.assertNotIn("reported:", plain)
                self.assertNotIn("ub-agents recover", plain)

    def test_another_agents_live_lease_does_not_supply_the_rows_report(self):
        other = agent(self.root, name="other", kind="issue")
        for has_previous_report in (False, True):
            with self.subTest(has_previous_report=has_previous_report):
                self.github = FakeGitHub(issue())
                self.loop = Loop(config(self.root, self.agent, other), self.github, "operator",
                                 output=lambda *_: None)
                self.loop.coordinator.clock = lambda: self.now
                previous = None
                if has_previous_report:
                    earlier = self.claim()
                    previous = self.loop.coordinator.report(earlier, "retry", "Earlier worker report")
                    self.loop.coordinator.release(earlier, "retry", "Earlier worker report")
                    self.now += 1

                lease = self.claim(other.name)
                self.loop.coordinator.update(lease, host="local-host", process_group=1234)
                reported = self.loop.coordinator.report(lease, "success", "Other completed", outcome="done")
                rows, plain = self.status()
                by_agent = {row["agent"]: row for row in rows}
                worker_row, other_row = by_agent[self.agent.name], by_agent[other.name]
                self.assertEqual((worker_row["process"], other_row["process"]), ("exited", "exited"))
                self.assertIn("reported outcome", worker_row["process_reason"])
                self.assertNotIn("ub-agents recover", worker_row["process_reason"])
                self.assertNotIn("--agent worker", worker_row["process_reason"])
                self.assertEqual(worker_row["state"], "owned")
                self.assertEqual(worker_row["lease"]["run"], lease["run"])
                self.assertEqual(other_row["outcome"]["id"], reported["id"])
                worker_line = next(line for line in plain.splitlines() if line.startswith("#1 worker:"))
                if previous:
                    self.assertEqual(worker_row["outcome"]["id"], previous["id"])
                    self.assertIn("reported: retry", worker_line)
                else:
                    self.assertIsNone(worker_row["outcome"])
                    self.assertNotIn("reported:", worker_line)
                self.assertIn("reported: success (unaccepted)", plain)
        self.assertEqual(self.groups.call_count, 4)  # One check per lease per invocation, not per row.


class ItemStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "ub-agents.yaml").touch()
        self.config = config(self.root)
        self.github = PollGitHub(issue(11), issue(1, labels=("ready", "urgent")))
        self.now = seconds("2026-10-03T09:50:00Z")
        self.enterContext(patch("ub_agents.cli.socket.gethostname", return_value="local-host"))
        self.groups = self.enterContext(patch("ub_agents.status.group_members", return_value=[]))
        for target in ("ub_agents.loop.Loop.maintain_runtimes", "ub_agents.loop.Loop.execute",
                       "ub_agents.loop.Loop.recover", "ub_agents.loop.Loop.park_approval",
                       "ub_agents.cli.repository_checks", "ub_agents.cli.launch_checks",
                       "ub_agents.cli.launch_output", "ub_agents.updates.Updates",
                       "ub_agents.observations.Publisher"):
            self.enterContext(patch(target, side_effect=AssertionError(f"Status touched {target}")))

    def coordinator(self):
        return Coordinator(self.github, "operator", clock=lambda: self.now, queue=self.config.queue)

    def claim(self, name="worker"):
        co = self.coordinator()
        role = next(a for a in self.config.agents if a.name == name)
        lease = co.claim(co.plan(self.github.items[11], role, ()))
        co.update(lease, state="running", started=True)
        return co, lease

    def invoke(self, *args):
        def create(*args, **kwargs):
            loop = Loop(*args, **kwargs)
            loop.coordinator.clock = lambda: self.now
            return loop

        before = deepcopy((self.github.items, self.github.store, self.github.writes))
        files = list(self.root.iterdir())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop", side_effect=create), \
                patch("ub_agents.cli.timestamp", return_value=self.now), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.root / "ub-agents.yaml"), "status", *args])
        self.assertEqual((self.github.items, self.github.store, self.github.writes), before)
        self.assertEqual(list(self.root.iterdir()), files)
        return code, stdout.getvalue(), stderr.getvalue()

    def status(self):
        code, structured, errors = self.invoke("11", "--json")
        self.assertEqual((code, errors), (0, ""))
        code, plain, errors = self.invoke("11")
        self.assertEqual((code, errors), (0, ""))
        forbidden = {"observe", "repository_comments", "dependency_graph", "milestone_order", "active_milestone"}
        self.assertFalse(forbidden.intersection(name for name, _ in self.github.reads), self.github.reads)
        item_reads = {"item", "comments", "timeline", "issue_content", "pr_content", "reviews",
                      "review_comments", "blocked_by"}
        self.assertTrue(all(args[0] == 11 for name, args in self.github.reads if name in item_reads),
                        self.github.reads)
        return json.loads(structured), plain

    def test_ready_rows_for_each_matching_agent_only(self):
        self.config = config(self.root, agent(self.root, name="first", kind="issue"),
                             agent(self.root, name="second", kind="either"),
                             agent(self.root, name="reviewer", kind="pr"),
                             agent(self.root, name="other", triggers=("other",)))
        result, plain = self.status()
        self.assertEqual(set(result), {"assignments"})
        self.assertEqual([(r["number"], r["agent"], r["state"], r["attempts"])
                          for r in result["assignments"]], [(11, "first", "ready", 0), (11, "second", "ready", 0)])
        self.assertEqual(plain, "#11 first: ready · priority none · attempts 0\n  Trigger matched\n"
                               "#11 second: ready · priority none · attempts 0\n  Trigger matched\n")

    def test_no_rows_explain_untriggered_closed_and_inapplicable_items(self):
        worker = agent(self.root, kind="issue", triggers=("ready",))
        second = agent(self.root, name="second", kind="either", triggers=("prepare",))
        reviewer = agent(self.root, name="reviewer", kind="pr", triggers=("needs-review",))
        for item, agents, reason in (
                (issue(11, labels=()), (worker, second, reviewer),
                 "No trigger matches; add a trigger label (worker: ready; second: prepare)"),
                (replace(issue(11), state="closed"), (worker,), "issue is closed"),
                (replace(pr(11), state="closed"), (reviewer,), "pr is closed"),
                (issue(11), (reviewer,), "No evaluated agent applies to this issue"),
                (pr(11, labels=()), (reviewer,),
                 "No trigger matches; add a trigger label (reviewer: needs-review)")):
            with self.subTest(kind=item.kind, state=item.state, reason=reason):
                self.github.items[11] = item
                self.config = config(self.root, *agents)
                result, plain = self.status()
                self.assertEqual(result, {"assignments": [], "explanation": reason})
                self.assertEqual(plain, f"#11: {reason}\n")

    def test_own_priority_and_milestone_without_inheritance_or_ordering(self):
        self.config = config(self.root, queue=Queue(milestones="order", priority=Priority(("urgent", "normal"))))
        self.github.change(1, milestone=3)
        self.github.dependencies[1] = [11]
        for item in (issue(11, labels=("ready", "normal"), milestone=7),
                     pr(11, labels=("ready", "normal"), milestone=7)):
            with self.subTest(kind=item.kind):
                self.github.items[11] = item
                result, plain = self.status()
                row = result["assignments"][0]
                self.assertEqual((row["priority"], row["milestone"]), ("normal", 7))
                self.assertIsNone(row["priority_inherited_from"])
                self.assertIsNone(row["priority_from_issue"])
                self.assertIsNone(row["milestone_inherited_from"])
                self.assertIn("priority normal", plain)
                if item.kind == "issue":
                    self.assertIn("milestone #7", plain)

    def test_approval_stop_and_dependency_gates_are_read_only(self):
        self.github.timelines[11] = []
        result, plain = self.status()
        self.assertEqual(result["assignments"][0]["state"], "parked")
        self.assertIn("No maintainer", plain)
        self.github.timelines.pop(11)
        self.github.change(11, labels=frozenset({"ready", "needs-human"}))
        result, plain = self.status()
        self.assertEqual(result["assignments"][0]["reason"], "Stop label needs-human is present")
        self.github.change(11, labels=frozenset({"ready"}))
        self.github.dependencies[11] = [1]
        result, plain = self.status()
        self.assertEqual(result["assignments"][0]["reason"], "Waiting for blockers #1")
        self.assertEqual(result["assignments"][0]["open_blockers"], ["#1"])

    def test_later_and_unmilestoned_targets_are_ready_under_milestone_gate(self):
        self.config = config(self.root, queue=Queue(milestones="gate"))
        self.github.milestones = [{"number": 3, "state": "open", "created_at": "2026-01-01T00:00:00Z"}]
        self.github.change(1, milestone=3)
        for milestone in (20, None):
            with self.subTest(milestone=milestone):
                self.github.change(11, milestone=milestone)
                result, plain = self.status()
                self.assertEqual(result["assignments"][0]["state"], "ready")
                self.assertIn("#11 worker: ready", plain)
                self.assertNotIn("Waiting for active milestone", plain)

    def test_pr_candidate_and_outside_edit_approval_gate(self):
        self.github.items[11] = pr(11)
        self.github.roles["outsider"] = "read"
        self.github.content_histories[11] = {"lastEditedAt": "2026-01-02T00:00:00Z", "edits": [
            {"editedAt": "2026-01-02T00:00:00Z", "editor": {"login": "outsider"},
             "diff": "Changed requirements", "deletedAt": None}]}
        result, plain = self.status()
        row = result["assignments"][0]
        self.assertEqual((row["kind"], row["candidate_sha"], row["state"]), ("pr", "a" * 40, "parked"))
        self.assertIn("Outside body edit", plain)

    def test_runtime_wait_and_unavailability_match_launch_gates(self):
        self.config = config(self.root, agent(self.root, command=(), runtimes=(Runtime("codex", "model", "high"),)))
        pause = {"reason": "usage limit reached", "ends_at": "2026-10-04T00:00:00Z"}
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", return_value=True), \
                patch("ub_agents.runtime_usage.RuntimeUsage.paused", return_value=pause):
            result, plain = self.status()
        self.assertEqual(result["assignments"][0]["state"], "waiting")
        self.assertIn("Waiting for codex: usage limit reached; pause ends 2026-10-04T00:00:00Z", plain)
        with patch("ub_agents.runtime_updates.RuntimeMaintenance.available", return_value=False):
            result, plain = self.status()
        self.assertEqual(result["assignments"][0]["state"], "blocked")
        self.assertIn("No eligible runtime executable is installed, usable and available", plain)

    def test_attempts_backoff_last_result_and_reported_outcome(self):
        co, lease = self.claim()
        reported = co.report(lease, "retry", "Try later")
        co.release(lease, "retry", "Try later", 120, attempt_effect="failure")
        result, plain = self.status()
        row = result["assignments"][0]
        self.assertEqual((row["state"], row["attempts"], row["result"]), ("backoff", 1, "retry"))
        self.assertEqual(row["outcome"]["id"], reported["id"])
        self.assertIn("attempts 1 · last result: retry · reported: retry", plain)
        self.config = config(self.root, replace(self.config.agents[0], max_attempts=1))
        result, plain = self.status()
        self.assertEqual(result["assignments"][0]["state"], "blocked")
        self.assertIn("Attempt limit exhausted", plain)

    def test_live_owner_and_report_without_trigger(self):
        co, lease = self.claim()
        co.update(lease, host="local-host", process_group=1234, log_dir="/runs/current")
        reported = co.report(lease, "success", "Completed", outcome="done")
        self.github.change(11, labels=frozenset())
        self.groups.return_value = [1235]
        result, plain = self.status()
        row = result["assignments"][0]
        self.assertEqual((row["state"], row["process"], row["lease"]["id"]), ("owned", "running", lease["id"]))
        self.assertEqual(row["outcome"]["id"], reported["id"])
        self.assertIn("#11 worker: running", plain)
        self.assertIn("claimed by @operator on local-host", plain)
        self.assertIn("reported: success (unaccepted)", plain)

    def test_recovery_of_open_and_closed_work_does_not_accept_or_execute(self):
        co, lease = self.claim()
        reported = co.report(lease, "success", "Completed before launcher stopped", outcome="done")
        self.now += 61
        for state in ("open", "closed"):
            with self.subTest(state=state):
                self.github.change(11, state=state, labels=frozenset({"needs-human"}))
                result, plain = self.status()
                row = result["assignments"][0]
                self.assertEqual((row["state"], row["outcome"]["id"]), ("recover", reported["id"]))
                self.assertFalse(row["outcome"]["accepted"])
                self.assertIn("#11 worker: recover", plain)
                self.assertIn("An expired run has an explicit outcome to validate without reexecution", plain)

    def test_unreadable_item_exits_one_with_named_error(self):
        for args in (("11",), ("11", "--json")):
            with self.subTest(args=args):
                self.github.read_results["item"] = [GitHubError("GET", "repos/org/project/issues/11", "HTTP 404")]
                code, stdout, errors = self.invoke(*args)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("Cannot read #11", errors)
                self.assertIn("HTTP 404", errors)
