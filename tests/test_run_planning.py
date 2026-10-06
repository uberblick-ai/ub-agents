from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ub_agents.config import LEASE_SECONDS, Priority, Queue
from ub_agents.errors import GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import COMMENT_RECOVERY_SECONDS, Loop
from ub_agents.observations import Observations
from ub_agents.records import iso, records, seconds
from ub_agents.run_planning import ObservationReads, PassEvents, RunPlanning
from tests.support import DiscoveryCostRunner, MemoryPublisher, PollGitHub, config, issue, pr, stub_refresh


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

    def test_claimed_worker_first_pass_uses_cursor_discovery_and_etags(self):
        transport = DiscoveryCostRunner()
        transport.rows = transport.rows[:3]
        transport.comments = {n: transport.comments[n] for n in (1, 2, 3)}
        responses = {}

        def request(command, **kwargs):
            endpoint = command[command.index('--include') + 1]
            path = urlsplit(endpoint).path
            method = command[command.index('--method') + 1]
            if path == 'repos/org/project/issues/1':
                transport.calls.append(command)
                raw = transport.rows[0]
            elif method == 'POST' and path == 'repos/org/project/issues/1/comments':
                transport.calls.append(command)
                raw = {'id': 100, 'body': json.loads(kwargs['input'])['body'],
                       'user': {'login': 'operator'}, 'created_at': iso(self.now),
                       'updated_at': iso(self.now),
                       'issue_url': 'https://api.github.com/repos/org/project/issues/1'}
                transport.comments[1].append(raw)
            else:
                raw = json.loads(transport(command, **kwargs).stdout)
            query = parse_qs(urlsplit(endpoint).query)
            if path.endswith('/issues/comments'):
                raw = [c for c in raw if seconds(c['updated_at']) > seconds(query['since'][0])]
                raw.sort(key=lambda c: seconds(c['updated_at']))
            payload = json.dumps(raw)
            headers, status, code = '', 200, 0
            if method == 'GET' and path != 'graphql' and 'since' not in query:
                version, previous = responses.get(endpoint, (0, None))
                version += payload != previous
                responses[endpoint] = (version, payload)
                etag = f'"v{version}"'
                headers = f'ETag: {etag}\n'
                if f'If-None-Match: {etag}' in command:
                    status, code, payload = 304, 1, ''
            return subprocess.CompletedProcess(command, code, f'HTTP/2.0 {status} Response\n{headers}\n{payload}', '')

        github = GitHub('org/project', request)
        loop = Loop(self.cfg, github, 'operator', observer=self.observer, output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        with patch('ub_agents.github.timestamp', side_effect=lambda: self.now):
            plans = list(loop.iter_plans())
            cursor = github._comment_since
            self.assertEqual(cursor, iso(self.now - 60))
            loop._continuous = True
            loop._pass_started = self.now
            self.now += 1
            with patch.object(RunPlanning, 'start'):
                lease = loop.coordinator.claim(plans[0], self.cfg.stop_labels)
            self.assertIsNotNone(lease)
            worker = loop._run_planning
            self.assertIsNotNone(worker)
            source_cache = deepcopy(loop.discovery.cache)
            source_comments = deepcopy(github._comment_cache)
            source_etags = deepcopy(github._etag_cache)
            source_counters = (github.rest_requests, github.quota_requests)
            transport.calls.clear()
            observed = list(worker.planner.iter_plans())

        paths = [urlsplit(c[c.index('--include') + 1]).path for c in transport.calls]
        scans = [c for c, p in zip(transport.calls, paths) if p.endswith('/issues/comments')]
        self.assertEqual(len(scans), 1)
        self.assertEqual(parse_qs(urlsplit(scans[0][scans[0].index('--include') + 1]).query)['since'], [cursor])
        self.assertNotIn('If-None-Match', ' '.join(scans[0]))
        self.assertIn('If-None-Match: "v1"', transport.calls[0])  # Unchanged issue list revalidates.
        item_reads = [p for p in paths if any(f'/issues/{n}/' in p for n in (1, 2, 3))]
        self.assertEqual(item_reads, ['repos/org/project/issues/1/dependencies/blocked_by',
                                     'repos/org/project/issues/1/comments'])
        self.assertNotIn('graphql', paths)
        for command, path in zip(transport.calls, paths):
            if path in item_reads:
                self.assertIn('If-None-Match: "v1"', command)
        self.assertEqual(observed[0].state, 'owned')
        self.assertEqual(observed[0].history[0]['id'], lease['id'])
        self.assertEqual(worker.planner.discovery.comments_index[1][-1], (lease['id'], lease['created']))
        self.assertEqual(loop.discovery.cache, source_cache)
        self.assertEqual(github._comment_cache, source_comments)
        self.assertEqual(github._etag_cache, source_etags)
        self.assertEqual(github._comment_since, cursor)
        self.assertEqual((github.rest_requests, github.quota_requests), source_counters)
        self.assertIsNone(worker.planner.github.lease)
        self.assertEqual(loop.github.lease, lease)

    def test_discovery_copies_are_independent_in_both_directions(self):
        self.github.create_comment(1, 'Feedback')
        list(self.loop.iter_plans())
        self.loop.discovery.closed_items.add(4)
        worker = RunPlanning(self.loop, self.now)
        source, copied = self.loop.discovery, worker.planner.discovery
        names = ('items', 'closed_items', 'comments_index', 'cache')
        snapshot = {name: deepcopy(getattr(copied, name)) for name in names}
        self.assertEqual({name: getattr(source, name) for name in names}, snapshot)
        self.assertIs(copied.github, worker.planner.github)
        source.items.pop(3)
        source.closed_items.add(5)
        source.comments_index[1][0] = (source.comments_index[1][0][0], 'changed')
        source.cache[('comments', (1,), None)][0]['body'] = 'Changed feedback'
        source.invalidate(2)
        self.assertEqual({name: getattr(copied, name) for name in names}, snapshot)
        snapshot = {name: deepcopy(getattr(source, name)) for name in names}
        copied.items.pop(2)
        copied.closed_items.add(6)
        copied.comments_index[1][0] = (copied.comments_index[1][0][0], 'worker')
        copied.cache[('comments', (1,), None)][0]['body'] = 'Worker feedback'
        copied.invalidate(3)
        self.assertEqual({name: getattr(source, name) for name in names}, snapshot)

    def test_worker_reuses_unchanged_issue_and_pr_discovery_inputs(self):
        for items in ([issue(1), issue(2)], [pr(1, body=''), pr(2, body='')]):
            with self.subTest(kind=items[0].kind):
                github = PollGitHub(*items)
                loop = Loop(self.cfg, github, 'operator')
                list(loop.iter_plans())
                worker = RunPlanning(loop, self.now)
                github.reads.clear()
                self.assertEqual(len(list(worker.planner.iter_plans())), 2)
                self.assertEqual(github.reads, [('observe', ()),
                                               ('repository_comments', (LEASE_SECONDS + COMMENT_RECOVERY_SECONDS,)),
                                               ('role', ('operator',))])

    def test_production_client_copies_caches_but_keeps_own_accounting_and_rate_state(self):
        github = GitHub('org/project')
        github._etag_cache = {'user': ('"initial"', '{"login": "operator"}')}
        github._comment_cache = {1: {'id': 1, 'body': 'Feedback', 'user': {'login': 'operator'}}}
        github._comment_since = iso(self.now - 60)
        github.resource_quotas = {'core': {'x-ratelimit-remaining': '1000'}}
        github.rest_requests, github.quota_requests = 12, 8
        github.quota_headers = {'x-ratelimit-remaining': '1000'}
        github.rate_limited = True
        loop = Loop(self.cfg, github, 'operator')
        worker = RunPlanning(loop, self.now)
        copied = worker.planner.github.github.github
        self.assertIsNot(copied, github)
        self.assertIsNot(worker.planner.github, loop.github)
        names = ('_etag_cache', '_comment_cache', '_comment_since', 'resource_quotas')
        snapshot = {name: deepcopy(getattr(copied, name)) for name in names}
        self.assertEqual({name: getattr(github, name) for name in names}, snapshot)
        self.assertEqual((copied.rest_requests, copied.quota_requests), (0, 0))
        self.assertEqual(copied.quota_headers, {})
        self.assertFalse(copied.rate_limited)
        github._etag_cache.clear()
        github._comment_cache[1]['user']['login'] = 'launcher'
        github._comment_since = iso(self.now)
        github.resource_quotas['core']['x-ratelimit-remaining'] = '900'
        self.assertEqual({name: getattr(copied, name) for name in names}, snapshot)
        snapshot = {name: deepcopy(getattr(github, name)) for name in names}
        copied._etag_cache['user'] = ('"worker"', '{}')
        copied._comment_cache[1]['body'] = 'Worker feedback'
        copied._comment_since = iso(self.now + 60)
        copied.resource_quotas['core']['x-ratelimit-remaining'] = '800'
        self.assertEqual({name: getattr(github, name) for name in names}, snapshot)

    def test_worker_with_cold_launcher_keeps_initial_lookback_scan(self):
        loop = Loop(self.cfg, GitHub('org/project'), 'operator')
        worker = RunPlanning(loop, self.now)
        with patch.object(GitHub, 'observe', return_value=[]), \
                patch.object(GitHub, 'request', return_value=[]) as request, \
                patch('ub_agents.github.timestamp', return_value=self.now):
            self.assertEqual(list(worker.planner.iter_plans()), [])
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query['since'], [iso(self.now - LEASE_SECONDS - COMMENT_RECOVERY_SECONDS)])

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

    def test_poll_now_during_run_refreshes_without_claiming_or_changing_assignment(self):
        self.loop.enable_poll_now()
        wait = self.loop.poll_now.wait
        # Send r once the worker is waiting, long before its regular 30s poll.
        with patch.object(self.loop.poll_now, 'wait',
                          side_effect=lambda stop, delay, update=None: wait(stop, delay, self.loop.request_poll)):
            self.running_assignment(forced=True)

    def test_planning_rate_limit_wait_rejects_poll_but_regular_wait_can_be_forced(self):
        self.loop.enable_poll_now()
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        waits = []

        def limited_wait(delay):
            waits.append(delay)
            self.loop.request_poll()
            self.assertEqual(self.memory.snapshots[-1]['poll_now']['rate_limit_until'],
                             '1970-01-01T00:16:48Z')
            self.now += delay
            return False

        with patch.object(worker.stop, 'wait', side_effect=limited_wait), \
                patch.object(self.loop.poll_now, 'wait', return_value=True) as regular:
            self.assertTrue(worker._wait(30, 1008))
        self.assertEqual(waits, [8])
        regular.assert_called_once_with(worker.stop, 22)
        self.assertIsNone(self.memory.snapshots[-1]['poll_now']['rate_limit_until'])
        self.assertEqual(self.github.writes, [])

    def test_rate_limit_wakeup_delay_does_not_extend_planning_interval(self):
        self.loop.enable_poll_now()
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)

        def late_wakeup(delay):
            self.now += delay + 5
            return False

        with patch.object(worker.stop, 'wait', side_effect=late_wakeup), \
                patch.object(self.loop.poll_now, 'wait', return_value=True) as regular:
            self.assertTrue(worker._wait(30, 1008))
        regular.assert_called_once_with(worker.stop, 17)

    def test_forced_planning_refresh_resets_regular_schedule_and_drops_inflight_press(self):
        self.loop.enable_poll_now()
        self.loop.poll_now.clock = lambda: self.now
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        wait = self.loop.poll_now.wait
        event = threading.Event
        starts, delays = [], []
        plans = worker.planner.iter_plans

        def refresh():
            starts.append(self.now)
            self.loop.request_poll()  # A pass already in progress is not queued again.
            yield from plans()

        def waiting(stop, delay):
            delays.append(delay)
            if len(delays) == 3:
                return True
            wake = event()

            def wakeup(_):
                self.now += 2 if len(delays) == 1 else delay
                if len(delays) == 1:
                    self.loop.request_poll()
                return wake.is_set()

            with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                    patch.object(wake, 'wait', side_effect=wakeup):
                return wait(stop, delay)

        with patch.object(self.loop.poll_now, 'wait', side_effect=waiting), \
                patch.object(worker.planner, 'iter_plans', side_effect=refresh):
            worker._run()
        self.assertEqual(starts, [1002, 1032])
        self.assertEqual(delays, [30, 30, 30])
        self.assertEqual(self.github.writes, [])

    def test_transient_observation_failure_does_not_change_successful_run(self):
        self.running_assignment(GitHubError('GET', 'items', 'temporary', retryable=True))

    def test_observation_rate_limit_does_not_change_successful_run(self):
        self.running_assignment(GitHubError('GET', 'items', 'rate limit', rate_limited=True,
                                            reset_at=self.now + 0.02))

    def running_assignment(self, failure=None, forced=False):
        role = replace(self.cfg.agents[0], outcomes={'done': {'add': (), 'remove': ('ready',)}})
        self.loop.config = replace(self.cfg, poll_seconds=30 if forced else 0.01, agents=(role,))
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
            assignment = deepcopy(self.memory.snapshots[-1]['assignment'])
            if failure:
                self.github.read_results['observe'] = [failure]
            self.assertTrue(completed.wait(3), 'Observation did not complete while running')
            self.assertEqual(self.github.writes, writes)
            snapshot = self.memory.snapshots[-1]
            self.assertEqual({r['item'] for r in snapshot['latest_pass']['rows']}, {1, 2, 3})
            self.assertEqual(snapshot['activity']['state'], 'running assignment')
            self.assertEqual(snapshot['assignment']['item'], 1)
            self.assertEqual(snapshot['assignment'], assignment)
            self.assertFalse(any('No eligible work' in line for line in self.lines))
            # The previously observed ready item is no longer authorized.
            self.github.change(2, labels=frozenset({'needs-human'}))
            self.loop.coordinator.report(self.loop.github.lease, 'success', 'Finished', outcome='done')
            if forced:
                self.loop.stop_event.set()
            return 0

        with patch('ub_agents.loop.supervise', side_effect=supervise), \
                patch.object(self.loop, 'tick', side_effect=claiming_pass):
            self.loop.launch()
        leases = [r for comments in self.github.store.values() for r in records(comments)
                  if r['kind'] == 'lease']
        self.assertEqual({r['assignment'] for r in leases}, {1})
        self.assertEqual(leases[-1]['result'], 'success', self.lines)
        self.assertEqual(len(ticks), 1 if forced else 2)
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
        outcome = self.loop.coordinator.report(lease, 'success', 'Maintainer needed', outcome='done',
                                               action='Maintainer: choose A or B; recommend A.')
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
