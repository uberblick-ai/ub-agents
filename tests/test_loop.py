from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main, status_rows
from ub_agents.config import Priority, Queue
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.records import attempts, iso, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root, kind="issue")
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

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.preparer = agent(self.root, name="preparer", kind="issue", triggers=("prepare",))
        # An either-kind implementer also handles requested changes on PRs.
        self.implementer = agent(self.root, name="implementer", triggers=("ready", "needs-changes"))
        self.github = FakeGitHub()
        self.loop = Loop(config(self.root, self.preparer, self.implementer, queue=Queue("gate")), self.github,
                         "operator", output=lambda *_: None)
        self.github.milestones = [
            {"number": 20, "state": "open", "created_at": iso(200)},
            {"number": 10, "state": "open", "created_at": iso(100)},
        ]

    def add(self, *items):
        self.github.items.update({item.number: item for item in items})

    def ready(self, name):
        return [p.item.number for p in self.loop.plans() if p.state == "ready" and p.agent.name == name]

    def test_each_issue_queue_uses_fifo_and_its_own_eligibility(self):
        self.add(issue(1, ("prepare",), iso(300), 10),
                 issue(2, ("ready",), iso(200), 10),
                 issue(30, ("prepare",), iso(100), 10),
                 issue(40, ("ready",), iso(50), 10))
        self.assertEqual(self.ready("preparer"), [30, 1])
        self.assertEqual(self.ready("implementer"), [40, 2])
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 40)

    def test_equal_creation_times_use_issue_number_for_both_agents(self):
        self.add(issue(30, ("prepare", "ready"), iso(100), 10),
                 issue(2, ("prepare", "ready"), iso(100), 10))
        self.assertEqual(self.ready("preparer"), [2, 30])
        self.assertEqual(self.ready("implementer"), [2, 30])

    def test_ignore_mode_never_reads_milestones_in_planning_or_claiming(self):
        for queue in (Queue(), Queue("ignore")):
            with self.subTest(queue=queue):
                self.github = FakeGitHub(issue(1, ("ready",), iso(300), 20),
                                         issue(2, ("ready",), iso(200)),
                                         issue(3, ("ready",), iso(100), 10))
                self.loop = Loop(config(self.root, self.implementer, queue=queue),
                                 self.github, "operator", output=lambda *_: None)
                with patch.object(self.github, "active_milestone",
                                  side_effect=AssertionError("ignore must not read milestones")):
                    self.assertEqual(self.ready("implementer"), [3, 2, 1])
                    for plan in self.loop.plans():
                        self.assertIsNotNone(self.loop.coordinator.claim(plan))

    def test_priority_highest_multiple_labels_default_and_fifo(self):
        priority = Priority(("urgent", "high", "normal", "low"), "normal")
        self.loop = Loop(config(self.root, self.preparer, self.implementer,
                                queue=Queue(priority=priority)),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "low"), iso(1)),
                 issue(2, ("ready",), iso(200)),
                 issue(3, ("ready", "normal"), iso(100)),
                 issue(4, ("ready", "low", "urgent"), iso(400)),
                 issue(5, ("ready", "high"), iso(300)),
                 issue(6, ("ready", "normal"), iso(100)),
                 issue(7, ("ready", "unconfigured"), iso(150)))
        self.assertEqual(self.ready("implementer"), [4, 5, 3, 6, 7, 2, 1])
        labels = {n: item.labels for n, item in self.github.items.items()}
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 4)
        self.assertEqual({n: item.labels for n, item in self.github.items.items()}, labels)
        self.assertEqual(self.github.writes, [])

    def test_omitted_priority_default_ranks_unlabeled_last(self):
        self.loop = Loop(config(self.root, self.implementer,
                                queue=Queue(priority=Priority(("urgent", "low")))),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready",), iso(1)),
                 issue(2, ("ready", "low"), iso(200)),
                 issue(3, ("ready", "urgent"), iso(300)),
                 issue(4, ("ready", "other"), iso(2)))
        self.assertEqual(self.ready("implementer"), [3, 2, 1, 4])
        self.assertEqual([r["priority"] for r in status_rows(self.loop)],
                         ["urgent", "low", None, None])

    def test_pr_work_runs_before_issues_and_uses_same_priority_fifo_rank(self):
        self.loop = Loop(config(self.root, self.implementer,
                                queue=Queue("gate", Priority(("urgent", "low")))),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "urgent"), iso(1), 10),
                 issue(2, ("ready", "urgent"), iso(2), 20),
                 pr(3, ("needs-changes", "low"), body="Unrelated", milestone=20),
                 replace(pr(4, ("needs-changes", "low"), body="Unrelated"), created_at=iso(100)),
                 replace(pr(5, ("needs-changes", "urgent"), body="Unrelated"), created_at=iso(300)),
                 replace(pr(6, ("needs-changes", "low"), body="Unrelated"), created_at=iso(100)))
        self.assertEqual(self.ready("implementer"), [5, 4, 6, 3, 1])
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 5)
        self.assertIsNotNone(self.loop.coordinator.claim(execute.call_args.args[0]))

    def test_default_queue_is_fifo_with_number_ties_for_both_work_classes(self):
        self.loop = Loop(config(self.root, self.implementer), self.github,
                         "operator", output=lambda *_: None)
        self.add(issue(1, ("ready",), iso(300)), issue(8, ("ready",), iso(100)),
                 issue(9, ("ready",), iso(100)),
                 replace(pr(2), created_at=iso(300)),
                 replace(pr(3), created_at=iso(100)),
                 replace(pr(4), created_at=iso(100)))
        self.assertEqual(self.ready("implementer"), [3, 4, 2, 8, 9, 1])
        self.assertTrue(all(row["priority"] is None for row in status_rows(self.loop)))

    def test_priority_does_not_bypass_stop_labels_backoff_or_attempt_limits(self):
        self.loop = Loop(config(self.root, self.implementer,
                                queue=Queue(priority=Priority(("urgent", "low")))),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "urgent")), issue(2, ("ready", "low")))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        old = co.claim(co.plan(self.github.item(1), self.implementer, ()))
        co.update(old, state="running", started=True)
        co.release(old, "retry", "Retry later", 60)
        self.assertEqual(self.ready("implementer"), [2])
        self.assertEqual(self.loop.plans()[0].state, "backoff")
        now += 61
        self.github.change(1, labels=frozenset({"ready", "urgent", "needs-human"}))
        self.assertEqual(self.ready("implementer"), [2])
        self.assertEqual(self.loop.plans()[0].state, "parked")
        self.github.change(1, labels=frozenset({"ready", "urgent"}))
        worker = replace(self.implementer, max_attempts=1)
        limited = Loop(config(self.root, worker, queue=self.loop.config.queue),
                       self.github, "operator", output=lambda *_: None)
        limited.coordinator.clock = lambda: now
        self.assertEqual(limited.plans()[0].state, "blocked")
        self.assertIn("Attempt limit", limited.plans()[0].reason)
        with patch.object(limited, "execute", return_value=True) as execute:
            self.assertTrue(limited.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 2)

    def test_priority_never_allows_later_or_unmilestoned_issue_to_start(self):
        self.loop = Loop(config(self.root, self.implementer,
                                queue=Queue("gate", Priority(("urgent", "low")))),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "urgent"), iso(1), 20),
                 issue(2, ("ready", "urgent"), iso(2)),
                 issue(3, ("ready", "low"), iso(300), 10))
        self.assertEqual(self.ready("implementer"), [3])
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 3)

    def test_agents_on_same_item_keep_yaml_order(self):
        self.add(issue(30, ("prepare", "ready"), iso(100), 10),
                 issue(2, ("prepare", "ready"), iso(100), 10))
        self.assertEqual([(p.item.number, p.agent.name) for p in self.loop.plans()],
                         [(2, "preparer"), (2, "implementer"),
                          (30, "preparer"), (30, "implementer")])

    def test_status_text_and_json_share_rank_priority_and_milestone_wait(self):
        settings = config(self.root, self.implementer,
                          queue=Queue("gate", Priority(("urgent", "normal", "low"), "normal")))
        self.loop = Loop(settings, self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "urgent"), iso(1), 20),
                 issue(2, ("ready",), iso(2)),
                 issue(3, (), iso(300), 10), pr(4, ("needs-changes", "low"), milestone=20))
        rows = status_rows(self.loop)
        self.assertEqual([r["number"] for r in rows], [4, 1, 2])
        self.assertEqual([r["priority"] for r in rows], ["urgent", "urgent", "normal"])
        self.assertEqual(rows[0]["priority_from_issue"], 1)
        for row in rows[1:]:
            self.assertEqual(row["state"], "parked")
            self.assertEqual(row["reason"], "Waiting for active milestone #10")
        with patch("ub_agents.cli.load_config", return_value=settings), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            structured = io.StringIO()
            with redirect_stdout(structured):
                self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(structured.getvalue()), rows)
            plain = io.StringIO()
            with redirect_stdout(plain):
                self.assertEqual(main(["status"]), 0)
        output = plain.getvalue()
        self.assertLess(output.index("#4 implementer"), output.index("#1 implementer"))
        self.assertLess(output.index("#1 implementer"), output.index("#2 implementer"))
        for priority in ("urgent", "normal"):
            self.assertIn(f"priority {priority}", output)
        self.assertIn("urgent (from closed issue #1)", output)
        self.assertEqual(output.count("Waiting for active milestone #10"), 2)
        self.assertEqual(self.github.writes, [])

    def test_later_and_unmilestoned_issues_wait_when_active_has_no_eligible_issue(self):
        self.add(issue(1, ("prepare", "ready"), iso(1), 20),
                 issue(2, ("prepare", "ready"), iso(2)),
                 issue(3, (), iso(300), 10))
        with patch.object(self.loop, "execute") as execute:
            self.assertFalse(self.loop.tick())
        execute.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(1, "parked"), (1, "parked"), (2, "parked"), (2, "parked")])

    def test_closing_active_milestone_advances_even_with_open_items(self):
        self.add(issue(1, ("prepare", "ready"), iso(1), 20), issue(2, (), iso(2), 10))
        self.assertEqual(self.ready("preparer"), [])
        self.github.milestones[1]["state"] = "closed"
        self.assertEqual(self.ready("preparer"), [1])
        self.assertEqual(self.ready("implementer"), [1])

    def test_closing_last_issue_or_pr_advances_to_next_milestone(self):
        for remaining in (issue(2, (), milestone=10), pr(2, (), milestone=10)):
            with self.subTest(kind=remaining.kind):
                self.add(issue(1, ("prepare", "ready"), iso(1), 20), remaining)
                self.assertEqual(self.ready("preparer"), [])
                self.assertEqual(self.ready("implementer"), [])
                self.github.change(2, state="closed")
                self.assertEqual(self.ready("preparer"), [1])
                self.assertEqual(self.ready("implementer"), [1])

    def test_empty_milestone_is_skipped_and_equal_milestones_use_number(self):
        self.github.milestones[0]["created_at"] = iso(100)
        self.add(issue(1, ("prepare", "ready"), iso(1), 20),
                 issue(2, ("prepare", "ready"), iso(200), 10))
        self.assertEqual(self.ready("preparer"), [2])
        self.github.change(2, state="closed")
        self.assertEqual(self.ready("preparer"), [1])

    def test_no_active_milestone_uses_global_fifo_for_each_agent(self):
        for milestones in ([], [dict(m, state="closed") for m in self.github.milestones],
                           [{"number": 99, "state": "open", "created_at": iso(1)}]):
            with self.subTest(milestones=milestones):
                self.github.milestones = milestones
                self.add(issue(1, ("prepare", "ready"), iso(300)),
                         issue(10, ("prepare", "ready"), iso(100), 20),
                         issue(20, ("prepare", "ready"), iso(100)))
                self.assertEqual(self.ready("preparer"), [10, 20, 1])
                self.assertEqual(self.ready("implementer"), [10, 20, 1])

    def test_pr_agents_are_ungated_and_use_creation_time_then_number(self):
        reviewer = agent(self.root, name="reviewer", kind="pr", triggers=("review",))
        integrator = agent(self.root, name="integrator", kind="pr", triggers=("merge",))
        self.loop = Loop(config(self.root, self.preparer, self.implementer, reviewer, integrator,
                                queue=Queue("gate")),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, (), milestone=10), pr(2, ("review",), milestone=20),
                 pr(3, ("merge",)), pr(4, ("needs-changes",), milestone=20))
        self.assertEqual(self.ready("reviewer"), [2])
        self.assertEqual(self.ready("integrator"), [3])
        self.assertEqual(self.ready("implementer"), [4])
        for number in (2, 3, 4):
            with patch.object(self.loop, "execute", return_value=True) as execute:
                self.assertTrue(self.loop.tick())
            plan = execute.call_args.args[0]
            self.assertEqual(plan.item.number, number)
            self.assertIsNotNone(self.loop.coordinator.claim(plan))
            self.github.change(number, labels=frozenset())

    def test_recovery_outside_active_milestone_runs_without_execution(self):
        self.loop = Loop(config(self.root, self.implementer,
                                queue=Queue("gate", Priority(("urgent", "low")))),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "urgent"), iso(1), 10),
                 issue(2, ("ready", "low"), iso(300), 20))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        # The run was claimed before an earlier milestone became active.
        self.github.milestones[1]["state"] = "closed"
        plan = co.plan(self.github.item(2), self.implementer, ())
        lease = co.claim(plan)
        co.update(lease, state="running", started=True)
        self.github.milestones[1]["state"] = "open"
        self.github.change(2, labels=frozenset())
        co.report(lease, "success", "Existing work completed")
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(2, "owned"), (1, "ready")])
        # Recovery remains ahead of a higher-priority new start after expiry,
        # even when the old item closed and lost its trigger.
        self.github.change(2, state="closed")
        now += 61
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(2, "recover"), (1, "ready")])
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(self.loop.tick())
        self.assertTrue(co.history(2)[1]["accepted"])
        recovery = next(r for r in co.history(2) if r.get("mode") == "recovery")
        self.assertEqual(recovery["result"], "success")

    def test_claim_rechecks_active_milestone_and_current_membership(self):
        self.add(issue(1, ("ready",), milestone=20))
        plan = next(p for p in self.loop.plans() if p.state == "ready")
        self.add(issue(2, (), milestone=10))
        self.assertIsNone(self.loop.coordinator.claim(plan))
        self.github.change(2, state="closed")
        self.github.change(1, milestone=None)
        self.add(pr(3, (), milestone=20))
        self.assertIsNone(self.loop.coordinator.claim(plan))
        self.assertEqual(self.github.writes, [])

    def test_milestone_read_failure_stops_selection_without_claiming(self):
        self.add(issue())
        with patch.object(self.github, "active_milestone", side_effect=AgentError("GitHub unavailable")):
            with self.assertRaises(AgentError):
                self.loop.tick()
        self.assertEqual(self.github.writes, [])
