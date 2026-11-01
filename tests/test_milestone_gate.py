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
from ub_agents.errors import AgentError, GitHubError
from ub_agents.github import Dependency
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import iso, timestamp
from ub_agents.run_planning import RunPlanning
from ub_agents.view_data import Session, work_pane
from tests.support import FakeGitHub, MemoryPublisher, PollGitHub, agent, config, issue, pr, stub_refresh


class MilestoneGateTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.priority = Priority(("high", "medium", "low"), "medium")
        self.github = FakeGitHub()
        self.github.milestones = [dict(number=20, title="v20", state="open", created_at=iso(200)),
                                  dict(number=10, title="v10", state="open", created_at=iso(100))]
        self.loop = self.make_loop()

    def make_loop(self, worker=None, **queue):
        return Loop(config(self.root, worker or self.worker,
                           queue=Queue("gate", queue.pop("priority", self.priority), **queue)),
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

    def test_cold_claim_reads_approval_only_through_the_selected_plan(self):
        self.github = PollGitHub()
        self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
        self.loop = self.make_loop()
        self.add(issue(1, milestone=10), issue(2, milestone=10),
                 *(issue(n, milestone=20) for n in range(3, 84)),
                 issue(84, ("ready", "needs-human"), milestone=10),
                 issue(85, milestone=10), issue(86, (), milestone=10),
                 issue(87, milestone=10), issue(88, milestone=10))
        self.github.dependencies[85] = [86]
        self.claim_other(87)
        lease = self.loop.coordinator.claim(self.loop.coordinator.plan(self.github.item(88), self.worker, ()))
        self.loop.coordinator.release(lease, "blocked", "Decision pending")
        self.github.reads.clear()
        self.github.timelines[1] = []
        with patch.object(self.loop, "park_approval") as parked:
            self.assertEqual(self.choice().item.number, 2)
        self.assertEqual([call.args[0].item.number for call in parked.call_args_list], [1])
        # One timeline and one edit-history read per reached, claimable item;
        # Later, stopped, dependency-blocked, owned and blocked-run work is unread.
        reads = [(name, args) for name, args in self.github.reads
                 if name in {"timeline", "issue_content"}]
        self.assertEqual(reads, [("issue_content", (1,)), ("timeline", (1,)),
                                 ("issue_content", (2,)), ("timeline", (2,))])
        self.assertEqual(len(reads), 4)

    def test_cold_claim_and_parking_reread_only_the_reached_items(self):
        self.github = PollGitHub(*(issue(n, milestone=10 if n <= 2 else 20) for n in range(1, 84)))
        self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
        self.github.timelines[1] = []
        self.loop = self.make_loop()

        def run(*args, **kwargs):
            self.loop.coordinator.report(self.loop.github.lease, "success", "Finished", outcome="done")
            return 0

        with patch("ub_agents.assignment_run.supervise", side_effect=run) as executed:
            self.assertTrue(self.loop.tick())
        executed.assert_called_once()
        # Discovery and fresh approval parking read #1; discovery, before-write
        # authorization and after-election authorization read the claimed #2.
        self.assertEqual([args[0] for name, args in self.github.reads if name == "timeline"], [1, 1, 2, 2, 2])
        self.assertEqual([args[0] for name, args in self.github.reads if name == "issue_content"], [1, 1, 2, 2, 2])
        self.assertIn("needs-human", self.github.items[1].labels)
        self.assertEqual(self.loop.coordinator.history(2)[0]["state"], "released")
        self.assertTrue(all(not self.github.store.get(n) for n in range(3, 84)))

    def test_later_pass_parks_an_initially_unreached_denial(self):
        self.add(issue(1, milestone=10), issue(2, milestone=10), issue(3, milestone=20))
        self.github.timelines[2] = []
        self.assertEqual(self.choice().item.number, 1)
        self.assertEqual(self.github.writes, [])
        self.github.change(1, labels=frozenset())
        self.assertEqual(self.choice().item.number, 3)
        self.assertIn("needs-human", self.github.items[2].labels)
        self.assertEqual(self.loop.coordinator.history(2), [])

    def test_unreadable_history_defers_approval_fallback_until_reached(self):
        for failed in (1, 2):
            with self.subTest(failed=failed):
                self.github = PollGitHub(issue(1, milestone=10), issue(2, milestone=20),
                                         issue(3, ("ready", "needs-human"), milestone=10))
                self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
                error = GitHubError("GET", "comments", "Unreadable comments")
                failures = [error if n in (failed, 3) else None for n in (1, 3, 2)]
                self.github.read_results["comments"] = list(failures)
                eager = self.make_loop()
                expected = next(p for p in eager.plans() if p.state == "ready")
                self.github.read_results["comments"] = list(failures)
                self.github.reads.clear()
                self.loop = self.make_loop()
                self.assertEqual(self.choice(), expected)
                self.assertEqual(self.loop.milestone_skips, eager.milestone_skips)
                reached = [1, 2] if failed == 1 else [1]
                self.assertEqual([args[0] for name, args in self.github.reads if name == "issue_content"], reached)

    def test_coordination_failure_with_readable_approval_still_stops_selection(self):
        self.add(issue(1, milestone=10))
        # An author-permission failure can affect coordination while the input
        # verdict remains readable; it must not become a claimable placeholder.
        with patch("ub_agents.coordination.Coordinator.history", side_effect=AgentError("History failed")), \
                patch.object(self.loop, "execute") as execute:
            with self.assertRaisesRegex(AgentError, "History failed"):
                self.loop.tick()
        execute.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_lazy_choice_and_milestone_skips_match_eager_approval(self):
        fixtures = [
            ("milestone order", [issue(1, ("ready", "low"), milestone=10),
                                 issue(2, ("ready", "high"), milestone=20)], ()),
            ("unlisted exception", [issue(1, ("ready", "medium"), milestone=10),
                                    issue(2, ("ready", "high"))], ()),
            ("unapproved milestone head", [issue(1, ("ready", "high"), milestone=10),
                                            issue(2, ("ready", "medium"), milestone=10),
                                            issue(3, ("ready", "high"))], (1,)),
            ("unlisted tie", [issue(1, ("ready", "medium"), milestone=10),
                              issue(2, ("ready", "medium"))], ()),
            ("unapproved unlisted head", [issue(1, ("ready", "medium"), milestone=10),
                                           issue(2, ("ready", "high")),
                                           issue(3, ("ready", "low"))], (2,)),
            ("existing PR", [issue(1, ("ready", "high"), milestone=10),
                             issue(2, ("ready", "low"), milestone=10),
                             pr(3, ("needs-changes", "medium"), body="Unrelated")], (1,)),
            ("existing PR tie", [issue(1, milestone=10),
                                 pr(2, body="Unrelated")], ()),
            ("unapproved PR", [issue(1, milestone=10),
                               pr(2, body="Unrelated")], (2,)),
            ("approval skip", [issue(1, milestone=10), issue(2, milestone=20)], (1,)),
            ("empty milestone skip", [issue(1, (), milestone=10),
                                      issue(2, ("ready", "needs-human"), milestone=10),
                                      issue(3, milestone=20)], ()),
            ("all assigned unapproved", [issue(1, milestone=10), issue(2, milestone=20),
                                         issue(3)], (1, 2)),
            ("none approved", [issue(1, milestone=10), issue(2, milestone=20),
                               issue(3)], (1, 2, 3)),
        ]
        for name, items, rejected in fixtures:
            with self.subTest(name=name):
                self.github = FakeGitHub(*items)
                self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
                self.github.timelines.update({n: [] for n in rejected})
                eager = self.make_loop()
                expected = next((p for p in eager.plans() if p.state == "ready"), None)
                self.loop = self.make_loop()
                with patch.object(self.loop, "execute", return_value=True) as execute, \
                        patch.object(self.loop, "park_approval"):
                    self.assertEqual(self.loop.tick(), expected is not None)
                selected = execute.call_args.args[0] if execute.called else None
                self.assertEqual(selected, expected)
                self.assertEqual(self.loop.milestone_skips, eager.milestone_skips)

    def test_unlisted_and_existing_work_need_the_approved_milestone_head_first(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                competitor = (pr(3, ("needs-changes", "medium"), body="Unrelated") if existing else
                              issue(3, ("ready", "medium")))
                self.github = PollGitHub(issue(1, ("ready", "high"), milestone=10),
                                         issue(2, ("ready", "low"), milestone=10), competitor,
                                         issue(4, milestone=20))
                self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
                self.github.timelines[1] = []
                self.loop = self.make_loop()
                with patch.object(self.loop, "park_approval"):
                    self.assertEqual(self.choice().item.number, 3)
                self.assertEqual([args[0] for name, args in self.github.reads if name == "timeline"], [1, 2, 3])

    def test_gate_claim_rechecks_approval_with_fresh_reads_before_its_first_write(self):
        self.github = PollGitHub(issue(1, milestone=10), issue(2, milestone=20))
        self.github.milestones = [dict(number=n, title=f"v{n:02}", state="open", created_at=iso(n)) for n in (10, 20)]
        self.loop = self.make_loop()
        plan = next(self.loop.iter_plans(claiming=True))
        self.github.roles["maintainer"] = "read"
        self.github.reads.clear()
        self.assertFalse(self.loop.execute(plan))
        self.assertIn(("timeline", (1,)), self.github.reads)
        self.assertIn(("issue_content", (1,)), self.github.reads)
        self.assertIn(("role", ("maintainer",)), self.github.reads)
        self.assertEqual(self.github.writes, [])

    def test_status_and_observation_show_unreached_approval_parking(self):
        self.add(issue(1, milestone=10), issue(2, milestone=10), issue(3, milestone=20))
        self.github.timelines[2] = self.github.timelines[3] = []
        self.assertEqual(self.choice().item.number, 1)
        rows = {row["number"]: row for row in status_rows(self.loop)}
        self.assertEqual(set(rows), {1, 2, 3})
        for n in (2, 3):
            self.assertEqual(rows[n]["state"], "parked")
            self.assertIn("No maintainer", rows[n]["reason"])
        memory = MemoryPublisher()
        worker = RunPlanning(self.loop, 0)
        worker.planner.observer = Observations(self.loop.config, "operator", None, memory)
        worker.planner.observer.begin_pass()
        plans = list(worker.planner.iter_plans())
        worker.planner.observer.complete_pass()
        self.assertEqual({p.item.number: p.state for p in plans}, {1: "ready", 2: "parked", 3: "parked"})
        pane = work_pane(Session(self.root / "session.json", memory.snapshots[-1]), self.root)
        attention = next(section for section in pane.sections if section.name == "Needs attention")
        self.assertEqual({row.item for row in attention.rows}, {2, 3})
        self.assertEqual(self.github.writes, [])

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

    def test_milestones_use_title_order_over_creation_and_number_and_skip_empty_ones(self):
        self.github.milestones[0]["title"] = "v0.4.6"
        self.github.milestones[1]["title"] = "v0.5.0"
        self.github.milestones.append(dict(number=5, title="v0.1.0", state="open", created_at=iso(1)))
        self.add(issue(1, ("ready", "high"), iso(1), 20),
                 issue(2, ("ready", "low"), iso(200), 10))
        self.assertEqual(self.ready(), [1, 2])
        self.assertEqual(self.choice().item.number, 1)

    def test_closed_milestone_uses_unmilestoned_exception(self):
        self.github.milestones.append(dict(number=30, title="v30", state="closed", created_at=iso(1)))
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

    def test_untriggered_early_milestone_allows_later_work_and_explains_skip(self):
        self.add(issue(1, (), milestone=10), issue(2, (), milestone=10), issue(3, milestone=20))
        expected = "Milestone #10 has no eligible issues for this launcher"
        self.assertEqual(self.ready(), [3])
        self.assertEqual(self.loop.milestone_skips, (expected,))
        lines = []
        self.loop.output = lines.append
        memory = MemoryPublisher()
        self.loop.observer = Observations(self.loop.config, "operator", None, memory)
        self.assertEqual(self.choice().item.number, 3)
        self.assertNotIn(expected, lines)
        self.assertEqual(self.loop.milestone_skips, (expected,))
        for snapshot in memory.snapshots:
            self.assertNotIn("milestone_skips", snapshot)
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status"]), 0)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("Waiting for active milestone", output.getvalue())
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["milestone_skips"], [expected])
        self.github.change(1, labels=frozenset({"ready"}))
        self.github.timelines[1] = [dict(event="labeled", actor={"login": "maintainer"},
                                         label={"name": "ready"}, created_at=iso(timestamp()))]
        self.assertEqual(self.choice().item.number, 1)
        self.assertEqual(self.loop.milestone_skips, ())
        self.assertNotIn("milestone_skips", memory.snapshots[-1])

    def test_only_untriggered_work_reports_all_skipped_milestones(self):
        self.add(issue(1, (), milestone=10), issue(2, (), milestone=20))
        self.assertEqual(self.ready(), [])
        self.assertEqual(self.loop.milestone_skips,
                         tuple(f"Milestone #{n} has no eligible issues for this launcher" for n in (10, 20)))

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
