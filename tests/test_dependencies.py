from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main, status_rows
from ub_agents.config import Priority, Queue
from ub_agents.errors import AgentError
from ub_agents.github import Dependency
from ub_agents.loop import Loop
from ub_agents.records import iso, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.refresh = stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.preparer = agent(self.root, name="preparer", kind="issue", triggers=("prepare",))
        self.github = FakeGitHub()
        self.priority = Priority(("priority:urgent", "priority:high", "priority:normal", "priority:low"),
                                 "priority:normal")
        self.loop = self.make_loop()

    def make_loop(self, **queue):
        return Loop(config(self.root, self.worker, self.preparer,
                           queue=Queue(priority=self.priority, **queue)),
                    self.github, "operator", output=lambda *_: None)

    def add(self, *items):
        self.github.items.update({i.number: i for i in items})

    def plans(self):
        return {p.item.number: p for p in self.loop.plans() if p.agent == self.worker}

    def test_zero_totals_skip_reads_but_unqueued_dependents_still_propagate(self):
        self.add(replace(issue(1, ("ready", "priority:low")), total_blocked_by=0),
                 replace(issue(2, ("priority:urgent",)), total_blocked_by=1),
                 issue(3, ()), replace(issue(4), total_blocked_by=0))
        self.github.dependencies[2] = [1]
        graph = self.github.dependency_graph()
        with patch.object(self.github, "dependency_graph", return_value=graph) as graph_read, \
                patch.object(self.github, "blocked_by", wraps=self.github.blocked_by) as read:
            for _ in range(2):
                plans = self.plans()
                self.assertEqual((plans[1].priority, plans[1].priority_source, plans[1].state),
                                 ("priority:urgent", 2, "ready"))
                self.assertEqual(plans[4].state, "ready")
            self.assertEqual(graph_read.call_count, 2)
            read.assert_not_called()
        # A fresh observation must not reuse the earlier zero summary.
        self.github.change(1, total_blocked_by=1)
        self.github.dependencies[1] = [4]
        self.assertEqual(self.plans()[1].state, "parked")

    def test_positive_total_with_no_open_blockers_still_reads_links(self):
        self.add(replace(issue(1), total_blocked_by=1),
                 replace(issue(2, ()), state="closed"))
        self.github.dependencies[1] = [2]
        with patch.object(self.github, "blocked_by", wraps=self.github.blocked_by) as read:
            self.assertEqual(self.plans()[1].state, "ready")
        read.assert_called_once_with(1)

    def test_empty_plans_skip_dependency_reads(self):
        self.add(issue(1, ()), issue(2, ()))
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("no work to rank")):
            self.assertEqual(self.loop.plans(), [])

    def test_blocker_status_sorts_by_repository_and_number(self):
        self.add(issue(1))
        self.github.dependencies[1] = [Dependency("org/project", 10, "open"),
                                       Dependency("other/project", 10, "open"),
                                       Dependency("org/project", 9, "open"),
                                       Dependency("other/project", 9, "open")]
        self.assertEqual(self.plans()[1].reason,
                         "Waiting for blockers #9, #10, other/project#9, other/project#10")

    def test_one_and_several_open_blockers_gate_preparation_and_implementation(self):
        self.add(issue(1, ("prepare", "ready")), issue(31, ()), issue(32, ()))
        for blockers in ([31], [31, 32]):
            with self.subTest(blockers=blockers):
                self.github.dependencies[1] = blockers
                plans = self.loop.plans()
                self.assertEqual([p.state for p in plans], ["parked", "parked"])
                self.assertTrue(all(p.reason == "Waiting for blockers " +
                                    ", ".join(f"#{n}" for n in blockers) for p in plans))
                with patch.object(self.loop, "execute") as execute:
                    self.assertFalse(self.loop.tick())
                execute.assert_not_called()
        self.github.change(31, state="closed")
        self.assertTrue(all(p.reason == "Waiting for blockers #32" for p in self.loop.plans()))
        self.github.change(32, state="closed")
        self.assertTrue(all(p.state == "ready" for p in self.loop.plans()))
        self.assertIsNotNone(self.loop.coordinator.claim(self.loop.plans()[0]))

    def test_no_queue_block_defaults_to_wait(self):
        self.add(issue(1), issue(2, ()))
        self.github.dependencies[1] = [2]
        loop = Loop(config(self.root, self.worker), self.github, "operator")
        self.assertEqual(loop.plans()[0].state, "parked")
        self.assertIsNone(loop.coordinator.claim(loop.coordinator.plan(issue(1), self.worker, ())))

    def test_external_blocker_gates_without_colliding_with_local_number(self):
        self.add(issue(1), issue(31, ("ready",)))
        self.github.dependencies[1] = [Dependency("elsewhere/project", 31, "open")]
        self.assertEqual(self.plans()[1].reason, "Waiting for blockers elsewhere/project#31")
        self.assertEqual(self.plans()[31].state, "ready")
        self.assertIsNone(self.loop.coordinator.claim(
            self.loop.coordinator.plan(issue(1), self.worker, ())))
        self.github.dependencies[1] = [Dependency("elsewhere/project", 31, "closed")]
        self.assertEqual(self.plans()[1].state, "ready")

    def test_claim_freshly_reads_blockers_before_writing_a_lease(self):
        self.add(*(replace(issue(n, ("ready",) if n == 1 else ()), total_blocked_by=0)
                   for n in (1, 31, 32)))
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("known zero totals")):
            plan = self.plans()[1]
        self.github.dependencies[1] = [31, 32]
        self.assertIsNone(self.loop.coordinator.claim(plan))
        self.github.change(31, state="closed")
        self.assertIsNone(self.loop.coordinator.claim(plan))
        self.assertEqual(self.github.writes, [])
        self.github.change(32, state="closed")
        self.assertIsNotNone(self.loop.coordinator.claim(plan))

    def test_ignore_reads_no_dependencies_and_disables_gate_and_inheritance(self):
        self.add(issue(1, ("ready", "priority:low")),
                 issue(2, ("ready", "priority:urgent")))
        self.github.dependencies = {2: [1], 1: [2]}
        self.loop = self.make_loop(dependencies="ignore")
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("ignore must not read")):
            plans = self.loop.plans()
            self.assertEqual([(p.item.number, p.priority, p.priority_source, p.state) for p in plans],
                             [(2, "priority:urgent", None, "ready"),
                              (1, "priority:low", None, "ready")])
            self.assertIsNotNone(self.loop.coordinator.claim(plans[0]))

    def test_transitive_inheritance_includes_unqueued_open_dependents(self):
        self.add(issue(1, ("ready", "priority:low"), iso(300)),
                 issue(2, ("priority:high",)), issue(21, ("priority:urgent",)),
                 issue(4, ("ready", "priority:high"), iso(1)))
        self.github.dependencies = {21: [2], 2: [1]}
        plans = self.plans()
        self.assertEqual((plans[1].priority, plans[1].priority_source), ("priority:urgent", 21))
        self.assertEqual([p.item.number for p in self.loop.plans()], [1, 4])
        # Closing the end dependent leaves the middle issue's own priority.
        self.github.change(21, state="closed")
        self.assertEqual((self.plans()[1].priority, self.plans()[1].priority_source),
                         ("priority:high", 2))
        # A closed intermediate node breaks the path from an open urgent issue.
        self.github.change(21, state="open")
        self.github.change(2, state="closed")
        self.assertEqual((self.plans()[1].priority, self.plans()[1].priority_source),
                         ("priority:low", None))

    def test_several_dependents_highest_priority_and_default_are_inherited(self):
        self.add(issue(1, ("ready", "priority:low")), issue(2, ()),
                 issue(3, ("priority:high", "priority:urgent")))
        self.github.dependencies = {2: [1], 3: [1]}
        self.assertEqual((self.plans()[1].priority, self.plans()[1].priority_source),
                         ("priority:urgent", 3))
        self.github.change(3, state="closed")
        self.assertEqual((self.plans()[1].priority, self.plans()[1].priority_source),
                         ("priority:normal", 2))

    def test_cycles_take_highest_reachable_priority_and_keep_own_equal_priority(self):
        self.add(issue(1, ("ready", "priority:low")), issue(2),
                 issue(3, ("ready", "priority:high")), issue(21, ("priority:urgent",)))
        self.github.dependencies = {1: [2], 2: [3], 3: [1], 21: [2]}
        plans = self.plans()
        self.assertTrue(all(p.priority == "priority:urgent" and p.priority_source == 21
                            and p.state == "parked" for p in plans.values()))
        self.github.change(21, state="closed")
        plans = self.plans()
        self.assertEqual((plans[1].priority, plans[1].priority_source), ("priority:high", 3))
        self.assertEqual((plans[3].priority, plans[3].priority_source), ("priority:high", None))

    def test_self_dependency_and_long_chain_terminate(self):
        self.add(*(issue(n, ("ready", "priority:low")) for n in range(1, 1100)),
                 issue(1100, ("priority:urgent",)))
        self.github.dependencies = {n: [n - 1] for n in range(2, 1101)}
        self.github.dependencies[1] = [1]
        plans = self.plans()
        self.assertEqual((plans[1].priority, plans[1].priority_source, plans[1].state),
                         ("priority:urgent", 1100, "parked"))

    def test_dependency_wait_still_applies_with_milestone_order_and_inherited_priority(self):
        self.add(issue(1, ("ready", "priority:low"), milestone=20),
                 issue(2, (), milestone=10), issue(21, ("priority:urgent",)))
        self.github.dependencies = {1: [2], 21: [1]}
        self.github.milestones = [{"number": 10, "state": "open", "created_at": iso(1)}]
        self.loop = self.make_loop(milestones="order")
        plan = self.plans()[1]
        self.assertEqual(plan.priority, "priority:urgent")
        self.assertEqual(plan.reason, "Waiting for blockers #2")
        self.github.change(2, state="closed")
        self.assertEqual(self.plans()[1].state, "ready")

    def test_milestone_inheritance_without_priority_includes_transitive_unqueued_dependents(self):
        self.add(issue(1, ("ready",), iso(300)), issue(2, (), milestone=20),
                 issue(21, (), milestone=10), issue(4, ("ready",), iso(1), 20))
        self.github.milestones = [{"number": 20, "state": "open", "created_at": iso(2)},
                                  {"number": 10, "state": "open", "created_at": iso(1)}]
        self.github.dependencies = {21: [2], 2: [1]}
        self.loop = Loop(config(self.root, self.worker, queue=Queue(milestones="order")),
                         self.github, "operator", output=lambda *_: None)
        plan = self.plans()[1]
        self.assertEqual((plan.milestone, plan.milestone_source, plan.priority, plan.state),
                         (10, 21, None, "ready"))
        self.assertEqual([p.item.number for p in self.loop.plans()], [1, 4])
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        self.assertEqual(execute.call_args.args[0].item.number, 1)
        self.assertIsNotNone(self.loop.coordinator.claim(plan))
        self.github.change(21, state="closed")
        plan = self.plans()[1]
        self.assertEqual((plan.milestone, plan.milestone_source), (20, 2))
        self.github.change(21, state="open")
        self.github.change(2, state="closed")
        self.assertEqual((self.plans()[1].milestone, self.plans()[1].milestone_source), (None, None))

    def test_milestone_and_priority_inheritance_choose_their_own_sources(self):
        self.add(issue(1, ("ready", "priority:low"), milestone=20),
                 issue(21, ("priority:low",), milestone=10),
                 issue(22, ("priority:urgent",), milestone=20),
                 issue(23, ("priority:normal",), milestone=10))
        self.github.milestones = [{"number": 10, "state": "open", "created_at": iso(1)},
                                  {"number": 20, "state": "open", "created_at": iso(2)}]
        self.github.dependencies = {21: [1], 22: [1], 23: [1]}
        self.loop = self.make_loop(milestones="order")
        plan = self.plans()[1]
        self.assertEqual((plan.milestone, plan.milestone_source, plan.priority, plan.priority_source),
                         (10, 21, "priority:urgent", 22))
        self.github.change(1, milestone=10)
        self.assertEqual((self.plans()[1].milestone, self.plans()[1].milestone_source), (10, None))

    def test_milestone_cycles_share_earliest_reachable_and_ignore_external_and_closed_dependents(self):
        self.add(issue(1, milestone=20), issue(2, milestone=10),
                 issue(3), replace(issue(21, (), milestone=5), state="closed"))
        self.github.milestones = [{"number": n, "state": "open", "created_at": iso(n)}
                                  for n in (5, 10, 20)]
        self.github.dependencies = {1: [2], 2: [3], 3: [1], 21: [1]}
        self.loop = self.make_loop(milestones="order")
        self.assertEqual([(p.item.number, p.milestone, p.milestone_source, p.state)
                          for p in self.loop.plans()],
                         [(1, 10, 2, "parked"), (2, 10, None, "parked"), (3, 10, 2, "parked")])
        self.github.dependencies[2] = [Dependency("other/project", 3, "open")]
        self.assertEqual((self.plans()[3].milestone, self.plans()[3].milestone_source), (None, None))

    def test_dependency_ignore_disables_milestone_inheritance(self):
        self.add(issue(1), issue(21, (), milestone=10))
        self.github.milestones = [{"number": 10, "state": "open", "created_at": iso(1)}]
        self.github.dependencies[21] = [1]
        self.loop = self.make_loop(milestones="order", dependencies="ignore")
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("ignore must not read")):
            self.assertEqual((self.plans()[1].milestone, self.plans()[1].milestone_source), (None, None))

    def test_gate_and_ignore_keep_blockers_own_milestones(self):
        self.add(issue(1, ("ready", "priority:low"), milestone=20),
                 issue(21, ("priority:urgent",), milestone=10))
        self.github.milestones = [{"number": n, "state": "open", "created_at": iso(n)}
                                  for n in (10, 20)]
        self.github.dependencies[21] = [1]
        for mode in ("gate", "ignore"):
            for priority in (Priority(), self.priority):
                with self.subTest(mode=mode, priority=priority):
                    self.loop = Loop(config(self.root, self.worker, queue=Queue(mode, priority)),
                                     self.github, "operator", output=lambda *_: None)
                    plan = self.plans()[1]
                    self.assertEqual((plan.milestone, plan.milestone_source), (20, None))
                    self.assertEqual(plan.state, "parked" if mode == "gate" else "ready")

    def test_status_text_and_json_name_inherited_milestone(self):
        self.add(issue(1, ("ready", "priority:low")), issue(21, (), milestone=10))
        self.github.milestones = [{"number": 10, "state": "open", "created_at": iso(1)}]
        self.github.dependencies[21] = [1]
        self.loop = self.make_loop(milestones="order")
        rows = status_rows(self.loop)
        self.assertEqual((rows[0]["milestone"], rows[0]["milestone_inherited_from"]), (10, 21))
        with patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), rows)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status"]), 0)
            self.assertIn("priority priority:normal (inherited from #21) · "
                          "milestone #10 (inherited from #21)", output.getvalue())

    def test_pr_assignments_are_ungated_and_unrelated_issues_do_not_raise_priority(self):
        self.add(pr(2, ("needs-changes", "priority:low")),
                 issue(21, ("ready", "priority:urgent")))
        self.github.dependencies[2] = [21]
        plans = self.loop.plans()
        self.assertEqual([p.item.number for p in plans], [2, 21])
        self.assertEqual((plans[0].priority, plans[0].priority_source), ("priority:low", None))
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("PR claim is ungated")):
            self.assertIsNotNone(self.loop.coordinator.claim(plans[0]))

    def test_pr_inherits_effective_priority_of_issue_it_closes(self):
        self.add(issue(1, ("priority:low",)), issue(21, ("priority:urgent",)),
                 pr(2, ("needs-changes",), body="Closes #1"),
                 pr(3, ("needs-changes", "priority:high"), body="Unrelated #21"))
        self.github.dependencies[21] = [1]
        plans = self.loop.plans()
        self.assertEqual([p.item.number for p in plans], [2, 3])
        self.assertEqual((plans[0].priority, plans[0].priority_from_issue), ("priority:urgent", 1))
        self.assertEqual((plans[1].priority, plans[1].priority_from_issue), ("priority:high", None))

    def test_pr_inheritance_also_applies_in_dependency_ignore_mode(self):
        self.add(issue(1, ("priority:urgent",)), pr(2, ("needs-changes",), body="Fixes #1"))
        self.loop = self.make_loop(dependencies="ignore")
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("ignore must not read")):
            plan = self.loop.plans()[0]
            self.assertEqual((plan.priority, plan.priority_from_issue), ("priority:urgent", 1))

    def test_pr_closing_several_issues_takes_highest_and_own_priority_wins_ties(self):
        self.add(issue(1, ("priority:normal",)), issue(21, ("priority:high",)),
                 pr(2, ("needs-changes",), body="Closes #1, resolves #21"))
        self.assertEqual((self.plans()[2].priority, self.plans()[2].priority_from_issue),
                         ("priority:high", 21))
        for label in ("priority:high", "priority:urgent"):
            self.github.change(2, labels=frozenset({"needs-changes", label}))
            self.assertEqual((self.plans()[2].priority, self.plans()[2].priority_from_issue),
                             (label, None))

    def test_pr_inheritance_ignores_closed_and_foreign_issues_and_pr_references(self):
        self.add(replace(issue(1, ("priority:urgent",)), state="closed"),
                 issue(21, ("priority:urgent",)),
                 pr(2, ("needs-changes",), body="Closes #1; fixes other/project#21; resolves #3"),
                 pr(3, ("priority:urgent",)))
        self.assertEqual((self.plans()[2].priority, self.plans()[2].priority_from_issue),
                         ("priority:normal", None))

    def test_pr_status_names_closing_issue_in_text_and_json(self):
        self.add(issue(21, ("priority:urgent",)), pr(2, ("needs-changes",), body="Closes #21"))
        rows = status_rows(self.loop)
        self.assertEqual(rows[0]["priority_from_issue"], 21)
        with patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status"]), 0)
        self.assertIn("priority:urgent (from closed issue #21)", output.getvalue())

    def test_started_run_completes_when_a_new_blocker_appears(self):
        self.add(issue(1), issue(31, ()))
        plan = self.plans()[1]

        def execute(*args, **kwargs):
            self.github.dependencies[1] = [31]
            co = self.loop.coordinator
            lease = next(r for r in co.history(1) if r["kind"] == "lease")
            self.assertEqual(self.plans()[1].state, "owned")
            co.report(lease, "success", "Completed started work", outcome="done")
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execute):
            self.assertTrue(self.loop.execute(plan))
        history = self.loop.coordinator.history(1)
        self.assertEqual(history[0]["result"], "success")
        self.assertTrue(history[1]["accepted"])

    def test_started_run_recovers_with_open_blockers_without_reexecution(self):
        self.add(issue(1), issue(31, ()))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        lease = co.claim(self.plans()[1])
        co.update(lease, state="running", started=True)
        self.github.dependencies[1] = [31]
        co.report(lease, "success", "Started work completed", outcome="done")
        now += 61
        self.assertEqual(self.plans()[1].state, "recover")
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(self.loop.tick())
        self.assertTrue(co.history(1)[1]["accepted"])
        self.assertEqual(co.history(1)[2]["result"], "success")

    def test_failure_stops_selection_and_claim_visibly_without_writes(self):
        self.add(issue(1))
        plan = self.plans()[1]
        with patch.object(self.github, "blocked_by", side_effect=AgentError("Dependency read failed")):
            with self.assertRaisesRegex(AgentError, "Dependency read failed"):
                self.loop.tick()
            with self.assertRaisesRegex(AgentError, "Dependency read failed"):
                self.loop.coordinator.claim(plan)
            with patch("ub_agents.cli.Loop", return_value=self.loop), \
                    patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                    patch("ub_agents.cli.GitHub", return_value=self.github), \
                    redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["status"]), 1)
            self.assertIn("Dependency read failed", errors.getvalue())
        self.assertEqual(self.github.writes, [])

    def test_status_text_and_json_name_blockers_and_inherited_source(self):
        self.add(issue(1, ("ready", "priority:low")), issue(21, ("ready", "priority:urgent")),
                 issue(31, ()), issue(32, ()))
        self.github.dependencies = {21: [1], 1: [31, 32]}
        rows = status_rows(self.loop)
        self.assertEqual(rows[0]["priority"], "priority:urgent")
        self.assertEqual(rows[0]["priority_inherited_from"], 21)
        self.assertEqual(rows[0]["open_blockers"], ["#31", "#32"])
        with patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), rows)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status"]), 0)
            self.assertIn("priority:urgent (inherited from #21)", output.getvalue())
            self.assertIn("Waiting for blockers #31, #32", output.getvalue())

    def test_dependency_and_milestone_gates_both_apply_to_inherited_priority(self):
        self.add(issue(1, ("ready", "priority:low"), milestone=20),
                 issue(2, (), milestone=10), issue(21, ("priority:urgent",)))
        self.github.dependencies = {1: [2], 21: [1]}
        self.github.milestones = [{"number": 10, "state": "open", "created_at": iso(1)}]
        self.loop = self.make_loop(milestones="gate")
        plan = self.plans()[1]
        self.assertEqual(plan.priority, "priority:urgent")
        self.assertEqual(plan.reason, "Waiting for active milestone #10; Waiting for blockers #2")
        self.github.change(2, state="closed")
        self.assertEqual(self.plans()[1].state, "ready")
