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
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class FailureCountTests(unittest.TestCase):
    def setUp(self):
        self.refresh = stub_refresh(self)
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
                self.loop.coordinator.report(lease, result, 'Run result', outcome='done' if result == 'success' else None,
                                             action=('Maintainer: choose A or B; recommend A.'
                                                     if result == 'blocked' else None))
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
            self.execute(None, exit_code=1)
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
        self.execute(None, exit_code=1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(seconds(self.loop.coordinator.history(1)[-2]['retry_after']) - self.now, 10)

    def test_nonzero_without_report_then_success_resets_count_and_backoff(self):
        self.execute(None, exit_code=1)
        self.finish_backoff()
        self.execute('success', exit_code=1)
        lease, outcome = self.loop.coordinator.history(1)[-2:]
        self.assertEqual((lease['result'], lease['attempt_effect']), ('success', 'reset'))
        self.assertTrue(outcome['accepted'])
        self.loop = self.restart()
        self.assertEqual(self.count(), 0)
        self.github.change(1, labels=frozenset({'ready'}))
        self.assertEqual((self.plan().state, self.plan().attempt), ('ready', 1))
        self.execute(None, exit_code=1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(seconds(self.loop.coordinator.history(1)[-2]['retry_after']) - self.now, 10)

    def test_failure_report_before_nonzero_exit_keeps_its_count_effect(self):
        for status, count, state in (('retry', 2, 'backoff'), ('blocked', 1, 'blocked')):
            with self.subTest(status=status):
                self.setUp()
                self.execute(None, exit_code=1)
                self.finish_backoff()
                self.execute(status, exit_code=1)
                self.loop = self.restart()
                lease, outcome = self.loop.coordinator.history(1)[-2:]
                self.assertEqual((lease['result'], outcome['status']), (status, status))
                self.assertEqual((self.count(), self.plan().state), (count, state))

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
                self.loop.coordinator.report(source, status, 'Reported before crash',
                                             action=('Maintainer: choose A or B; recommend A.'
                                                     if status == 'blocked' else None))
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
                 ('nonzero', None, None, 1, 'backoff'),
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

    def test_instruction_refresh_failure_preserves_count_without_claiming(self):
        self.execute()
        self.finish_backoff()
        writes = list(self.github.writes)
        with patch('ub_agents.loop.refresh_instructions', side_effect=AgentError('Refresh failed')), \
                patch('ub_agents.loop.Workspace.prepare') as prepare, \
                patch('ub_agents.loop.supervise') as supervise:
            with self.assertRaisesRegex(AgentError, 'Refresh failed'):
                self.loop.tick()
        prepare.assert_not_called()
        supervise.assert_not_called()
        self.assertEqual(self.github.writes, writes)
        self.loop = self.restart()
        self.assertEqual((self.count(), self.plan().state), (1, 'ready'))
        self.execute('success')
        self.assertEqual(self.count(), 0)

    def test_pre_execution_head_trigger_and_state_changes_retry_with_backoff(self):
        for changed in ('head', 'trigger', 'state'):
            with self.subTest(changed=changed):
                self.setUp()
                self.github.items[1] = pr(1)
                self.execute()
                self.finish_backoff()
                changes = {'head': {'head': 'b' * 40},
                           'trigger': {'labels': frozenset()},
                           'state': {'state': 'closed'}}[changed]

                def prepare():
                    self.github.change(1, **changes)
                    return self.root

                with patch('ub_agents.loop.Workspace.prepare', side_effect=prepare), \
                        patch('ub_agents.loop.Workspace.cleanup') as cleanup, \
                        patch('ub_agents.loop.supervise') as supervise:
                    self.assertTrue(self.loop.execute(self.plan()))
                supervise.assert_not_called()
                cleanup.assert_called_once()
                lease = self.loop.coordinator.history(1)[-2]
                self.assertEqual((lease['state'], lease['result'], lease['attempt_effect']),
                                 ('released', 'retry', 'failure'))
                self.assertEqual(self.count(), 2)
                self.assertEqual(seconds(lease['retry_after']) - self.now, 20)
                self.github.change(1, state='open', labels=frozenset({'needs-changes'}))
                self.loop = self.restart()
                self.assertEqual(self.plan().state, 'backoff')
                self.finish_backoff()
                self.assertEqual((self.plan().state, self.plan().item.head),
                                 ('ready', self.github.item(1).head))
                self.execute('success')
                self.assertEqual(self.count(), 0)

    def test_pre_execution_stop_label_preserves_count_and_parks_until_reset(self):
        self.execute()
        self.finish_backoff()

        def prepare():
            # A stop takes precedence even if the trigger also disappears.
            self.github.change(1, labels=frozenset({'needs-human'}))
            return self.root

        with patch('ub_agents.loop.Workspace.prepare', side_effect=prepare), \
                patch('ub_agents.loop.Workspace.cleanup') as cleanup, \
                patch('ub_agents.loop.supervise') as supervise:
            self.assertTrue(self.loop.execute(self.plan()))
        supervise.assert_not_called()
        cleanup.assert_called_once()
        lease = self.loop.coordinator.history(1)[-2]
        self.assertEqual((lease['state'], lease['result'], lease['attempt_effect']),
                         ('released', 'blocked', 'unchanged'))
        self.assertNotIn('retry_after', lease)
        self.assertEqual(self.count(), 1)
        self.github.change(1, labels=frozenset({'ready'}))
        self.now += 10000
        self.loop = self.restart()
        self.assertEqual(self.plan().state, 'blocked')
        with patch('ub_agents.loop.supervise') as supervise:
            self.assertFalse(self.loop.tick())
        supervise.assert_not_called()

    def test_unconfirmed_cleanup_counts_even_with_success_report_and_never_recovers(self):
        with patch('ub_agents.loop.Workspace.cleanup', side_effect=CleanupError('Termination unknown')):
            with self.assertRaises(CleanupError):
                self.execute('success', exit_code=1)
        self.assertEqual(self.count(), 1)
        self.now += 61
        self.loop = self.restart()
        self.assertEqual(self.plan().state, 'blocked')
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertFalse(self.loop.tick())
        self.assertFalse(self.loop.coordinator.history(1)[1]['accepted'])

    def test_crash_before_release_preserves_supervised_verdict_over_early_report(self):
        for ending, count, state in (('interrupt', 0, 'ready'), ('timeout', 1, 'ready'),
                                     ('unsafe', 1, 'blocked')):
            for reported in (None, 'success', 'blocked'):
                with self.subTest(ending=ending, reported=reported):
                    self.setUp()
                    def execute(*args, **kwargs):
                        lease = self.loop.coordinator.history(1)[0]
                        if reported:
                            self.loop.coordinator.report(lease, reported, 'Early report',
                                                         outcome='done' if reported == 'success' else None,
                                                         action=('Maintainer: choose A or B; recommend A.'
                                                                 if reported == 'blocked' else None))
                        if ending == 'interrupt':
                            raise KeyboardInterrupt
                        if ending == 'timeout':
                            raise RetryableExecutionError('Timeout')
                        raise AgentError('Unclassified supervision error')
                    with patch('ub_agents.loop.supervise', side_effect=execute), \
                            patch.object(self.loop.coordinator, 'release', side_effect=AgentError('Release failed')):
                        with self.assertRaisesRegex(AgentError, 'Release failed'):
                            self.loop.tick()
                    self.now += 61
                    self.loop = self.restart()
                    self.assertEqual((self.count(), self.plan().state), (count, state))
                    history = self.loop.coordinator.history(1)
                    self.assertIsNone(self.loop.coordinator.pending_completion(history, self.agent.name, self.now))
                    self.assertFalse(any(r.get('accepted') for r in history))
                    if state == 'blocked':
                        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                            self.assertFalse(self.loop.tick())

    def test_unknown_recovery_failure_counts_and_parks_instead_of_recovering_forever(self):
        source = self.start()
        self.loop.coordinator.report(source, 'success', 'Reported before crash', outcome='done')
        self.now += 61
        with patch.object(self.loop, 'validate_success', side_effect=RuntimeError('Unexpected failure')):
            self.assertTrue(self.loop.tick())
        self.assertEqual((self.count(), self.plan().state), (1, 'blocked'))
        self.now += 61
        self.loop = self.restart()
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertFalse(self.loop.tick())

    def test_log_directory_setup_failure_is_a_durable_retry(self):
        # A file where the run directory belongs makes mkdir fail before execution.
        (self.root / '.ub-agents').mkdir()
        (self.root / '.ub-agents' / 'runs').write_text('not a directory')
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertTrue(self.loop.tick())
        self.assertEqual((self.count(), self.plan().state), (1, 'backoff'))

    def test_unexpected_setup_error_is_unsafe_and_parks(self):
        with patch('ub_agents.loop.Workspace.prepare', side_effect=RuntimeError('Unexpected setup error')):
            self.assertTrue(self.loop.tick())
        self.assertEqual((self.count(), self.plan().state), (1, 'blocked'))

    def test_invalid_success_increments_on_completion_and_recovery(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                self.setUp()
                self.github.items[1] = pr(1)
                if recovery:
                    source = self.start()
                    self.loop.coordinator.report(source, 'success', 'Completed', outcome='done')
                    self.github.change(1, head='b' * 40)
                    self.now += 61
                    self.loop.tick()
                else:
                    self.execute('success', change=lambda: self.github.change(1, head='b' * 40))
                self.assertEqual((self.count(), self.plan().state), (1, 'blocked'))
                self.now += 10000
                self.loop = self.restart()
                with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                    self.assertFalse(self.loop.tick())

    def test_conflicting_expired_outcomes_count_as_failure_in_status(self):
        source = self.start()
        report = self.loop.coordinator.report(source, 'success', 'Reported', outcome='done')
        self.github.create_comment(1, body(payload(report)))
        self.now += 61
        self.assertEqual(self.count(), 1)
        row = status_rows(self.loop)[0]
        self.assertEqual((row['state'], row['attempts']), ('blocked', 1))
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertFalse(self.loop.tick())

    def test_many_successful_pr_revisions_do_not_exhaust_limit_of_one(self):
        self.agent = replace(self.agent, max_attempts=1)
        self.loop = self.restart()
        self.github.items[1] = pr(1)
        for revision in range(5):
            self.github.change(1, labels=frozenset({'needs-changes'}), head=str(revision) * 40)
            self.loop = self.restart()
            self.assertEqual((self.plan().state, self.count()), ('ready', 0))
            self.execute('success')
            self.assertEqual(self.count(), 0)
        self.github.change(1, labels=frozenset({'needs-changes'}))
        self.execute()
        self.assertEqual((self.count(), self.plan().state), (1, 'blocked'))

    def test_accepted_success_resets_before_release_and_recovery_repairs_release(self):
        self.execute()
        self.finish_backoff()
        source = self.start()
        report = self.loop.coordinator.report(source, 'success', 'Completed', outcome='done')
        self.loop.finalize(source, self.plan(), report, 'completion')
        self.assertEqual(self.count(), 0)
        self.now += 61
        self.loop = self.restart()
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.count(), 0)
