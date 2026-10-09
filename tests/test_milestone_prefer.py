from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main, status_rows
from ub_agents.config import Priority, Queue, Runtime
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError
from ub_agents.github import Dependency
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import iso, timestamp
from ub_agents.view_data import Session, work_pane
from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue, pr, stub_refresh


class MilestonePreferTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.priority = Priority(("high", "medium", "low"), "medium")
        self.github = FakeGitHub()
        self.github.milestones = [dict(number=20, state="open", created_at=iso(200)),
                                  dict(number=10, state="open", created_at=iso(100))]
        self.loop = self.make_loop()

    def make_loop(self, worker=None, **queue):
        return Loop(config(self.root, worker or self.worker,
                           queue=Queue("prefer", queue.pop("priority", self.priority), **queue)),
                    self.github, "operator", output=lambda *_: None)

    def add(self, *items):
        self.github.items.update({item.number: item for item in items})

    def ready(self):
        return [p.item.number for p in self.loop.plans() if p.state == "ready"]

    def choice(self):
        with patch.object(self.loop, "execute", return_value=True) as execute:
            self.assertTrue(self.loop.tick())
        return execute.call_args.args[0]

    def claim_other(self, number):
        self.github.roles["other-launcher"] = "write"
        other = Coordinator(self.github, "other-launcher", queue=self.loop.config.queue)
        with patch.object(self.github, "login", "other-launcher"):
            lease = other.claim(other.plan(self.github.item(number), self.worker, ()))
        self.assertIsNotNone(lease)
        return other, lease

    def test_milestone_first_with_strict_priority_exception_and_ten_medium_issues(self):
        self.add(*(issue(n, ("ready", "medium"), iso(n), 10) for n in range(1, 11)),
                 issue(21, ("ready", "high"), iso(1)),
                 issue(22, ("ready", "medium"), iso(1)),
                 issue(23, ("ready", "low"), iso(1)),
                 issue(24, ("ready", "high"), iso(1), 20))
        self.assertEqual(self.ready(), [21, *range(1, 11), 24, 22, 23])
        self.assertEqual(self.choice().item.number, 21)
        self.github.change(21, labels=frozenset())
        self.assertEqual(self.choice().item.number, 1)

    def test_exception_compares_with_next_eligible_issue_and_is_repeated(self):
        self.add(issue(1, ("ready", "high", "needs-human"), iso(1), 10),
                 issue(2, ("ready", "medium"), iso(2), 10),
                 issue(3, ("ready", "low"), iso(3), 10),
                 issue(4, ("ready", "high"), iso(1)),
                 issue(5, ("ready", "medium"), iso(1)),
                 issue(6, ("ready", "high"), iso(1), 20))
        self.assertEqual(self.ready(), [4, 2, 5, 3, 6])

    def test_within_milestone_uses_priority_age_number_and_yaml_agent_order(self):
        preparer = agent(self.root, name="preparer", triggers=("prepare",))
        self.loop = Loop(config(self.root, preparer, self.worker, queue=self.loop.config.queue),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("ready", "prepare", "low"), iso(1), 10),
                 issue(2, ("ready", "prepare", "high"), iso(300), 10),
                 issue(4, ("ready", "prepare", "high"), iso(100), 10),
                 issue(3, ("ready", "prepare", "high"), iso(100), 10))
        self.assertEqual([(p.item.number, p.agent.name) for p in self.loop.plans()],
                         [(n, name) for n in (3, 4, 2, 1) for name in ("preparer", "worker")])

    def test_milestones_use_creation_time_then_number_and_skip_empty_ones(self):
        self.github.milestones[0]["created_at"] = iso(100)
        self.github.milestones.append(dict(number=5, state="open", created_at=iso(1)))
        self.add(issue(1, ("ready", "high"), iso(1), 20),
                 issue(2, ("ready", "low"), iso(200), 10))
        self.assertEqual(self.ready(), [2, 1])

    def test_closed_milestone_uses_unmilestoned_exception(self):
        self.github.milestones.append(dict(number=30, state="closed", created_at=iso(1)))
        self.add(issue(1, ("ready", "high"), iso(1), 30),
                 issue(2, ("ready", "medium"), iso(2), 30),
                 issue(3, ("ready", "medium"), iso(300), 10))
        self.assertEqual(self.ready(), [1, 3, 2])

    def test_two_launchers_fall_back_without_touching_the_early_owner(self):
        self.add(issue(1, milestone=10), issue(2, milestone=10), issue(3, milestone=20))
        other, first = self.claim_other(1)
        _, second = self.claim_other(2)
        before = list(self.github.writes)
        plan = self.choice()
        self.assertEqual(plan.item.number, 3)
        self.assertEqual(self.github.writes, before)
        self.assertIsNotNone(self.loop.coordinator.claim(plan))
        self.assertEqual(other.history(1), [first])
        self.assertEqual(other.history(2), [second])

    def test_dependency_blocked_early_work_and_release_prep_do_not_gate_independent_later_work(self):
        self.add(issue(1, milestone=10), issue(2, (), milestone=10),
                 issue(3, milestone=20), issue(4, milestone=20),
                 issue(5, milestone=10))  # release prep
        self.github.dependencies = {1: [2], 4: [1], 5: [1, 2]}
        self.assertEqual(self.ready(), [3])
        self.assertEqual(self.choice().item.number, 3)
        plans = {p.item.number: p for p in self.loop.plans()}
        self.assertEqual(plans[1].reason, "Waiting for blockers #2")
        self.assertEqual(plans[4].reason, "Waiting for blockers #1")
        self.assertEqual(plans[5].reason, "Waiting for blockers #1, #2")

    def test_next_pass_reconsiders_earlier_work_when_its_blocker_closes(self):
        self.add(issue(1, ("ready", "low"), milestone=10), issue(2, (), milestone=10),
                 issue(3, ("ready", "high"), milestone=20))
        self.github.dependencies = {1: [2]}
        self.assertEqual(self.choice().item.number, 3)
        self.github.change(2, state="closed")
        self.assertEqual(self.choice().item.number, 1)
        self.assertEqual(self.github.writes, [])

    def test_next_pass_reconsiders_earlier_work_when_its_owner_releases_it(self):
        self.add(issue(1, ("ready", "low"), milestone=10),
                 issue(2, ("ready", "high"), milestone=20))
        other, lease = self.claim_other(1)
        self.assertEqual(self.choice().item.number, 2)
        other.release(lease, "interrupted", "Stopped before starting")
        self.assertEqual(self.choice().item.number, 1)

    def test_terminal_case_with_open_but_unavailable_milestones_uses_priority(self):
        self.add(issue(1, ("ready", "needs-human"), milestone=10), issue(2, (), milestone=20),
                 issue(3, ("ready", "low"), iso(1)), issue(4, ("ready", "high"), iso(300)),
                 issue(5, ("ready", "high"), iso(200)))
        self.assertEqual(self.ready(), [5, 4, 3])
        self.assertEqual(self.choice().item.number, 5)

    def test_approval_parked_early_work_does_not_gate_later_work(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20))
        self.github.timelines[1] = []
        plans = {p.item.number: p for p in self.loop.plans()}
        self.assertEqual(plans[1].state, "parked")
        self.assertIn("No maintainer", plans[1].reason)
        self.assertEqual(self.ready(), [2])
        self.assertEqual(self.choice().item.number, 2)

    def test_approval_parking_does_not_recheck_milestones(self):
        self.add(issue(1, milestone=20), issue(2, (), milestone=10))
        self.github.timelines[1] = []
        park = self.loop.park_approval

        def without_milestone_read(plan):
            with patch.object(self.github, "milestone_order", side_effect=AssertionError("no parking reread")), \
                    patch.object(self.github, "active_milestone", side_effect=AssertionError("no parking gate")):
                park(plan)

        with patch.object(self.loop, "park_approval", side_effect=without_milestone_read) as parked:
            self.assertFalse(self.loop.tick())
        parked.assert_called_once()
        self.assertTrue(self.github.writes)

    def test_external_dependency_in_early_milestone_allows_independent_fallback(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20))
        self.github.dependencies[1] = [Dependency("other/project", 31, "open")]
        self.assertEqual(self.ready(), [2])
        self.assertEqual(self.choice().item.number, 2)
        self.assertEqual(self.loop.plans()[0].reason, "Waiting for blockers other/project#31")

    def test_missing_runtime_in_early_milestone_does_not_gate_later_work(self):
        runtime_worker = replace(self.worker, name="runtime-worker", command=(),
                                 runtimes=(Runtime("codex", "model", "high"),), triggers=("runtime",))
        self.loop = Loop(config(self.root, runtime_worker, self.worker, queue=self.loop.config.queue),
                         self.github, "operator", output=lambda *_: None)
        self.add(issue(1, ("runtime",), milestone=10), issue(2, milestone=20))
        with patch.object(self.loop.maintenance, "available", side_effect=lambda cli: cli != "codex"):
            self.assertEqual(self.ready(), [2])
            self.assertEqual(self.choice().item.number, 2)

    def test_backoff_attempt_limit_and_blocked_run_in_early_milestone_allow_fallback(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        lease = co.claim(co.plan(self.github.item(1), self.worker, ()))
        co.update(lease, state="running", started=True)
        co.release(lease, "retry", "Retry later", 60)
        self.assertEqual(self.ready(), [2])
        self.assertEqual(self.choice().item.number, 2)
        now += 61
        self.loop = self.make_loop(replace(self.worker, max_attempts=1))
        self.loop.coordinator.clock = lambda: now
        self.assertEqual(self.ready(), [2])
        self.assertIn("Attempt limit", self.loop.plans()[0].reason)
        self.assertEqual(self.choice().item.number, 2)
        co.update(lease, result="blocked", summary="Decision pending")
        self.loop = self.make_loop()
        self.loop.coordinator.clock = lambda: now
        self.assertEqual(self.ready(), [2])
        self.assertIn("Last run blocked", self.loop.plans()[0].reason)

    def test_blockers_inherit_earliest_milestone_transitively_without_priority_labels(self):
        self.loop = self.make_loop(priority=Priority())
        self.add(issue(1, ("ready",), iso(300)), issue(2, (), milestone=20),
                 issue(3, (), milestone=10), issue(4, ("ready",), iso(1), 20))
        self.github.dependencies = {3: [2], 2: [1]}
        self.assertEqual(self.ready(), [1, 4])
        plan = self.choice()
        self.assertEqual((plan.milestone, plan.milestone_source, plan.priority), (10, 3, None))
        self.assertIsNotNone(self.loop.coordinator.claim(plan))

    def test_priority_and_milestone_inheritance_choose_independent_sources(self):
        self.add(issue(1, ("ready", "low"), milestone=20),
                 issue(2, ("medium",), milestone=10), issue(3, ("high",), milestone=20),
                 issue(4, ("ready", "high"), milestone=20))
        self.github.dependencies = {2: [1], 3: [1]}
        plan = self.choice()
        self.assertEqual((plan.item.number, plan.milestone, plan.milestone_source,
                          plan.priority, plan.priority_source), (1, 10, 2, "high", 3))

    def test_inherited_priority_applies_to_unmilestoned_exception(self):
        self.add(issue(1, ("ready", "low")), issue(2, ("high",)),
                 issue(3, ("ready", "medium"), milestone=10))
        self.github.dependencies = {2: [1]}
        self.assertEqual(self.ready(), [1, 3])
        self.assertEqual(self.choice().priority_source, 2)

    def test_dependencies_ignore_skips_links_and_inheritance(self):
        self.loop = self.make_loop(dependencies="ignore")
        self.add(issue(1, ("ready", "low"), milestone=20),
                 issue(2, (), milestone=10), issue(3, ("ready", "medium"), milestone=10))
        self.github.dependencies = {2: [1], 3: [1]}
        with patch.object(self.github, "blocked_by", side_effect=AssertionError("ignore must not read")):
            self.assertEqual(self.ready(), [3, 1])
            self.assertIsNotNone(self.loop.coordinator.claim(self.choice()))

    def test_existing_prs_use_priority_and_win_ties_against_selected_new_issue(self):
        self.add(issue(1, ("ready", "medium"), iso(1), 10),
                 issue(2, ("ready", "high"), iso(2), 20),
                 pr(3, ("needs-changes", "low"), body="Unrelated", milestone=10),
                 pr(4, ("needs-changes", "medium"), body="Unrelated", milestone=20),
                 pr(5, ("needs-changes", "high"), body="Closes #1"))
        self.assertEqual(self.ready(), [5, 4, 1, 2, 3])
        self.assertEqual(self.choice().item.number, 5)

    def test_owned_and_recovery_use_priority_instead_of_milestones(self):
        self.add(issue(1, ("ready", "high"), milestone=20),
                 issue(2, ("ready", "low"), milestone=10),
                 issue(3, ("ready", "medium"), milestone=10))
        co = self.loop.coordinator
        now = timestamp()
        co.clock = lambda: now
        for number in (1, 2):
            lease = co.claim(co.plan(self.github.item(number), self.worker, ()))
            co.update(lease, state="running", started=True)
            co.report(lease, "success", "Completed", outcome="done")
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(1, "owned"), (3, "ready"), (2, "owned")])
        now += 61
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(1, "recover"), (3, "ready"), (2, "recover")])
        self.github.change(3, labels=frozenset({"ready", "low"}))
        self.assertEqual([(p.item.number, p.state) for p in self.loop.plans()],
                         [(1, "recover"), (2, "recover"), (3, "ready")])

    def test_unreadable_milestones_dependencies_and_item_stop_selection(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20), pr(3, body="Unrelated"))
        for method in ("milestone_order", "blocked_by", "item"):
            with self.subTest(method=method), \
                    patch.object(self.github, method, side_effect=AgentError("Unreadable data")), \
                    patch.object(self.loop, "execute") as execute:
                with self.assertRaisesRegex(AgentError, "Unreadable data"):
                    self.loop.tick()
                execute.assert_not_called()
                with self.assertRaisesRegex(AgentError, "Unreadable data"):
                    self.loop.plans()
        self.assertEqual(self.github.writes, [])

    def test_unreadable_unqueued_dependency_cannot_be_treated_as_no_earlier_work(self):
        self.add(issue(1, (), milestone=10), issue(2, milestone=20))
        with patch.object(self.github, "blocked_by", side_effect=AgentError("Unreadable link")), \
                patch.object(self.loop, "execute") as execute:
            with self.assertRaisesRegex(AgentError, "Unreadable link"):
                self.loop.tick()
            execute.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_claim_freshly_rechecks_blockers_but_does_not_recheck_milestones(self):
        self.add(issue(1, milestone=10), issue(2, (), milestone=20))
        plan = self.choice()
        self.github.change(1, milestone=20)
        with patch.object(self.github, "milestone_order", side_effect=AssertionError("no milestone reread")), \
                patch.object(self.github, "active_milestone", side_effect=AssertionError("no milestone gate")):
            self.github.dependencies[1] = [2]
            self.assertIsNone(self.loop.coordinator.claim(plan))
            self.assertEqual(self.github.writes, [])
            self.github.dependencies[1] = [Dependency("org/project", 2, "closed")]
            self.assertIsNotNone(self.loop.coordinator.claim(plan))

    def test_lost_claim_election_after_selection_moves_to_next_issue(self):
        self.add(issue(1, milestone=10), issue(2, milestone=20))
        self.github.roles["other-launcher"] = "write"
        other = Coordinator(self.github, "other-launcher", queue=self.loop.config.queue)
        create = self.github.create_comment
        competed = False

        def race(number, text):
            nonlocal competed
            if number == 1 and not competed:
                competed = True
                with patch.object(self.github, "login", "other-launcher"):
                    winner = other.claim(other.plan(self.github.item(1), self.worker, ()))
                self.assertIsNotNone(winner)
            return create(number, text)

        claimed = []
        def execute(plan):
            lease = self.loop.coordinator.claim(plan)
            if lease is not None:
                claimed.append(plan.item.number)
            return lease is not None

        with patch.object(self.github, "create_comment", side_effect=race), \
                patch.object(self.loop, "execute", side_effect=execute):
            self.assertTrue(self.loop.tick())
        self.assertEqual(claimed, [2])
        self.assertEqual([r["state"] for r in other.history(1)], ["claiming", "withdrawn"])
        self.assertEqual(other.history(1)[0]["actor"], "other-launcher")
        self.assertEqual(len(self.loop.coordinator.history(2)), 1)

    def test_status_and_work_pane_share_selection_order_and_inherited_milestone(self):
        self.add(issue(1, ("ready", "low"), iso(300)), issue(2, ("medium",), milestone=10),
                 issue(3, ("ready", "high"), iso(1), 20),
                 issue(4, ("ready", "high"), iso(1)), issue(5, ("ready", "medium"), iso(1)))
        self.github.dependencies = {2: [1]}
        expected = [4, 1, 3, 5]
        rows = status_rows(self.loop)
        self.assertEqual([r["number"] for r in rows], expected)
        self.assertEqual((rows[1]["milestone"], rows[1]["milestone_inherited_from"]), (10, 2))
        memory = MemoryPublisher()
        observer = Observations(self.loop.config, "operator", None, memory)
        self.loop.observer = observer
        observer.begin_pass()
        self.assertEqual([p.item.number for p in self.loop.iter_plans()], expected)
        observer.complete_pass()
        pane = work_pane(Session(self.root / "session.json", memory.snapshots[-1]), self.root)
        eligible = next(section for section in pane.sections if section.name == "Eligible")
        self.assertEqual([r.item for r in eligible.rows], expected)
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["assignments"], rows)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status"]), 0)
            text = output.getvalue()
        for before, after in zip(expected, expected[1:]):
            self.assertLess(text.index(f"#{before} worker"), text.index(f"#{after} worker"))
        self.assertIn("milestone #10 (inherited from #2)", text)
        self.assertIn("milestone none", text)
        self.assertNotIn("Waiting for active milestone", text)

    def test_named_item_uses_its_own_priority_and_milestone_without_order_reads(self):
        self.add(issue(1, ("ready", "low"), milestone=20),
                 issue(2, ("high",), milestone=10))
        self.github.dependencies = {2: [1]}
        with patch.object(self.github, "milestone_order", side_effect=AssertionError("named item must not rank")):
            item, plans = self.loop.item_plans(1)
            self.assertEqual(item.milestone, 20)
            self.assertEqual(list(plans)[0].state, "ready")
            with patch.object(self.loop, "execute", return_value=True) as execute:
                self.assertTrue(self.loop.tick_item(1))
            self.assertEqual(execute.call_args.args[0].item.labels, frozenset({"ready", "low"}))
