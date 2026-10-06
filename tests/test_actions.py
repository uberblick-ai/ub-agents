from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.errors import AgentError, RecordError
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import body, payload, records, reported_actions, timestamp, validate_report_action
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class ActionReportTests(unittest.TestCase):
    action = 'Maintainer: choose A or B; recommend A.'

    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root, kind='issue', outcomes={
            'done': {'add': ('needs-review',), 'remove': ()},
            'human': {'add': ('needs-human',), 'remove': ()}})
        self.github = FakeGitHub(issue(), pr())
        self.now = timestamp()
        self.loop = Loop(config(self.root, self.worker), self.github, 'operator', output=lambda *_: None)
        self.loop.coordinator.clock = lambda: self.now
        self.co = self.loop.coordinator

    def claim(self):
        lease = self.co.claim(self.loop.plans()[0], self.loop.config.stop_labels)
        self.co.update(lease, state='running', started=True)
        return lease

    def cli(self, lease, options):
        env = {'UB_AGENTS_REPOSITORY': 'org/repo', 'UB_AGENTS_ASSIGNMENT': '1',
               'UB_AGENTS_RUN': lease['run'], 'UB_AGENTS_LEASE_ID': str(lease['id'])}
        with patch.dict(os.environ, env), patch('ub_agents.cli.GitHub', return_value=self.github), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            code = main(['report', *options, '--summary', 'Gate details'])
        return code, stderr.getvalue()

    def test_cli_refuses_stop_reports_before_any_write(self):
        lease = self.claim()
        before = list(self.github.writes)
        for options in (['--status', 'blocked'], ['--outcome', 'human']):
            with self.subTest(options=options):
                code, error = self.cli(lease, options)
                self.assertEqual(code, 1)
                self.assertIn('--action', error)
                self.assertEqual(self.github.writes, before)
        with self.assertRaisesRegex(AgentError, 'not declared'):
            self.co.report(lease, 'success', 'Gate details', outcome='unknown', action=self.action)
        self.assertEqual(self.github.writes, before)

    def test_cli_stores_action_on_stop_report(self):
        lease = self.claim()
        code, error = self.cli(lease, ['--outcome', 'human', '--action', self.action])
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(self.co.outcome(lease)['action'], self.action)

    def test_repeated_actions_are_stored_and_each_visible_without_reasoning(self):
        lease = self.claim()
        second = 'Owner: choose immediate or staged rollout; recommend staged.'
        code, error = self.cli(lease, ['--status', 'blocked', '--action', self.action, '--action', second])
        self.assertEqual((code, error), (0, ''))
        outcome = self.co.outcome(lease)
        self.assertEqual(outcome['action'], self.action)
        self.assertEqual(outcome['actions'], [self.action, second])
        self.co.release(lease, 'blocked', outcome['summary'])
        notice = next(c['body'] for c in self.github.comments(1) if c['body'].startswith(ACTION_MARKER))
        visible, details = notice.split('<details>', 1)
        self.assertIn(f'- **{self.action}**\n- **{second}**', visible)
        self.assertNotIn('Gate details', visible)
        self.assertIn('Gate details', details)

    def test_invalid_repeated_actions_are_refused_before_any_write(self):
        lease = self.claim()
        before = list(self.github.writes)
        for actions in ([], [self.action, ''], [self.action, None], ['a' * 300] * 27):
            with self.subTest(actions=actions), self.assertRaisesRegex(AgentError, '--action'):
                self.co.report(lease, 'blocked', 'Gate details', action=actions)
            self.assertEqual(self.github.writes, before)

    def test_options_only_cli_keeps_a_legacy_scalar_without_inventing_an_ask(self):
        for verdict in (['--status', 'blocked'], ['--outcome', 'human']):
            with self.subTest(verdict=verdict):
                self.setUp()
                lease = self.claim()
                options = ['Maintainer: run CI: `mise run ci SHA`', 'Maintainer: merge the test fix.']
                code, error = self.cli(lease, [*verdict, '--option', options[0], '--option', options[1]])
                self.assertEqual((code, error), (0, ''))
                outcome = self.co.outcome(lease)
                self.assertEqual(outcome['options'], options)
                self.assertEqual(outcome['action'], options[0])
                self.assertEqual(reported_actions(outcome), [])
                # v0.1.13 ignores options and still requires this scalar on stop reports.
                legacy = {key: value for key, value in outcome.items() if key != 'options'}
                validate_report_action(lease, legacy)

    def test_actions_and_options_are_distinct_and_share_the_limit(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'blocked', 'Gate details', action=self.action,
                                 option=['Maintainer: run CI.', 'Maintainer: merge a fix.'])
        self.assertEqual(reported_actions(outcome), [self.action])
        self.assertEqual(outcome['actions'], [self.action])
        self.assertEqual(outcome['action'], self.action)
        self.setUp()
        lease = self.claim()
        before = list(self.github.writes)
        with self.assertRaisesRegex(AgentError, '8000'):
            self.co.report(lease, 'blocked', 'Gate details', action=['a' * 300] * 14,
                           option=['o' * 300] * 13)
        self.assertEqual(self.github.writes, before)
        outcome = self.co.report(lease, 'blocked', 'Gate details', action=['a' * 300] * 14,
                                 option=['o' * 300] * 12 + ['o' * 200])
        self.assertEqual(sum(map(len, outcome['actions'] + outcome['options'])), 8000)

    def test_invalid_options_are_refused_before_writes_and_when_parsing_records(self):
        lease = self.claim()
        before = list(self.github.writes)
        for option in ('', ' ', 'a\nb', 'a\n', 'a\rb', 'a\u2028b', 'a\x00b', '界' * 301):
            with self.subTest(option=option):
                code, error = self.cli(lease, ['--status', 'blocked', '--option', option])
                self.assertEqual(code, 1)
                self.assertIn('--option', error)
                self.assertEqual(self.github.writes, before)
        with self.assertRaisesRegex(AgentError, '--option'):
            self.co.report(lease, 'blocked', 'Gate details', option=[])
        outcome = self.co.report(lease, 'blocked', 'Gate details', option=['界' * 300])
        for options in ([], 'wrong type', [None], [''], ['Other first option'], ['界' * 300] * 27):
            with self.subTest(options=options), self.assertRaises(RecordError):
                records([self.github.comments(1)[-1] | {'body': body(payload(outcome) | {'options': options})}])
        with self.assertRaises(RecordError):
            records([self.github.comments(1)[-1] | {'body': body(payload(outcome) | {
                'action': 'a' * 300, 'actions': ['a' * 300] * 14, 'options': ['o' * 300] * 13})}])

    def test_options_only_reports_complete_and_recover(self):
        for recover in (False, True):
            for status in ('blocked', 'success'):
                with self.subTest(recover=recover, status=status):
                    self.setUp()
                    def report(lease):
                        self.co.report(lease, status, 'CI is red. Evidence follows.',
                                       outcome='human' if status == 'success' else None,
                                       option=['Maintainer: run CI.', 'Maintainer: merge a fix.'])
                    if recover:
                        lease = self.claim()
                        report(lease)
                        self.now += 61
                        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                            self.assertTrue(self.loop.tick())
                    else:
                        def run(*args, **kwargs):
                            report(self.co.history(1)[0])
                            return 0
                        with patch('ub_agents.loop.supervise', side_effect=run):
                            self.assertTrue(self.loop.tick())
                    outcome = next(r for r in self.co.history(1) if r['kind'] == 'outcome')
                    self.assertNotIn('rejected', outcome)
                    notice = next(c['body'] for c in self.github.comments(1) if c['body'].startswith(ACTION_MARKER))
                    self.assertIn('1. Maintainer: run CI. (recommended)', notice)
                    self.assertIn('2. Maintainer: merge a fix.', notice)
                    self.assertNotIn('**Maintainer: run CI.**', notice)

    def test_repeated_action_record_validation_requires_matching_legacy_scalar(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'blocked', 'Gate details', action=self.action)
        for actions in ([], 'wrong type', [self.action, ''], ['Other first ask'], [self.action] * 300):
            with self.subTest(actions=actions):
                comment = self.github.comments(1)[-1] | {'body': body(payload(outcome) | {'actions': actions})}
                with self.assertRaises(RecordError):
                    records([comment])

    def test_action_rejects_empty_multiline_and_overlength_values_without_writes(self):
        lease = self.claim()
        before = list(self.github.writes)
        for action in ('', '  ', 'a\nb', 'a\n', 'a\rb', 'a\u2028b', 'a\x00b', '界' * 301):
            with self.subTest(action=action):
                code, error = self.cli(lease, ['--status', 'blocked', '--action', action])
                self.assertEqual(code, 1)
                self.assertIn('--action', error)
                self.assertEqual(self.github.writes, before)
        outcome = self.co.report(lease, 'blocked', 'Gate details', action='界' * 300)
        self.assertEqual(outcome['action'], '界' * 300)

    def test_non_stop_reports_do_not_require_action(self):
        lease = self.claim()
        code, error = self.cli(lease, ['--status', 'retry'])
        self.assertEqual((code, error), (0, ''))
        self.assertNotIn('action', self.co.outcome(lease))
        self.co.release(lease, 'retry', 'Transient failure')
        lease = self.claim()
        code, error = self.cli(lease, ['--outcome', 'done'])
        self.assertEqual((code, error), (0, ''))
        self.assertNotIn('action', self.co.outcome(lease))

    def test_surrounding_spaces_do_not_break_the_notice_bold_sentence(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'blocked', 'Gate details', action=f'  {self.action}  ')
        self.assertEqual(outcome['action'], f'  {self.action}  ')
        self.co.release(lease, 'blocked', outcome['summary'])
        notice = next(c['body'] for c in self.github.comments(1) if c['body'].startswith(ACTION_MARKER))
        visible, details = notice.split('<details>', 1)
        self.assertIn(f'**Action needed**\n\n**{self.action}**\n\n', visible)
        self.assertNotIn('Gate details', visible)
        self.assertIn('Gate details\n\nCandidate:', details)

    def test_invalid_action_field_fails_record_parsing(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'blocked', 'Gate details', action=self.action)
        for action in (None, 1, '', 'a\nb', 'a' * 301):
            with self.subTest(action=action):
                comment = self.github.comments(1)[-1] | {'body': body(payload(outcome) | {'action': action})}
                # body omits None fields, so encode a present null explicitly.
                if action is None:
                    comment['body'] = comment['body'].replace('"accepted": false,', '"accepted": false, "action": null,')
                with self.assertRaises(RecordError):
                    records([comment])

    def remove_action(self, lease, status, legacy=False):
        if legacy:
            self.co.update(lease, action_required=None)
        outcome = self.co.report(lease, status, 'Gate details',
                                 outcome='human' if status == 'success' else None, action=self.action)
        self.co.update_outcome(lease, outcome, action=None)
        self.assertNotIn('action', outcome)
        return outcome

    def test_launcher_rejects_missing_action_even_when_reporting_check_was_bypassed(self):
        for status in ('blocked', 'success'):
            with self.subTest(status=status):
                self.setUp()
                def run(*args, **kwargs):
                    self.remove_action(self.co.history(1)[0], status)
                    return 0
                with patch('ub_agents.loop.supervise', side_effect=run):
                    self.assertTrue(self.loop.tick())
                lease, outcome = self.co.history(1)
                self.assertEqual((lease['result'], lease['attempt_effect']), ('blocked', 'failure'))
                self.assertIn('--action', outcome['rejected'])
                self.assertFalse(outcome['accepted'])
                self.assertEqual(self.github.item(1).labels, {'ready'})

    def test_recovery_rejects_missing_action_and_accepts_historical_stop_records(self):
        for status in ('blocked', 'success'):
            for legacy in (False, True):
                with self.subTest(status=status, legacy=legacy):
                    self.setUp()
                    lease = self.claim()
                    outcome = self.remove_action(lease, status, legacy=legacy)
                    # Parsing remains compatible; validation uses the source lease.
                    self.assertEqual(self.co.history(1)[1]['id'], outcome['id'])
                    self.now += 61
                    with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                        self.assertTrue(self.loop.tick())
                    reported = self.co.history(1)[1]
                    self.assertEqual('rejected' in reported, not legacy)
                    self.assertEqual(reported['accepted'], legacy and status == 'success')
                    self.assertEqual('needs-human' in self.github.item(1).labels, legacy and status == 'success')

    def test_launcher_only_blocked_release_collapses_full_details(self):
        with patch('ub_agents.loop.supervise', side_effect=RuntimeError('Missing evidence')):
            self.assertTrue(self.loop.tick())
        lease, outcome = self.co.history(1)
        self.assertTrue(lease['unreported'])
        self.assertNotIn('action', outcome)
        self.assertNotIn('rejected', outcome)
        notice = next(c['body'] for c in self.github.comments(1) if c['body'].startswith(ACTION_MARKER))
        visible, details = notice.split('<details>', 1)
        self.assertIn('**Maintainer: review the blocker details and decide the next step.**', visible)
        self.assertNotIn('Missing evidence', visible)
        self.assertIn('Missing evidence\n\nCandidate:', details)

    def test_failure_outcome_name_never_changes_retry_completion_or_recovery(self):
        for name in ('typo', 'human', 'done'):
            for recover in (False, True):
                with self.subTest(name=name, recover=recover):
                    self.setUp()
                    def bypass(lease):
                        outcome = self.co.report(lease, 'retry', 'Transient failure')
                        self.github.update_comment(outcome['id'], body(payload(outcome) | {'outcome': name}))
                    if recover:
                        lease = self.claim()
                        bypass(lease)
                        self.now += 61
                        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                            self.assertTrue(self.loop.tick())
                    else:
                        def run(*args, **kwargs):
                            bypass(self.co.history(1)[0])
                            return 1
                        with patch('ub_agents.loop.supervise', side_effect=run):
                            self.assertTrue(self.loop.tick())
                    history = self.co.history(1)
                    leases = [r for r in history if r['kind'] == 'lease']
                    self.assertEqual(leases[-1]['state'], 'released')
                    self.assertEqual(leases[-1]['result'], 'retry')
                    source = next(r for r in history if r['kind'] == 'outcome')
                    self.assertNotIn('rejected', source)
                    self.assertFalse(source['accepted'])
                    self.assertEqual(self.github.item(1).labels, {'ready'})
