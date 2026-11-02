from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.queue_planner import QueuePlanner
from ub_agents.run_planning import ObservationReads, RunPlanning
from tests.support import PollGitHub, agent, config, issue


class TraceEvents:
    def __init__(self, trace):
        self.trace = trace

    def __getattr__(self, name):
        return lambda *args: self.trace.append(("event", name, args))


class ObservationPlannerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = agent(self.root, name="first")
        self.second = agent(self.root, name="second")
        self.config = config(self.root, self.first, self.second)
        self.github = PollGitHub(issue(1), issue(2))
        self.lines = []
        self.loop = Loop(self.config, self.github, "operator", output=self.lines.append)
        self.loop.coordinator.clock = lambda: 1000

    def test_worker_constructs_queue_planner_without_constructing_another_loop(self):
        with patch.object(Loop, "__init__", side_effect=AssertionError("Second launcher")):
            worker = RunPlanning(self.loop, 1000)
        self.assertIsInstance(worker.planner.queue_planner, QueuePlanner)
        self.assertEqual([(plan.item.number, plan.agent.name) for plan in worker.planner.iter_plans()],
                         [(1, "first"), (1, "second"), (2, "first"), (2, "second")])
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.lines, [])

    def test_worker_keeps_start_context_and_shares_runtime_helpers(self):
        self.loop._launch_agent = "first"
        self.loop._launch_number = 2
        worker = RunPlanning(self.loop, 1000)
        planner = worker.planner
        self.assertIs(planner.config, self.config)
        self.assertIsNot(planner.coordinator, self.loop.coordinator)
        self.assertIs(planner.coordinator.clock, self.loop.coordinator.clock)
        for name in ("usage", "health", "maintenance", "discovery_budget"):
            self.assertIs(getattr(planner, name), getattr(self.loop, name))
        self.assertIsNot(planner.pass_messages, self.loop.pass_messages)
        self.assertIsNot(planner.pass_output, self.loop.pass_output)
        self.loop.config = replace(self.config, agents=(self.second,))
        self.loop.coordinator.actor = "replacement"
        self.loop._launch_agent = "second"
        self.assertEqual(planner.coordinator.actor, "operator")
        trace = []
        planner.observer = TraceEvents(trace)
        self.assertEqual([plan.agent.name for plan in planner.iter_plans()], ["first", "first"])
        self.assertEqual(trace[-1], ("event", "queue_idle",
                                    ("Agent first: no eligible work; waiting items: 2 ready",)))
        self.assertEqual(self.github.writes, [])

    def test_worker_preserves_construction_and_generator_read_event_order(self):
        for policy in (None, "on", "off"):
            with self.subTest(approvals=policy):
                cfg = replace(self.config, approvals=policy, trusted_pr_authors=("authorbot[bot]",))
                traces, boundaries = [], []
                for worker in (False, True):
                    github = PollGitHub(issue(1), issue(2))
                    launcher = Loop(cfg, github, "operator", output=lambda _: None)
                    launcher.coordinator.clock = lambda: 1000
                    trace, reached = [], []
                    read = github._read

                    def record_read(name, *args):
                        trace.append(("read", name, args))
                        return read(name, *args)

                    with patch.object(github, "_read", side_effect=record_read):
                        planner = (RunPlanning(launcher, 1000).planner if worker else
                                   Loop(cfg, ObservationReads(github, threading.Event()), "operator",
                                        output=lambda _: None))
                        planner.coordinator.clock = launcher.coordinator.clock
                        planner.observer = TraceEvents(trace)
                        reached.append(len(trace))
                        plans = planner.iter_plans()
                        self.addCleanup(plans.close)
                        self.assertEqual(len(trace), reached[-1])
                        for plan in plans:
                            trace.append(("yield", plan))
                            reached.append(len(trace))
                    traces.append(trace)
                    boundaries.append(reached)
                    self.assertEqual(github.writes, [])
                self.assertEqual(traces[1], traces[0])
                self.assertEqual(boundaries[1], boundaries[0])
                self.assertEqual(boundaries[1][0], 0)
                self.assertEqual(traces[1][0], ("read", "visibility", ()) if policy is None
                                 else ("read", "observe", ()))

    def test_rate_limited_read_reaches_worker_without_owned_run_wait(self):
        error = GitHubError("GET", "items", "rate limit", rate_limited=True, reset_at=1200)
        self.github.read_results["observe"] = [error]
        with patch.object(self.loop, "wait_rate_limit", side_effect=AssertionError("Owned-run wait")):
            worker = RunPlanning(self.loop, 1000)
            with worker.planner.discovery_pass("observation", self.loop.pass_output):
                with self.assertRaises(GitHubError) as raised:
                    list(worker.planner.iter_plans())
        self.assertIs(raised.exception, error)
        self.assertIsNone(worker.planner.github.lease)
        self.assertEqual(self.github.writes, [])
