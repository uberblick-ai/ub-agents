from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Priority, Queue
from ub_agents.discovery import Discovery
from ub_agents.errors import GitHubError
from ub_agents.loop import COMMENT_RECOVERY_SECONDS, Loop
from tests.support import PollGitHub, agent, config, issue, pr, stub_refresh


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def loop(self, items, queue=Queue()):
        github = PollGitHub(*items)
        return Loop(config(self.root, agent(self.root), queue=queue), github, "operator", output=lambda _: None)

    def item_reads(self, github):
        return {args[0] for name, args in github.reads if name in Discovery.ITEM_READS}

    def test_cold_claim_cost_is_independent_of_issue_and_pr_queue_size(self):
        for kind in ("issue", "pr"):
            for priority in (Priority(), Priority(("urgent", "low"), "low")):
                counts = []
                for size in (1, 30, 101):
                    with self.subTest(kind=kind, priority=priority, size=size):
                        items = [issue(n) if kind == "issue" else pr(n, body="")
                                 for n in range(1, size + 1)]
                        loop = self.loop(items, Queue(priority=priority))
                        with patch.object(loop, "execute", side_effect=lambda plan:
                                          loop.coordinator.claim(plan, loop.config.stop_labels) is not None):
                            self.assertTrue(loop.tick())
                        counts.append(len(loop.github.reads))
                        self.assertEqual(self.item_reads(loop.github), {1})
                        self.assertEqual(len(loop.github.store), 1)
                # List reads in this fake each represent one paginated operation.
                self.assertEqual(counts, [counts[0]] * len(counts))

    def test_unchanged_and_single_changed_inputs(self):
        for items in ([issue(1), issue(2), issue(3)], [pr(1, body=""), pr(2, body=""), pr(3, body="")]):
            for queue in (Queue(), Queue(priority=Priority(("urgent", "low"), "low"))):
                with self.subTest(kind=items[0].kind, queue=queue):
                    loop = self.loop(items, queue)
                    list(loop.iter_plans())
                    loop.github.reads.clear()
                    list(loop.iter_plans())
                    self.assertEqual(loop.github.reads, [("observe", ()), ("repository_comments", (60 + COMMENT_RECOVERY_SECONDS,))])
                    for changes in ({"labels": items[1].labels | {"extra"}},
                                    {"updated_at": "2026-01-03T00:00:00Z"}):
                        loop.github.change(2, **changes)
                        loop.github.reads.clear()
                        list(loop.iter_plans())
                        self.assertEqual(self.item_reads(loop.github), {2})
                    # The repository comment scan invalidates one item even if
                    # the list timestamp does not advance for a comment edit.
                    loop.github.create_comment(2, "Maintainer feedback")
                    for edit in (False, True):
                        if edit:
                            loop.github.update_comment(loop.github.store[2][0]["id"], "Edited feedback")
                        loop.github.reads.clear()
                        list(loop.iter_plans())
                        self.assertEqual(self.item_reads(loop.github), {2})

    def test_lower_ranked_approval_gate_waits_until_reached(self):
        loop = self.loop([issue(1), issue(2)])
        loop.github.timelines[2] = []
        with patch.object(loop, "execute", return_value=True):
            self.assertTrue(loop.tick())
        self.assertEqual(self.item_reads(loop.github), {1})
        self.assertEqual(loop.github.writes, [])
        loop.github.change(1, labels=frozenset())
        self.assertFalse(loop.tick())
        self.assertIn("needs-human", loop.github.items[2].labels)
        self.assertTrue(any(name == "timeline" and args == (2,) for name, args in loop.github.reads))

    def test_later_agent_on_same_item_is_not_evaluated_after_claim(self):
        github = PollGitHub(issue())
        workers = (agent(self.root, name="first"), agent(self.root, name="second"))
        loop = Loop(config(self.root, *workers), github, "operator", output=lambda _: None)
        from ub_agents.coordination import Coordinator
        visited = []
        choose = Coordinator.choose_runtime
        def select(coordinator, item, worker, history):
            visited.append(worker.name)
            return choose(coordinator, item, worker, history)
        with patch.object(Coordinator, "choose_runtime", select), \
                patch.object(loop, "execute", return_value=True):
            self.assertTrue(loop.tick())
        self.assertEqual(visited, ["first"])

    def test_external_blocker_summary_change_invalidates_dependency_links(self):
        from ub_agents.github import Dependency
        loop = self.loop([replace(issue(1), total_blocked_by=1, open_blocked_by=1)])
        loop.github.dependencies[1] = [Dependency("other/project", 31, "open")]
        self.assertEqual(next(loop.iter_plans()).state, "parked")
        loop.github.dependencies[1] = [Dependency("other/project", 31, "closed")]
        loop.github.change(1, open_blocked_by=0)
        loop.github.reads.clear()
        self.assertEqual(next(loop.iter_plans()).state, "ready")
        self.assertIn(("blocked_by", (1,)), loop.github.reads)

    def test_status_reads_every_row_and_does_not_use_discovery_cache(self):
        loop = self.loop([issue(1), pr(2)])
        with patch.object(loop, "execute", return_value=True):
            loop.tick()
        loop.github.reads.clear()
        self.assertEqual(len(loop.plans()), 2)
        self.assertEqual(self.item_reads(loop.github), {1, 2})
        self.assertEqual(loop.github.writes, [])

    def test_cached_approval_never_authorizes_a_claim(self):
        loop = self.loop([issue(1)])
        plan = next(loop.iter_plans())
        self.assertEqual(plan.state, "ready")
        # Change only permission; there is no list/comment invalidation hint.
        loop.github.roles["maintainer"] = "read"
        loop.github.reads.clear()
        self.assertFalse(loop.execute(plan))
        self.assertTrue(any(name == "role" and args == ("maintainer",) for name, args in loop.github.reads))
        self.assertEqual(loop.github.writes, [])
        # Fresh denial also refreshes discovery so the next reached poll can
        # park the approval gate even without a timestamp/comment hint.
        self.assertFalse(loop.tick())
        self.assertIn("needs-human", loop.github.items[1].labels)
        self.assertEqual(loop.coordinator.history(1), [])

    def test_cached_approval_never_authorizes_parking(self):
        loop = self.loop([issue(1)])
        loop.github.timelines[1] = []
        plan = next(loop.iter_plans())
        self.assertIsNotNone(plan.approval_gate)
        # A fresh maintainer start arrives after discovery.
        loop.github.timelines[1] = [{"event": "labeled", "actor": {"login": "maintainer"},
                                    "label": {"name": "ready"}, "created_at": issue().created_at}]
        loop.park_approval(plan)
        self.assertEqual(loop.github.writes, [])

    def test_time_dependent_lease_is_replanned_with_cached_history(self):
        loop = self.loop([issue(1)])
        now = loop.coordinator.clock()
        loop.coordinator.clock = lambda: now
        loop.coordinator.claim(loop.plans()[0])
        self.assertEqual(next(loop.iter_plans()).state, "owned")
        loop.github.reads.clear()
        now += 61
        self.assertEqual(next(loop.iter_plans()).state, "ready")
        self.assertNotIn(("comments", (1,)), loop.github.reads)
        loop.github.reads.clear()
        self.assertEqual(next(loop.iter_plans()).state, "ready")
        self.assertEqual(loop.github.reads, [("observe", ()), ("repository_comments", (60 + COMMENT_RECOVERY_SECONDS,))])

    def test_changed_blocker_state_updates_inheritance_without_rereading_dependents(self):
        loop = self.loop([issue(1, ("ready", "low")), issue(2, ("urgent",))],
                         Queue(priority=Priority(("urgent", "low"))))
        loop.github.dependencies[2] = [1]
        self.assertEqual(next(loop.iter_plans()).priority_source, 2)
        loop.github.change(2, state="closed")
        loop.github.reads.clear()
        self.assertIsNone(next(loop.iter_plans()).priority_source)
        self.assertEqual(loop.github.reads, [("observe", ()), ("repository_comments", (60 + COMMENT_RECOVERY_SECONDS,))])

    def test_dependency_links_are_fresh_at_claim_even_after_cached_zero(self):
        loop = self.loop([replace(issue(1), total_blocked_by=0), issue(2, ())])
        plan = next(loop.iter_plans())
        loop.github.dependencies[1] = [2]
        self.assertIsNone(loop.coordinator.claim(plan))
        self.assertEqual(loop.github.writes, [])

    def test_failed_read_is_retried_on_next_unchanged_poll(self):
        loop = self.loop([issue(1)])
        from ub_agents.errors import GitHubError
        loop.github.read_results["issue_content"] = [GitHubError("POST", "graphql", "failed")]
        self.assertEqual(next(loop.iter_plans()).state, "parked")
        loop.github.reads.clear()
        self.assertEqual(next(loop.iter_plans()).state, "ready")
        self.assertIn(("issue_content", (1,)), loop.github.reads)

    def test_rate_limits_propagate_from_fresh_and_cached_planning(self):
        cases = [("comments", "issue"), ("timeline", "issue"), ("role", "issue"),
                 ("issue_content", "issue"), ("blocked_by", "issue"),
                 ("item", "pr"), ("pr_content", "pr"), ("reviews", "pr"),
                 ("review_comments", "pr")]
        for name, kind in cases:
            for cached in (False, True):
                with self.subTest(read=name, kind=kind, cached=cached):
                    loop = self.loop([issue(1) if kind == "issue" else pr(1, body="")])
                    error = GitHubError("GET", name, "rate limited", rate_limited=True)
                    loop.github.read_results[name] = [error]
                    # A failed history read must propagate even if approval
                    # would otherwise park the item for a missing start.
                    if name == "comments":
                        loop.github.timelines[1] = []
                    with self.assertRaises(GitHubError) as raised:
                        list(loop.iter_plans(cached=cached))
                    self.assertIs(raised.exception, error)
                    self.assertEqual(loop.github.writes, [])
                    # The failed read must not stick in an unchanged cache.
                    loop.github.reads.clear()
                    list(loop.iter_plans(cached=cached))
                    self.assertTrue(any(read == name for read, _ in loop.github.reads))

    def test_rejected_claim_invalidates_item_without_list_change(self):
        loop = self.loop([pr(1, body=""), pr(2, body="")])
        plans = list(loop.iter_plans())
        # An updated head is visible only in the detailed read. The list
        # snapshot deliberately retains the same timestamp and body.
        loop.github.change(1, head="b" * 40)
        self.assertFalse(loop.execute(plans[0]))
        loop.github.reads.clear()
        fresh = list(loop.iter_plans())
        self.assertEqual(fresh[0].item.head, "b" * 40)
        self.assertEqual(self.item_reads(loop.github), {1})
        self.assertEqual(loop.github.writes, [])
