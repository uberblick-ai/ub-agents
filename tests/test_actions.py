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
from ub_agents.records import body, payload, records, timestamp
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

    def test_launcher_only_blocked_release_keeps_existing_notice_form(self):
        with patch('ub_agents.loop.supervise', side_effect=RuntimeError('Missing evidence')):
            self.assertTrue(self.loop.tick())
        lease, outcome = self.co.history(1)
        self.assertTrue(lease['unreported'])
        self.assertNotIn('action', outcome)
        self.assertNotIn('rejected', outcome)
        notice = next(c['body'] for c in self.github.comments(1) if c['body'].startswith(ACTION_MARKER))
        self.assertIn('**Action needed**\n\nMissing evidence\n\nCandidate:', notice)
