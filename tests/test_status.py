from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import CleanupError
from ub_agents.loop import Loop
from ub_agents.records import iso, seconds
from tests.support import FakeGitHub, PollGitHub, agent, config, issue, pr


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
        self.loop.coordinator.update(lease, host="local-host", process_group=1234,
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

    def test_exited_with_report_explains_acceptance_and_manual_recovery(self):
        lease = self.local_lease()
        report = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        rows, plain = self.status()
        self.assertEqual(rows[0]["process"], "exited")
        self.assertEqual(rows[0]["outcome"]["id"], report["id"])
        self.assertIn("reported: success (unaccepted)", plain)
        self.assertIn("Agent process has exited. A launcher accepts the reported outcome when it "
                      "recovers the lease after 09:51Z, or run "
                      "`ub-agents recover --number 1 --agent worker --reason TEXT` now.", plain)

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
            ("observe", ()), ("repository_comments", (604860,)), ("role", ("operator",)),
            ("blocked_by", (1,)), ("comments", (1,)),
            ("observe", ()), ("repository_comments", (604860,)), ("role", ("operator",)),
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
        self.loop.coordinator.update(later, state="released", result="retry", attempt_effect="failure")
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
                self.assertIn("--number 1 --agent other --reason TEXT", worker_row["process_reason"])
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
