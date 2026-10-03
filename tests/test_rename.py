"""The one-upgrade compatibility boundary for the plural product name."""

from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents import __version__
from ub_agents.approvals import approval_body, check_pr, parse_approval
from ub_agents.cli import legacy_main, main
from ub_agents.config import DEFAULT_CONFIG, resolve_config_path
from ub_agents.coordination import Coordinator
from ub_agents.errors import LostOwnership, RecordError
from ub_agents.loop import Loop
from ub_agents.notices import Notices
from ub_agents.records import MARKER, iso, records, seconds
from tests.support import FakeGitHub, agent, config, edit_lease, issue, pr
from tests.test_approval_enforcement import feedback


def old_record_names(github, version=2):
    for comments in github.store.values():
        for comment in comments:
            text = comment['body']
            if not text.startswith(MARKER):
                continue
            if version == 1:
                payload = text.split('```json\n', 1)[1].split('\n```', 1)[0]
                comment['body'] = '<!-- ub-agent:v1 -->\n\n```json\n' + payload + '\n```\n'
            else:
                comment['body'] = text.replace(MARKER, '<!-- ub-agent:v2 -->', 1)


class RenameCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.enterContext(patch('ub_agents.config._warned_legacy_config', False))

    def write_config(self, name=DEFAULT_CONFIG):
        path = self.root / name
        path.write_text('repository: org/project\nagents:\n  worker:\n'
                        '    trigger: ready\n    command: [python3, -c, pass]\n'
                        '    outcomes: {done: {}}\n')
        return path

    def test_installed_alias_preserves_stdout_and_exit_status(self):
        self.write_config()
        binaries = Path(sys.executable).parent
        for args in (['--version'], ['--help'], ['check'], ['missing-command']):
            with self.subTest(args=args):
                result = subprocess.run([str(binaries / 'ub-agents'), *args], cwd=self.root,
                                        capture_output=True, text=True, timeout=10)
                alias = subprocess.run([str(binaries / 'ub-agent'), *args], cwd=self.root,
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual((alias.stdout, alias.returncode), (result.stdout, result.returncode))
                notice, remaining = alias.stderr.split('\n', 1)
                self.assertIn('deprecated', notice)
                self.assertIn('ub-agents', notice)
                self.assertEqual(remaining, result.stderr)
                if args == ['--version']:
                    self.assertEqual(result.stdout, f'ub-agents {__version__}\n')

    def test_default_config_fallback_warns_once_and_prefers_new_name(self):
        old = self.write_config('ub-agent.yaml')
        with chdir(self.root), redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.assertEqual(main(['check']), 0)
            self.assertEqual(resolve_config_path(), old)
            warning = error.getvalue()
            self.assertEqual(len(warning.splitlines()), 1)
            self.assertIn('ub-agent.yaml has been renamed to ub-agents.yaml', warning)
            preferred = self.write_config()
            self.assertEqual(resolve_config_path(), preferred)
            old.write_text('invalid: configuration\n')
            self.assertEqual(main(['check']), 0)
            self.assertEqual(error.getvalue(), warning)

    def test_explicit_config_never_falls_back_or_warns(self):
        old = self.write_config('ub-agent.yaml')
        with chdir(self.root), redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', DEFAULT_CONFIG, 'check']), 1)
            self.assertIn('Cannot read configuration', error.getvalue())
            self.assertNotIn('renamed', error.getvalue())
            error.seek(0)
            error.truncate()
            self.assertEqual(main(['--config', str(old), 'check']), 0)
            self.assertEqual(error.getvalue(), '')

    def test_doctor_and_launcher_share_default_resolution(self):
        old = self.write_config('ub-agent.yaml')
        for command in ('doctor', 'launch', 'status'):
            with self.subTest(command=command), chdir(self.root), \
                    patch('ub_agents.cli.run', return_value=0) as run, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main([command]), 0)
                args = run.call_args.args[0]
                self.assertEqual(args.config, old)
                self.assertTrue(args.default_config)

    def test_init_always_writes_new_default(self):
        from tests.support import DoctorGitHub
        with chdir(self.root), patch('ub_agents.cli.GitHub', return_value=DoctorGitHub()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['init', '--repository', 'org/project']), 0)
        self.assertTrue((self.root / DEFAULT_CONFIG).exists())
        self.assertFalse((self.root / 'ub-agent.yaml').exists())
        self.assertIn('.ub-agents/', (self.root / '.gitignore').read_text())

    def test_report_from_old_launcher_through_both_commands(self):
        for entrypoint in (main, legacy_main):
            with self.subTest(entrypoint=entrypoint):
                github = FakeGitHub(issue())
                worker = agent(self.root)
                co = Coordinator(github, 'operator')
                lease = co.claim(co.plan(github.item(1), worker, ()))
                co.update(lease, state='running', started=True)
                old_record_names(github)
                env = {'UB_AGENT_REPOSITORY': github.repository, 'UB_AGENT_ASSIGNMENT': '1',
                       'UB_AGENT_RUN': lease['run'], 'UB_AGENT_LEASE_ID': str(lease['id'])}
                with patch.dict(os.environ, env, clear=True), \
                        patch('ub_agents.cli.GitHub', return_value=github), \
                        redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(entrypoint(['report', '--outcome', 'done', '--summary', 'Finished']), 0)
                outcome = co.outcome(lease)
                self.assertEqual(json.loads(output.getvalue()),
                                 {'run': lease['run'], 'status': 'success', 'url': outcome['url']})
                self.assertEqual(len(error.getvalue().splitlines()), int(entrypoint is legacy_main))
                self.assertTrue(github.store[1][-1]['body'].startswith(MARKER))

    def test_new_report_environment_wins_even_when_empty(self):
        github = FakeGitHub(issue())
        co = Coordinator(github, 'operator')
        lease = co.claim(co.plan(github.item(1), agent(self.root), ()))
        co.update(lease, state='running', started=True)
        env = {'UB_AGENTS_REPOSITORY': github.repository, 'UB_AGENTS_ASSIGNMENT': '1',
               'UB_AGENTS_RUN': lease['run'], 'UB_AGENTS_LEASE_ID': str(lease['id']),
               'UB_AGENT_REPOSITORY': 'wrong/repository', 'UB_AGENT_ASSIGNMENT': '999',
               'UB_AGENT_RUN': 'wrong', 'UB_AGENT_LEASE_ID': '999'}
        with patch.dict(os.environ, env, clear=True), patch('ub_agents.cli.GitHub', return_value=github) as factory, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['report', '--outcome', 'done', '--summary', 'Finished']), 0)
            factory.assert_called_once_with(github.repository)
        for entrypoint in (main, legacy_main):
            with self.subTest(entrypoint=entrypoint), patch.dict(os.environ, env | {'UB_AGENTS_RUN': ''}, clear=True), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                self.assertEqual(entrypoint(['report', '--outcome', 'done', '--summary', 'Finished']), 1)
                self.assertEqual(output.getvalue(), '')


class DurableRenameTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.worker = agent(self.root)
        self.github = FakeGitHub(issue(), pr())
        self.now = 1000
        self.co = Coordinator(self.github, 'operator', lambda: self.now, output=lambda *_: None)

    def start(self):
        lease = self.co.claim(self.co.plan(self.github.item(1), self.worker, ()))
        self.co.update(lease, state='running', started=True)
        return lease

    def test_old_v1_and_v2_leases_preserve_ownership_attempts_and_recovery(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.github = FakeGitHub(issue(), pr())
                self.co = Coordinator(self.github, 'operator', lambda: self.now, output=lambda *_: None)
                failed = self.start()
                self.co.release(failed, 'retry', 'First failed attempt')
                lease = self.start()
                outcome = self.co.report(lease, 'success', 'Finished', outcome='done')
                old_record_names(self.github, version)
                self.assertEqual(self.co.plan(self.github.item(1), self.worker, ()).state, 'owned')
                self.assertIsNone(self.co.claim(self.co.plan(self.github.item(1), self.worker, ())))
                self.now = seconds(lease['expires']) + 1
                plan = self.co.plan(self.github.item(1), self.worker, ())
                self.assertEqual(plan.state, 'recover')
                self.assertEqual(self.co.pending_completion(self.co.history(1), 'worker', self.now), outcome)
                self.assertEqual(plan.attempt, 2)
                recovery = self.co.claim(plan, recovery=True)
                self.assertIsNotNone(recovery)
                with self.assertRaises(LostOwnership):
                    self.co.assert_owned(lease)
                old_record_names(self.github, version)
                with self.assertRaises(LostOwnership):
                    self.co.assert_owned(lease)
                self.assertTrue(self.co.repository_history()[0])
                self.assertEqual(self.co.repository_history()[1], set())

    def test_old_marker_validation_keeps_format_version_semantics(self):
        comment = {'id': 1, 'body': '<!-- ub-agent:v1 -->\nunreadable', 'user': {'login': 'operator'}}
        self.assertEqual(records([comment]), [])
        with self.assertRaises(RecordError):
            records([comment | {'body': '<!-- ub-agent:v2 -->\nunreadable'}])

    def test_old_branch_links_pr_ownership_to_issue(self):
        lease = self.start()
        branch = f'ub-agent/worker/1/{lease["run"]}'
        self.co.update(lease, branch=branch)
        old_record_names(self.github)
        self.github.change(2, branch=branch)
        self.assertEqual(self.co.plan(self.github.item(2), self.worker, ()).state, 'owned')
        edit_lease(self.github, lease, cleanup='unconfirmed', expires=iso(self.now - 1))
        self.assertEqual(self.co.plan(self.github.item(2), self.worker, ()).state, 'blocked')

    def test_old_approval_and_system_comments_keep_pr_gate_and_snapshot(self):
        github = self.github
        github.pr_content = lambda n: github.issue_content(n) | {
            'head': github.item(n).head, 'author': {'login': 'outsider'}, 'head_repository': github.repository}
        approval = approval_body(2, github.item(2).title, github.item(2).body, [], head=github.item(2).head)
        approval = approval.replace('ub-agents:approval:', 'ub-agent:approval:', 1)
        self.assertIsNotNone(parse_approval(approval, 2, 'pr'))
        system = ['<!-- ub-agent:v1 -->\nold record', '<!-- ub-agent:v2 -->\nold record',
                  '<!-- ub-agent:action-needed old-run -->\nAction needed']
        github.store[2] = [feedback(100, 'maintainer', approval, 5)] + [
            feedback(101 + i, 'operator', text, 10) for i, text in enumerate(system)]
        check = check_pr(github, 2, {'needs-changes'}, 'operator')
        self.assertTrue(check.allowed, check.reason)
        self.assertEqual(check.snapshot['comments'], [])
        github.change(2, head='b' * 40)
        self.assertFalse(check_pr(github, 2, {'needs-changes'}, 'operator').allowed)

    def test_old_action_notices_deduplicate_and_minimize_after_restart(self):
        lease = self.start()
        outcome = self.co.report(lease, 'blocked', 'Decision needed')
        self.co.release(lease, 'blocked', 'Decision needed')
        notice = self.github.store[1][-1]
        notice['body'] = notice['body'].replace('ub-agents:action-needed', 'ub-agent:action-needed', 1)
        notices = Notices(self.github, 'operator', output=lambda *_: None)
        before = self.github.writes[:]
        notices.post_action(1, lease, outcome, 'Decision needed', ())
        self.assertEqual(self.github.writes, before)
        notices.resumed(1)
        self.assertIn(notice['id'], self.github.minimized_ids)

    def test_old_approval_notice_deduplicates_after_restart(self):
        self.github.timelines[1] = []
        loop = Loop(config(self.root), self.github, 'operator')
        check = loop.input_check(self.github.item(1))
        self.co.notices.approval(1, check, ('needs-human',), ('ready',))
        notice = self.github.store[1][-1]
        notice['body'] = notice['body'].replace('ub-agents:action-needed', 'ub-agent:action-needed', 1)
        before = self.github.writes[:]
        Notices(self.github, 'operator').approval(1, check, ('needs-human',), ('ready',))
        self.assertEqual(self.github.writes, before)
