from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.config import Priority, Queue
from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import records
from ub_agents.run_planning import ObservationReads, PassEvents, RunPlanning
from tests.support import MemoryPublisher, PollGitHub, config, issue, stub_refresh


class RunPlanningTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = replace(config(self.root), poll_seconds=30)
        self.github = PollGitHub(issue(1), issue(2), issue(3))
        self.memory = MemoryPublisher()
        self.observer = Observations(self.cfg, 'operator', None, self.memory)
        self.lines = []
        self.loop = Loop(self.cfg, self.github, 'operator', observer=self.observer,
                         output=self.lines.append, interrupt_event=threading.Event())
        self.now = 1000
        self.loop.coordinator.clock = lambda: self.now
        self.loop._requests_before = 0

    def passes(self, *, requests=0, duration=0, failure=None, quotas=None):
        worker = RunPlanning(self.loop, self.now)
        starts, waits = [], []
        iterate = worker.planner.iter_plans

        def plans():
            starts.append(self.now)
            self.github.quota_requests += requests
            self.now += duration
            if failure and len(starts) == 1:
                raise failure
            yield from iterate()

        def wait(delay):
            waits.append(delay)
            if len(starts) == 2:
                return True
            self.now += delay
            return False

        self.github.resource_quotas = quotas or {}
        with patch('ub_agents.run_planning.monotonic', side_effect=lambda: self.now), \
                patch.object(worker.stop, 'wait', side_effect=wait), \
                patch.object(worker.planner, 'iter_plans', side_effect=plans):
            worker._run()
        return worker, starts, waits

    def test_complete_ranked_observations_are_read_only_and_silent(self):
        priority = Priority(('priority:urgent', 'priority:high', 'priority:low'), None)
        self.loop.config = replace(self.cfg, queue=Queue(priority=priority))
        self.github.change(3, labels=frozenset({'ready', 'priority:urgent'}))
        self.github.change(2, labels=frozenset({'ready', 'priority:high'}))
        worker, _, _ = self.passes()
        latest = self.memory.snapshots[-1]['latest_pass']
        self.assertEqual(latest['state'], 'complete')
        self.assertEqual([r['item'] for r in latest['rows']], [3, 2, 1])
        self.assertEqual([r['priority'] for r in latest['rows']], ['urgent', 'high', None])
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.lines, [])
        self.assertIsNot(worker.planner.discovery, self.loop.discovery)

    def test_effective_default_and_inherited_priority_words(self):
        priority = Priority(('queue:priority:urgent', 'priority:medium'), 'priority:medium')
        self.loop.config = replace(self.cfg, queue=Queue(priority=priority))
        self.github.change(2, labels=frozenset({'ready', 'queue:priority:urgent'}))
        self.github.dependencies = {2: [1]}
        self.passes()
        rows = self.memory.snapshots[-1]['latest_pass']['rows']
        self.assertEqual({r['item']: r['priority'] for r in rows},
                         {1: 'urgent', 2: 'urgent', 3: 'medium'})

    def test_observation_spacing_uses_cost_duration_low_quota_and_hour_cap(self):
        for requests, duration, quotas, expected in (
                (0, 3, {}, 30), (5, 3, {}, 72), (300, 3, {}, 3600),
                (5, 100, {}, 100),
                (5, 3, {'core': {'x-ratelimit-limit': '5000', 'x-ratelimit-remaining': '999',
                                 'x-ratelimit-reset': '9999'}}, 144)):
            with self.subTest(requests=requests, duration=duration, quotas=quotas):
                self.setUp()
                _, starts, _ = self.passes(requests=requests, duration=duration, quotas=quotas)
                first = 1060 if quotas else 1030
                self.assertEqual(starts, [first, first + expected])

    def test_failed_and_rate_limited_passes_keep_previous_rows_and_continue(self):
        for failure, gap in (
                (GitHubError('GET', 'items', 'temporary', retryable=True), 30),
                (GitHubError('GET', 'items', 'rate limit', rate_limited=True, reset_at=1130), 100)):
            with self.subTest(failure=failure):
                self.setUp()
                self.observer.begin_pass()
                list(self.loop.iter_plans())
                self.observer.complete_pass()
                previous = deepcopy(self.memory.snapshots[-1]['latest_pass'])
                self.github.change(2, state='closed')
                _, starts, _ = self.passes(failure=failure)
                self.assertEqual(starts, [1030, 1030 + gap])
                passes = [s['latest_pass'] for s in self.memory.snapshots
                          if s['latest_pass'] and s['latest_pass']['started_at'] != previous['started_at']]
                self.assertEqual(len(passes), 1)  # No partial failed-pass publication.
                self.assertEqual([r['item'] for r in passes[0]['rows']], [1, 3])
                self.assertEqual(self.github.writes, [])
                self.assertFalse(self.loop.stop_event.is_set())

    def test_reader_forbids_mutations_and_cancels_between_reads(self):
        stop = threading.Event()
        reader = ObservationReads(self.github, stop)
        for name in ('create_comment', 'update_comment', 'add_labels', 'remove_label', 'minimize_comment'):
            with self.assertRaises(AttributeError):
                getattr(reader, name)
        self.assertEqual(reader.item(1).number, 1)
        stop.set()
        with self.assertRaises(Exception):
            reader.item(1)
        self.assertEqual(self.github.writes, [])

    def test_running_assignment_refreshes_without_writes_then_rechecks_claim_authority(self):
        self.running_assignment()

    def test_transient_observation_failure_does_not_change_successful_run(self):
        self.running_assignment(GitHubError('GET', 'items', 'temporary', retryable=True))

    def test_observation_rate_limit_does_not_change_successful_run(self):
        self.running_assignment(GitHubError('GET', 'items', 'rate limit', rate_limited=True,
                                            reset_at=self.now + 0.02))

    def running_assignment(self, failure=None):
        role = replace(self.cfg.agents[0], outcomes={'done': {'add': (), 'remove': ('ready',)}})
        self.loop.config = replace(self.cfg, poll_seconds=0.01, agents=(role,))
        completed = threading.Event()
        submit = self.memory.submit

        def publish(data):
            submit(data)
            latest = self.memory.snapshots[-1].get('latest_pass') or {}
            if latest.get('state') == 'complete':
                completed.set()

        self.memory.submit = publish
        # An approval gate must remain a plan, without being parked on GitHub.
        self.github.timelines[3] = []
        ticks = []
        tick = self.loop.tick

        def claiming_pass():
            ticks.append(True)
            worked = tick()
            if len(ticks) == 2:
                self.loop.stop_event.set()
            return worked

        def supervise(*args, **kwargs):
            writes = list(self.github.writes)
            if failure:
                self.github.read_results['observe'] = [failure]
            self.assertTrue(completed.wait(3), 'Observation did not complete while running')
            self.assertEqual(self.github.writes, writes)
            snapshot = self.memory.snapshots[-1]
            self.assertEqual({r['item'] for r in snapshot['latest_pass']['rows']}, {1, 2, 3})
            self.assertEqual(snapshot['activity']['state'], 'running assignment')
            self.assertEqual(snapshot['assignment']['item'], 1)
            self.assertFalse(any('No eligible work' in line for line in self.lines))
            # The previously observed ready item is no longer authorized.
            self.github.change(2, labels=frozenset({'needs-human'}))
            self.loop.coordinator.report(self.loop.github.lease, 'success', 'Finished', outcome='done')
            return 0

        with patch('ub_agents.loop.supervise', side_effect=supervise), \
                patch.object(self.loop, 'tick', side_effect=claiming_pass):
            self.loop.launch()
        leases = [r for comments in self.github.store.values() for r in records(comments)
                  if r['kind'] == 'lease']
        self.assertEqual({r['assignment'] for r in leases}, {1})
        self.assertEqual(leases[-1]['result'], 'success', self.lines)
        self.assertEqual(len(ticks), 2)
        self.assertTrue(self.memory.snapshots[-1]['ended'])
        self.assertEqual(self.loop._planning_workers, [])

    def test_slow_observation_does_not_delay_report_transitions_or_release(self):
        role = replace(self.cfg.agents[0], outcomes={'done': {'add': (), 'remove': ('ready',)}})
        self.loop.config = replace(self.cfg, poll_seconds=0.01, agents=(role,))
        entered, unblock = threading.Event(), threading.Event()
        self.addCleanup(unblock.set)
        observe, release = self.github.observe, self.loop.coordinator.release

        def read(*args, **kwargs):
            if threading.current_thread().name == 'run-planning':
                entered.set()
                unblock.wait(3)
            return observe(*args, **kwargs)

        def finish(*args, **kwargs):
            self.assertTrue(entered.wait(3))
            self.loop.coordinator.report(self.loop.github.lease, 'success', 'Finished', outcome='done')
            self.loop.stop_event.set()
            return 0

        def released(*args, **kwargs):
            self.assertFalse(unblock.is_set())
            self.assertTrue(self.loop._run_planning.thread.is_alive())
            try:
                return release(*args, **kwargs)
            finally:
                unblock.set()

        with patch.object(self.github, 'observe', side_effect=read), \
                patch('ub_agents.loop.supervise', side_effect=finish), \
                patch.object(self.loop.coordinator, 'release', side_effect=released):
            self.loop.launch()
        outcomes = self.memory.snapshots[-1]['outcomes']
        self.assertEqual(outcomes[0]['acceptance'], 'finalized')
        self.assertEqual(outcomes[0]['result'], 'success')
        self.assertEqual(self.loop._planning_workers, [])

    def test_worker_start_failure_does_not_change_run_outcome(self):
        def finish(*args, **kwargs):
            self.loop.coordinator.report(self.loop.github.lease, 'success', 'Finished', outcome='done')
            self.loop.stop_event.set()
            return 0

        with patch.object(RunPlanning, 'start', side_effect=RuntimeError('No worker available')), \
                patch('ub_agents.loop.supervise', side_effect=finish):
            self.loop.launch()
        self.assertEqual(self.memory.snapshots[-1]['outcomes'][0]['result'], 'success')
        self.assertEqual(self.loop._planning_workers, [])
        self.assertTrue(any('Cannot start queue observations' in line for line in self.lines))

    def test_completed_pass_cannot_overwrite_newer_own_outcome_or_notice(self):
        role = replace(self.cfg.agents[0], outcomes={'done': {'add': ('needs-human',), 'remove': ('ready',)}})
        self.loop.config = replace(self.cfg, agents=(role,))
        self.observer.begin_pass()
        plan = self.loop.plans()[0]
        self.observer.assignment(plan)
        lease = self.loop.coordinator.claim(plan, self.cfg.stop_labels)
        self.loop.coordinator.update(lease, state='running', started=True)
        worker = RunPlanning(self.loop, self.now)
        events = PassEvents()
        worker.planner.observer = events
        list(worker.planner.iter_plans())
        # The pass read the old labels before the run finalized a parking outcome.
        outcome = self.loop.coordinator.report(lease, 'success', 'Maintainer needed', outcome='done')
        self.loop.finalize(lease, plan, outcome, 'test completion')
        self.loop.coordinator.release(lease, 'success', 'Maintainer needed')
        previous = deepcopy(self.memory.snapshots[-1])
        self.observer.observation_pass(self.now, events.events)
        snapshot = self.memory.snapshots[-1]
        self.assertEqual(snapshot['outcomes'], previous['outcomes'])
        self.assertEqual(snapshot['action_needed'].get('1'), previous['action_needed']['1'])
        self.assertEqual(snapshot['histories']['1'], previous['histories']['1'])

    def test_after_run_wait_uses_latest_observation_start_and_only_poll_seconds(self):
        waits = []
        ticks = []

        def tick():
            ticks.append(self.now)
            if len(ticks) == 2:
                self.loop.stop_event.set()
                return False
            self.now = 1144  # A costly observation starts during the run.
            self.loop._pass_started = self.now
            self.now += 7  # Reporting and cleanup take seven seconds.
            return True

        def wait(event, delay, reason):
            waits.append(delay)
            self.now += delay

        with patch('ub_agents.loop.monotonic', side_effect=lambda: self.now), \
                patch.object(self.loop, 'tick', side_effect=tick), \
                patch.object(self.loop, '_wait', side_effect=wait):
            self.loop.launch()
        self.assertEqual(ticks, [1000, 1174])
        self.assertEqual(waits, [23])
