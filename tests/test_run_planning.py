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
from ub_agents.polling import DiscoveryBudget
from ub_agents.records import iso, records, seconds
from ub_agents.run_planning import ObservationReads, PassEvents, RunPlanning, _Cancelled
from tests.support import DiscoveryCostRunner, MemoryPublisher, PollGitHub, agent, config, issue, pr, stub_refresh


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
        self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
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

    def test_complete_ranked_observations_print_only_coalesced_request_stats(self):
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
        self.assertEqual(len(self.lines), 1)
        self.assertRegex(self.lines[0], r'^Discovery pass observation: .*; gh calls=0, REST quota=0, '
                         r'HTTP 304=0, GraphQL calls=0; candidates reached=3$')
        self.assertIsNot(worker.planner.discovery, self.loop.discovery)
        self.assertFalse(any(s.get('poll_now', {}).get('refreshing') for s in self.memory.snapshots))

    def test_filtered_run_observations_keep_selected_agent_and_waiting_explanation(self):
        selected = agent(self.root, name='triage', triggers=('prepare',))
        self.loop.config = config(self.root, self.cfg.agents[0], selected)
        self.loop._launch_agent = 'triage'
        self.observer.queue_scope('triage')
        self.github.change(2, labels=frozenset({'prepare', 'needs-human'}))
        self.github.change(3, labels=frozenset({'prepare', 'needs-human'}))
        worker, _, _ = self.passes()
        snapshot = self.memory.snapshots[-1]
        self.assertEqual(worker.planner._launch_agent, 'triage')
        self.assertEqual({(r['item'], r['agent']) for r in snapshot['latest_pass']['rows']},
                         {(2, 'triage'), (3, 'triage')})
        self.assertIn('2 parked — Stop label needs-human is present', snapshot['queue_idle'])
        self.assertEqual(self.github.writes, [])

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

    def test_pass_counts_use_worker_client_even_with_launcher_requests_during_observation(self):
        transport = DiscoveryCostRunner()
        source = GitHub('org/project', transport)
        cfg = replace(self.cfg, queue=Queue(priority=Priority(('urgent', 'low'), 'low')))
        loop = Loop(cfg, source, 'operator', output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        loop._requests_before = 0
        worker = RunPlanning(loop, self.now)
        iterate = worker.planner.iter_plans

        def plans():
            source.role('operator')
            yield from iterate()

        with patch.object(worker, '_wait', side_effect=[False, True]), \
                patch.object(worker.planner, 'iter_plans', side_effect=plans):
            worker._run()
        self.assertEqual(len(transport.calls), 97)  # 96 worker calls and one launcher call.
        self.assertEqual((source.gh_calls, source.quota_requests, source.graphql_calls), (1, 1, 0))
        self.assertEqual(len(self.lines), 1)
        self.assertRegex(self.lines[0], r'^Discovery pass observation: .*gh calls=96, REST quota=65, '
                         r'HTTP 304=0, GraphQL calls=31; candidates reached=30$')

    def test_discovery_copies_are_independent_in_both_directions(self):
        self.github.create_comment(1, 'Feedback')
        list(self.loop.iter_plans())
        self.loop.discovery.closed_items.add(4)
        worker = RunPlanning(self.loop, self.now)
        source, copied = self.loop.discovery, worker.planner.discovery
        names = ('items', 'closed_items', 'comments_index', 'cache', 'comment_store',
                 'reconciled_comments', 'comment_window_start', 'repository_index')
        snapshot = {name: deepcopy(getattr(copied, name)) for name in names}
        self.assertEqual({name: getattr(source, name) for name in names}, snapshot)
        self.assertIs(copied.github, worker.planner.github)
        source.items.pop(3)
        source.closed_items.add(5)
        source.comments_index[1][0] = (source.comments_index[1][0][0], 'changed')
        source.cache[('comments', (1,), None)][0]['body'] = 'Changed feedback'
        source.comment_store[1][1]['user']['login'] = 'launcher'
        source.reconciled_comments.clear()
        source.comment_window_start = 100
        source.repository_index.clear()
        source.invalidate(2)
        self.assertEqual({name: getattr(copied, name) for name in names}, snapshot)
        snapshot = {name: deepcopy(getattr(source, name)) for name in names}
        copied.items.pop(2)
        copied.closed_items.add(6)
        copied.comments_index[1][0] = (copied.comments_index[1][0][0], 'worker')
        copied.cache[('comments', (1,), None)][0]['body'] = 'Worker feedback'
        copied.comment_store[1][1]['body'] = 'Worker stored feedback'
        copied.reconciled_comments.add(3)
        copied.comment_window_start = 200
        copied.repository_index.clear()
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
        github.comment_window_start = self.now - 100
        github.resource_quotas = {'core': {'x-ratelimit-remaining': '1000'}}
        github.rest_requests, github.quota_requests = 12, 8
        github.gh_calls, github.not_modified_responses, github.graphql_calls = 17, 4, 5
        github.quota_headers = {'x-ratelimit-remaining': '1000'}
        github.rate_limited = True
        loop = Loop(self.cfg, github, 'operator')
        worker = RunPlanning(loop, self.now)
        copied = worker.planner.github.github.github
        self.assertIsNot(copied, github)
        self.assertIsNot(worker.planner.github, loop.github)
        names = ('_etag_cache', '_comment_cache', '_comment_since', 'comment_window_start', 'resource_quotas')
        snapshot = {name: deepcopy(getattr(copied, name)) for name in names}
        self.assertEqual({name: getattr(github, name) for name in names}, snapshot)
        self.assertEqual((copied.rest_requests, copied.quota_requests), (0, 0))
        self.assertEqual((copied.gh_calls, copied.not_modified_responses, copied.graphql_calls), (0, 0, 0))
        self.assertEqual(copied.quota_headers, {})
        self.assertFalse(copied.rate_limited)
        github._etag_cache.clear()
        github._comment_cache[1]['user']['login'] = 'launcher'
        github._comment_since = iso(self.now)
        github.comment_window_start = self.now
        github.resource_quotas['core']['x-ratelimit-remaining'] = '900'
        self.assertEqual({name: getattr(copied, name) for name in names}, snapshot)
        snapshot = {name: deepcopy(getattr(github, name)) for name in names}
        copied._etag_cache['user'] = ('"worker"', '{}')
        copied._comment_cache[1]['body'] = 'Worker feedback'
        copied._comment_since = iso(self.now + 60)
        copied.comment_window_start = self.now + 60
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
                (0, 3, {}, 30), (5, 3, {}, 30), (300, 3, {}, 2537.4),
                (5, 100, {}, 100),
                (5, 3, {'core': {'x-ratelimit-limit': '5000', 'x-ratelimit-remaining': '999',
                                 'x-ratelimit-reset': '9999'}}, 60)):
            with self.subTest(requests=requests, duration=duration, quotas=quotas):
                self.setUp()
                _, starts, _ = self.passes(requests=requests, duration=duration, quotas=quotas)
                first = 1060 if quotas else 1030
                self.assertEqual(starts[0], first)
                self.assertAlmostEqual(starts[1], first + expected)

    def test_claim_debit_precedes_first_observation_and_run_requests_are_excluded(self):
        for requests, elapsed, expected in ((123, 10, 30), (230, 10, 1526.4),
                                             (123, 40, 40)):
            with self.subTest(requests=requests, elapsed=elapsed):
                self.setUp()
                self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
                claimed_at = self.now
                self.loop._continuous = True
                self.loop._pass_started = claimed_at
                plan = self.loop.plans()[0]

                with self.loop.discovery_pass():
                    self.github.quota_requests += requests
                    boundary = self.loop._claim_boundary()
                    self.now += elapsed
                    self.github.quota_requests += 70  # Fresh claim checks and owned-run traffic.
                    with patch.object(RunPlanning, 'start'):
                        self.assertIsNotNone(self.loop.coordinator.claim(plan))
                    worker = self.loop._run_planning
                    worker.clock = lambda: self.now
                    self.assertIs(worker.planner.discovery_budget, self.loop.discovery_budget)
                    waits = []

                    def wait(delay, rate_until):
                        waits.append(delay)
                        return True

                    with patch('ub_agents.run_planning.monotonic', side_effect=lambda: self.now), \
                            patch.object(worker, '_wait', side_effect=wait):
                        worker._run()  # The claim callback can run this before claim() returns.
                    self.loop._claimed_pass(boundary)
                self.assertAlmostEqual(waits[0], max(0, expected - elapsed))
                self.assertIn(f'REST quota={requests},', self.lines[-1])
                self.assertAlmostEqual(self.loop.discovery_budget.balance, 125 - requests + elapsed / 14.4)

    def test_observation_debt_is_shared_with_later_claiming_discovery(self):
        self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
        self.loop.discovery_budget.debit(230)
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        starts = []
        waits = []

        def wait(delay, rate_until):
            waits.append(delay)
            if starts:
                return True
            self.now += delay
            return False

        def observe():
            starts.append(self.now)
            self.github.quota_requests += 5
            return iter(())

        with patch.object(worker, '_wait', side_effect=wait), \
                patch.object(worker.planner, 'iter_plans', side_effect=observe):
            worker._run()
        self.assertAlmostEqual(waits[0], 1526.4)
        self.assertAlmostEqual(waits[1], 72)
        self.assertAlmostEqual(self.loop.discovery_budget.balance, -4)
        # A claiming pass after work is exempt, but adds its discovery to the debt.
        with self.loop.discovery_pass():
            self.github.quota_requests += 10
        self.assertAlmostEqual(self.loop.discovery_budget.wait_seconds(), 216)

    def test_failed_and_cancelled_observation_passes_debit_the_shared_balance(self):
        for failure in (GitHubError('GET', 'items', 'temporary', retryable=True), _Cancelled()):
            with self.subTest(failure=failure):
                self.setUp()
                self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
                worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)

                def observe():
                    self.github.quota_requests += 230
                    raise failure

                def wait(delay, rate_until):
                    self.now += delay
                    return self.github.quota_requests > 0

                with patch.object(worker, '_wait', side_effect=wait), \
                        patch.object(worker.planner, 'iter_plans', side_effect=observe):
                    worker._run()
                self.assertIn('REST quota=230,', self.lines[-1])
                if isinstance(failure, _Cancelled):
                    self.assertAlmostEqual(self.loop.discovery_budget.wait_seconds(), 1526.4)
                else:
                    self.assertAlmostEqual(self.loop.discovery_budget.balance, -105)

    def test_observation_rechecks_shared_debit_while_waiting(self):
        self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        starts, waits = [], []

        def wait(delay, rate_until):
            waits.append(delay)
            if starts:
                return True
            self.now += delay
            if len(waits) == 1:
                self.loop.discovery_budget.debit(230)
            return False

        def observe():
            starts.append(self.now)
            return iter(())

        with patch.object(worker, '_wait', side_effect=wait), \
                patch.object(worker.planner, 'iter_plans', side_effect=observe):
            worker._run()
        self.assertAlmostEqual(starts[0], 2556.4)
        self.assertEqual(waits[0], 30)
        self.assertAlmostEqual(waits[1], 1526.4)

    def test_repeated_forced_observations_bypass_balance_and_debit_each_pass(self):
        self.loop.discovery_budget = DiscoveryBudget(clock=lambda: self.now)
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        starts, waits = [], []

        def wait(delay, rate_until):
            waits.append(delay)
            if len(starts) == 3:
                return True
            self.now += 10 if starts else 2
            worker._requested()  # Accepted poll-now wakeup.
            return False

        def observe():
            starts.append(self.now)
            self.github.quota_requests += 100
            return iter(())

        with patch.object(worker, '_wait', side_effect=wait), \
                patch.object(worker.planner, 'iter_plans', side_effect=observe):
            worker._run()
        self.assertEqual(starts, [1002, 1012, 1022])
        self.assertAlmostEqual(waits[-1], 2514.4)
        self.assertAlmostEqual(self.loop.discovery_budget.balance, -175 + 20 / 14.4)

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
                          side_effect=lambda stop, delay, update=None, on_request=None:
                          wait(stop, delay, self.loop.request_poll, on_request)):
            self.running_assignment(forced=True)

    def test_once_and_single_item_runs_drop_poll_requests_without_planning(self):
        for launch in ({'once': True}, {'number': 1}):
            with self.subTest(launch=launch):
                self.setUp()
                self.loop.enable_poll_now()

                def finish(*args, **kwargs):
                    self.assertIsNone(self.loop._run_planning)
                    self.assertIsNone(self.loop.poll_now.waiter)
                    snapshot = deepcopy(self.memory.snapshots[-1])
                    self.assertEqual(snapshot['activity']['state'], 'running assignment')
                    self.assertFalse(snapshot.get('poll_now', {}).get('waiting'))
                    self.loop.request_poll()
                    self.assertEqual(self.loop.poll_now.next_allowed, 0)
                    self.assertEqual(self.memory.snapshots[-1], snapshot)
                    self.loop.coordinator.report(self.loop.github.lease, 'success', 'Finished', outcome='done')
                    return 0

                with patch('ub_agents.loop.supervise', side_effect=finish):
                    self.loop.launch(**launch)
                self.assertEqual(self.memory.snapshots[-1]['outcomes'][0]['result'], 'success')
                self.assertEqual(self.loop._planning_workers, [])

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
        regular.assert_called_once_with(worker.stop, 22, on_request=worker._requested)
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
        regular.assert_called_once_with(worker.stop, 17, on_request=worker._requested)

    def test_forced_planning_refresh_resets_regular_schedule_and_drops_inflight_press(self):
        self.loop.enable_poll_now()
        self.loop.poll_now.clock = lambda: self.now
        self.observer.activity('running assignment')
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        wait = self.loop.poll_now.wait
        event = threading.Event
        starts, delays = [], []
        plans = worker.planner.iter_plans

        def refresh():
            starts.append(self.now)
            self.assertEqual(self.memory.snapshots[-1]['activity']['state'], 'running assignment')
            self.assertFalse(self.memory.snapshots[-1]['poll_now']['waiting'])
            self.assertEqual(self.memory.snapshots[-1]['poll_now']['refreshing'], len(starts) == 1)
            self.loop.request_poll()  # A pass already in progress is not queued again.
            self.assertEqual(self.memory.snapshots[-1]['poll_now']['refreshing'], len(starts) == 1)
            yield from plans()

        def waiting(stop, delay, on_request=None):
            delays.append(delay)
            if len(delays) > 1:
                self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
            if len(delays) == 3:
                return True
            wake = event()

            def wakeup(_):
                self.assertTrue(self.memory.snapshots[-1]['poll_now']['waiting'])
                if len(delays) == 1:
                    self.now += 2
                    self.loop.request_poll()
                else:
                    self.now += 1
                    self.loop.request_poll()
                    self.assertFalse(wake.is_set())
                    self.assertEqual(self.memory.snapshots[-1]['poll_now']['cooldown_until'], iso(1012))
                    self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
                    self.now += delay - 1
                return wake.is_set()

            with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                    patch.object(wake, 'wait', side_effect=wakeup):
                return wait(stop, delay, on_request=on_request)

        with patch.object(self.loop.poll_now, 'wait', side_effect=waiting), \
                patch.object(worker.planner, 'iter_plans', side_effect=refresh):
            worker._run()
        self.assertEqual(starts, [1002, 1032])
        self.assertEqual(delays, [30, 30, 30])
        self.assertEqual(self.github.writes, [])

    def test_forced_refresh_label_clears_on_failure_interruption_and_cancellation(self):
        for failure in (GitHubError('GET', 'items', 'temporary', retryable=True),
                        GitHubError('GET', 'items', 'rate limit', rate_limited=True, reset_at=1130),
                        KeyboardInterrupt(), _Cancelled(), None):
            with self.subTest(failure=failure):
                self.setUp()
                self.loop.enable_poll_now()
                self.loop.poll_now.clock = lambda: self.now
                self.observer.activity('running assignment')
                worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
                wait = self.loop.poll_now.wait
                wake = threading.Event()
                started = False

                def waiting(stop, delay, on_request=None):
                    if started:
                        self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
                        return True
                    with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                            patch.object(wake, 'wait', side_effect=lambda _: self.loop.request_poll() or wake.is_set()):
                        return wait(stop, delay, on_request=on_request)

                def refresh():
                    nonlocal started
                    started = True
                    self.assertTrue(self.memory.snapshots[-1]['poll_now']['refreshing'])
                    if failure is not None:
                        raise failure
                    worker.cancel()
                    self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
                    return iter(())

                with patch.object(self.loop.poll_now, 'wait', side_effect=waiting), \
                        patch.object(worker.stop, 'wait', return_value=True), \
                        patch.object(worker.planner, 'iter_plans', side_effect=refresh):
                    worker._run()
                self.assertTrue(started)
                self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
                self.assertEqual(self.memory.snapshots[-1]['activity']['state'], 'running assignment')

    def test_cancelled_refresh_cannot_clear_newer_refresh_when_blocked_read_returns(self):
        self.loop.enable_poll_now()
        worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
        entered, release = threading.Event(), threading.Event()
        wait = self.loop.poll_now.wait

        def refresh():
            entered.set()
            release.wait(3)
            return iter(())

        with patch.object(self.loop.poll_now, 'wait', side_effect=lambda stop, delay, on_request=None:
                          wait(stop, delay, self.loop.request_poll, on_request)), \
                patch.object(worker.planner, 'iter_plans', side_effect=refresh):
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.memory.snapshots[-1]['poll_now']['refreshing'])
                worker.cancel()
                self.assertFalse(self.memory.snapshots[-1]['poll_now']['refreshing'])
                self.assertTrue(worker.thread.is_alive())
                newer = RunPlanning(self.loop, self.now)
                newer._requested()
                release.set()
                worker.close()
                self.assertTrue(self.memory.snapshots[-1]['poll_now']['refreshing'])
                newer.cancel()
            finally:
                release.set()
                worker.close()

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

    def test_after_run_starts_next_pass_immediately_then_paces_empty_pass(self):
        self.loop.config = replace(self.cfg, poll_seconds=120)
        waits = []
        ticks = []

        def tick():
            ticks.append(self.now)
            if len(ticks) == 3:
                self.loop.stop_event.set()
                return False
            if len(ticks) == 2:
                self.now += 5
                return False
            self.now = 1144  # A costly observation starts during the run.
            worker = RunPlanning(self.loop, self.now)
            self.loop._run_planning = worker
            self.now += 5  # Reporting and cleanup take five seconds.
            self.loop._stop_planning()
            return True

        def wait(event, delay, reason):
            waits.append(delay)
            self.now += delay

        with patch('ub_agents.loop.monotonic', side_effect=lambda: self.now), \
                patch.object(self.loop, 'tick', side_effect=tick), \
                patch.object(self.loop, '_wait', side_effect=wait):
            self.loop.launch()
        self.assertEqual(ticks, [1000, 1149, 1269])
        self.assertEqual(waits, [115])

    def test_post_run_pass_claims_next_ready_item_for_every_result(self):
        for result in ('success', 'retry', 'blocked'):
            with self.subTest(result=result):
                self.setUp()
                role = replace(self.cfg.agents[0], max_attempts=1, lease_seconds=LEASE_SECONDS,
                               outcomes={'done': {'add': (), 'remove': ('ready',)}})
                self.loop.config = replace(self.cfg, poll_seconds=120, agents=(role,))
                runs = []

                def supervise(*args, **kwargs):
                    lease = self.loop.github.lease
                    runs.append((lease['assignment'], self.now))
                    if len(runs) == 1:
                        # Publish the same ready queue an in-run refresh sees.
                        self.now = 1144
                        worker = self.loop._run_planning
                        with worker.lock:
                            worker.started = self.now
                        events = PassEvents()
                        worker.planner.observer = events
                        list(worker.planner.iter_plans())
                        self.observer.observation_pass(self.now, events.events)
                        self.now += 5
                        fields = {'outcome': 'done'} if result == 'success' else {}
                        if result == 'blocked':
                            fields['action'] = 'Maintainer: resolve the blocked item before retrying.'
                        self.loop.coordinator.report(lease, result, 'Finished', **fields)
                    else:
                        self.loop.coordinator.report(lease, 'success', 'Next item', outcome='done')
                        self.loop.stop_event.set()
                    return 0

                with patch('ub_agents.loop.monotonic', side_effect=lambda: self.now), \
                        patch('ub_agents.loop.supervise', side_effect=supervise), \
                        patch.object(self.loop.stop_event, 'wait') as wait:
                    self.loop.launch()
                self.assertEqual(runs, [(1, 1000), (2, 1149)])
                wait.assert_not_called()
                self.assertEqual(self.loop.coordinator.history(1)[0]['result'], result)

    def test_observation_rate_limit_wait_survives_run_completion(self):
        self.loop.enable_poll_now()
        waits, ticks = [], []

        def tick():
            ticks.append(self.now)
            if len(ticks) == 2:
                self.loop.stop_event.set()
                return False
            worker = RunPlanning(self.loop, self.now, clock=lambda: self.now)
            self.loop._run_planning = worker
            limited = GitHubError('GET', 'items', 'rate limit', rate_limited=True, reset_at=1200)

            def waiting(delay, rate_until):
                if rate_until is not None:
                    self.now += 5
                    return True
                self.now += delay
                return False

            with patch.object(worker, '_wait', side_effect=waiting), \
                    patch.object(worker.planner, 'iter_plans', side_effect=limited):
                worker._run()
            self.loop._stop_planning()
            return True

        def wait(event, delay, reason):
            waits.append((delay, reason))
            self.assertEqual(self.memory.snapshots[-1]['poll_now']['rate_limit_until'], iso(1200))
            self.loop.request_poll()
            self.now += delay

        with patch('ub_agents.loop.monotonic', side_effect=lambda: self.now), \
                patch.object(self.loop, 'tick', side_effect=tick), \
                patch.object(self.loop, '_wait', side_effect=wait):
            self.loop.launch()
        self.assertEqual(ticks, [1000, 1200])
        self.assertEqual(waits, [(165, 'rate-limit reset')])

    def test_recovered_work_starts_next_claiming_pass_immediately(self):
        self.loop.config = replace(self.cfg, poll_seconds=120)
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan, self.cfg.stop_labels)
        self.loop.coordinator.update(lease, state='running', started=True)
        self.loop.coordinator.report(lease, 'success', 'Recover me', outcome='done')
        self.now += LEASE_SECONDS + 1
        started = self.now
        ticks = []
        tick = self.loop.tick

        def discover():
            ticks.append(self.now)
            if len(ticks) == 2:
                self.loop.stop_event.set()
                return False
            worked = tick()
            self.now += 5
            return worked

        with patch('ub_agents.loop.monotonic', side_effect=lambda: self.now), \
                patch.object(self.loop, 'tick', side_effect=discover), \
                patch.object(self.loop.stop_event, 'wait') as wait, \
                patch('ub_agents.loop.supervise') as supervise:
            self.loop.launch()
        self.assertEqual(ticks, [started, started + 5])
        self.assertTrue(any('recovered durable outcome' in line for line in self.lines))
        wait.assert_not_called()
        supervise.assert_not_called()
