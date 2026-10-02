from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main, status_rows
from ub_agents.errors import AgentError, CleanupError, RetryableExecutionError
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, iso, payload, seconds
from tests.support import FakeGitHub, agent, config, issue, pr


class FailureCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root, backoff_seconds=10, max_backoff_seconds=100)
        self.github = FakeGitHub(issue(), pr(labels=()))
        self.now = 1000
        self.loop = self.restart()

    def restart(self):
        loop = Loop(config(self.root, self.agent), self.github, 'operator', output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        return loop

    def plan(self, number=1, worker=None):
        return self.loop.coordinator.plan(self.github.item(number), worker or self.agent, ('needs-human',))

    def count(self, number=1, worker=None):
        return len(attempts(self.loop.coordinator.history(number), (worker or self.agent).name, self.now))

    def start(self, number=1, worker=None):
        lease = self.loop.coordinator.claim(self.plan(number, worker), ('needs-human',))
        self.assertIsNotNone(lease)
        self.loop.coordinator.update(lease, state='running', started=True)
        return lease

    def execute(self, result='retry', number=1, error=None, exit_code=0, change=None):
        def execute(*args, **kwargs):
            if error:
                raise error
            lease = next(r for r in reversed(self.loop.coordinator.history(number)) if r['kind'] == 'lease')
            if result is not None:
                self.loop.coordinator.report(lease, result, 'Run result', outcome='done' if result == 'success' else None)
            if change:
                change()
            return exit_code
        with patch('ub_agents.loop.supervise', side_effect=execute):
            return self.loop.execute(self.plan(number))

    def finish_backoff(self):
        latest = next(r for r in reversed(self.loop.coordinator.history(1)) if r['kind'] == 'lease')
        self.now = seconds(latest['retry_after'])

    def test_repeated_failures_reach_limit_and_cli_retry_resets_count_and_backoff(self):
        for expected in range(1, self.agent.max_attempts + 1):
            self.execute()
            self.assertEqual(self.count(), expected)
            self.loop = self.restart()
            latest = self.loop.coordinator.history(1)[-2]
            self.assertEqual(seconds(latest['retry_after']) - self.now, 10 * 2 ** (expected - 1))
            self.assertEqual(self.plan().state, 'blocked' if expected == self.agent.max_attempts else 'backoff')
            self.finish_backoff()
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertFalse(self.loop.tick())
        self.assertEqual(status_rows(self.loop)[0]['attempts'], 3)
        with patch('ub_agents.cli.load_config', return_value=self.loop.config), \
                patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('ub_agents.cli.timestamp', return_value=self.now), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['retry', '--number', '1', '--agent', self.agent.name,
                                   '--reason', 'Transient failure resolved']), 0)
        self.loop = self.restart()
        self.assertEqual((self.plan().state, self.count()), ('ready', 0))
        self.execute()
        self.assertEqual(self.count(), 1)
        self.assertEqual(seconds(self.loop.coordinator.history(1)[-2]['retry_after']) - self.now, 10)

    def test_interrupt_preserves_prior_count_and_next_launch_has_no_backoff(self):
        self.execute()
        self.finish_backoff()
        for _ in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.execute(error=KeyboardInterrupt())
            self.loop = self.restart()
            self.assertEqual((self.plan().state, self.count()), ('ready', 1))
            self.assertNotIn('retry_after', self.loop.coordinator.history(1)[-2])
        self.execute()
        self.assertEqual(self.count(), 2)
        self.assertEqual(seconds(self.loop.coordinator.history(1)[-2]['retry_after']) - self.now, 20)

    def test_success_resets_on_completion_and_after_a_revised_pr(self):
        for number in (1, 2):
            with self.subTest(number=number):
                self.setUp()
                self.github.change(number, labels=frozenset({'needs-changes'}))
                self.execute(number=number)
                self.now += 10
                self.execute('success', number=number)
                self.assertEqual(self.count(number), 0)
                changes = {'labels': frozenset({'needs-changes'})}
                if number == 2:
                    changes['head'] = 'b' * 40
                self.github.change(number, **changes)
                self.loop = self.restart()
                self.assertEqual((self.plan(number).state, self.plan(number).attempt), ('ready', 1))
                self.execute(number=number)
                self.assertEqual(self.count(number), 1)
                self.assertEqual(seconds(self.loop.coordinator.history(number)[-2]['retry_after']) - self.now, 10)

    def test_recovered_success_resets_prior_failure_count(self):
        self.execute()
        self.finish_backoff()
        source = self.start()
        self.loop.coordinator.report(source, 'success', 'Completed', outcome='done')
        self.now += 61
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.count(), 0)
        self.github.change(1, labels=frozenset({'ready'}))
        self.execute()
        self.assertEqual(self.count(), 1)
        self.assertEqual(seconds(self.loop.coordinator.history(1)[-2]['retry_after']) - self.now, 10)

    def test_blocked_and_paused_transitions_preserve_prior_count_and_stay_parked(self):
        for ending in ('blocked', 'stop', 'trigger', 'recovered-stop'):
            with self.subTest(ending=ending):
                self.setUp()
                self.execute()
                self.finish_backoff()
                if ending == 'blocked':
                    self.execute('blocked')
                elif ending == 'recovered-stop':
                    lease = self.start()
                    self.loop.coordinator.report(lease, 'success', 'Completed', outcome='done')
                    self.github.change(1, labels=frozenset({'ready', 'needs-human'}))
                    self.now += 61
                    self.loop.tick()
                else:
                    labels = {'ready', 'needs-human'} if ending == 'stop' else set()
                    self.execute('success', change=lambda: self.github.change(1, labels=frozenset(labels)))
                self.assertEqual(self.count(), 1)
                self.github.change(1, labels=frozenset({'ready'}))
                self.loop = self.restart()
                self.assertEqual(self.plan().state, 'blocked')
                with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                    self.assertFalse(self.loop.tick())

    def test_issue_pr_and_agents_have_independent_counts_and_resets(self):
        self.github.change(2, labels=frozenset({'needs-changes'}))
        other = replace(self.agent, name='other')
        for number, worker in ((1, self.agent), (2, self.agent), (1, other)):
            lease = self.start(number, worker)
            self.loop.coordinator.report(lease, 'retry', 'Failed')
            self.loop.coordinator.release(lease, 'retry', 'Failed')
        source = self.start()
        report = self.loop.coordinator.report(source, 'success', 'Handoff', handoff=2, outcome='done')
        self.loop.finalize(source, self.plan(), report, 'completion')
        self.loop.coordinator.release(source, 'success', 'Handoff')
        self.assertEqual((self.count(1), self.count(2), self.count(1, other)), (0, 1, 1))
        # The copied issue provenance is not a PR run and cannot reset its count.
        self.github.change(2, labels=frozenset({'needs-changes'}))
        self.execute('success', number=2)
        self.assertEqual((self.count(1), self.count(2), self.count(1, other)), (0, 0, 1))

    def test_crash_without_report_counts_once_and_backoff_survives_restart(self):
        for claiming in (False, True):
            with self.subTest(claiming=claiming):
                self.setUp()
                if claiming:
                    self.loop.coordinator.claim(self.plan())
                else:
                    self.start()
                self.assertEqual(self.count(), 0)
                self.now += 60
                self.loop = self.restart()
                self.assertEqual((self.count(), self.plan().state), (1, 'backoff'))
                self.now += 10
                self.assertEqual((self.count(), self.plan().state), (1, 'ready'))
                self.execute()
                self.assertEqual(self.count(), 2)

    def test_retry_and_blocked_recovery_count_source_once_and_claims_cost_nothing(self):
        for status, count, state in (('retry', 1, 'backoff'), ('blocked', 0, 'blocked')):
            with self.subTest(status=status):
                self.setUp()
                source = self.start()
                self.loop.coordinator.report(source, status, 'Reported before crash')
                self.now += 61
                recovery = self.loop.coordinator.claim(self.plan(), recovery=True)
                self.assertEqual(self.count(), count)
                self.now += 61
                self.loop = self.restart()
                with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                    self.assertTrue(self.loop.tick())
                self.assertEqual((self.count(), self.plan().state), (count, state))
                self.assertEqual(recovery['recovered_lease_id'], source['id'])

    def test_classified_failures_retry_but_unsafe_failures_park_and_increment(self):
        cases = [('timeout', RetryableExecutionError('Timeout'), None, 0, 'backoff'),
                 ('launch', RetryableExecutionError('Cannot start'), None, 0, 'backoff'),
                 ('exit-zero', None, None, 0, 'backoff'),
                 ('nonzero', None, None, 1, 'blocked'),
                 ('conflicting-success', None, 'success', 1, 'blocked'),
                 ('unknown', AgentError('Unclassified failure'), None, 0, 'blocked'),
                 ('unexpected', RuntimeError('Unclassified failure'), None, 0, 'blocked')]
        for name, error, result, code, state in cases:
            with self.subTest(name=name):
                self.setUp()
                self.execute(result, error=error, exit_code=code)
                self.loop = self.restart()
                self.assertEqual((self.count(), self.plan().state), (1, state))
                if state == 'blocked':
                    self.now += 10000
                    with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                        self.assertFalse(self.loop.tick())

    def test_setup_failure_counts_and_retries(self):
        with patch('ub_agents.loop.Workspace.prepare', side_effect=AgentError('Git setup failed')), \
                patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.loop.tick()
        self.assertEqual((self.count(), self.plan().state), (1, 'backoff'))

    def test_unconfirmed_cleanup_counts_even_with_success_report_and_never_recovers(self):
        with patch('ub_agents.loop.Workspace.cleanup', side_effect=CleanupError('Termination unknown')):
            with self.assertRaises(CleanupError):
                self.execute('success')
        self.assertEqual(self.count(), 1)
        self.now += 61
        self.loop = self.restart()
        self.assertEqual(self.plan().state, 'blocked')
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertFalse(self.loop.tick())
        self.assertFalse(self.loop.coordinator.history(1)[1]['accepted'])

    def test_legacy_records_keep_start_count_until_cli_reset(self):
        source = self.start()
        legacy = payload(source)
        legacy.pop('attempt_effect')
        legacy.update(state='released', result='success', expires=iso(self.now))
        self.github.update_comment(source['id'], body(legacy))
        self.assertEqual(self.count(), 1)
        self.now += 60
        self.assertEqual(self.count(), 1)
