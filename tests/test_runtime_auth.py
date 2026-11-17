from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.cli.status import status_rows
from ub_agents.config import Runtime
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import timestamp
from ub_agents.run_planning import RunPlanning
from ub_agents.runtime_auth import AUTH_PROBES, RuntimeAuth
from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue, pr, stub_refresh


class RuntimeAuthTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.enterContext(patch('ub_agents.coordination.shutil.which', return_value='installed'))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.role = agent(self.root, command=(), runtimes=(Runtime('codex', 'model', 'high'),))
        self.github = FakeGitHub(issue(1), issue(2))
        self.lines = []
        self.loop = Loop(config(self.root, self.role), self.github, 'operator', output=self.lines.append)
        self.now = timestamp()
        self.loop.coordinator.clock = lambda: self.now
        self.loop.runtime_auth.clock = lambda: self.now
        self.enterContext(patch.object(self.loop.maintenance, 'available', return_value=True))
        self.probe = self.enterContext(patch('ub_agents.runtime_auth.run_check', return_value='Logged out'))

    def complete(self, *args, **kwargs):
        self.loop.coordinator.report(self.loop.github.lease, 'success', 'Work completed', outcome='done')
        return 0

    def test_logout_holds_claims_without_writes_and_recovery_announces_once(self):
        publisher = MemoryPublisher()
        self.loop.observer = Observations(self.loop.config, 'operator', None, publisher)
        for _ in range(3):
            self.assertFalse(self.loop.tick())
        self.assertEqual(self.probe.call_count, 3)
        self.probe.assert_called_with(AUTH_PROBES['codex'], self.root)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.github.item(1).labels, frozenset({'ready'}))
        rows = status_rows(self.loop)
        self.assertEqual([(r['state'], r['attempts'], r['lease']) for r in rows],
                         [('waiting', 0, None), ('waiting', 0, None)])
        self.assertTrue(all(r['reason'] == 'codex is not logged in; run codex login' for r in rows))
        notice = 'worker: waiting — codex is not logged in; run codex login'
        self.assertEqual(self.lines.count(notice), 1)
        self.assertEqual(publisher.snapshots[-1]['health_notice'], notice)
        self.assertTrue(all(r['state'] == 'waiting' for r in publisher.snapshots[-1]['latest_pass']['rows']))
        self.probe.return_value = None
        with patch('ub_agents.assignment_run.supervise', side_effect=self.complete):
            self.assertTrue(self.loop.tick())
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.loop.coordinator.history(1)[0]['attempt'], 1)
        self.assertEqual(self.loop.coordinator.history(2)[0]['attempt'], 1)
        recovery = 'worker: runtime login check passed — claiming resumes'
        self.assertEqual(self.lines.count(recovery), 1)
        self.assertEqual(publisher.snapshots[-1]['health_notice'], recovery)

    def test_alternative_and_other_cli_claim_while_logged_out_cli_waits(self):
        claude = Runtime('claude', 'other', 'high')
        fallback = replace(self.role, name='fallback', triggers=('fallback',),
                           runtimes=(*self.role.runtimes, claude))
        other = replace(self.role, name='other', triggers=('other',), runtimes=(claude,))
        self.loop.config = config(self.root, self.role, fallback, other)
        self.github.items[3] = issue(3, labels=('other',))
        self.github.change(2, labels=frozenset({'fallback'}))
        self.probe.side_effect = lambda argv, root: 'Logged out' if argv[0] == 'codex' else None
        with patch('ub_agents.assignment_run.supervise', side_effect=self.complete):
            self.assertTrue(self.loop.tick())
            self.assertEqual(self.probe.call_count, 2)
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.probe.call_count, 3)
        self.assertEqual(self.github.comments(1), [])
        self.assertEqual(self.loop.coordinator.history(2)[0]['runtime'], claude.name)
        self.assertEqual(self.loop.coordinator.history(3)[0]['runtime'], claude.name)

    def test_all_logged_out_clis_named_once_and_shared_across_items_and_agents(self):
        role = replace(self.role, runtimes=(*self.role.runtimes,
                       Runtime('codex', 'another', 'high'), Runtime('claude', 'other', 'high')))
        self.loop.config = config(self.root, role, replace(role, name='other'))
        plans = self.loop.plans()
        self.assertEqual(len(plans), 4)
        self.assertTrue(all(p.state == 'waiting' and p.health_wait for p in plans))
        self.assertEqual(plans[0].reason,
                         'codex is not logged in; run codex login; claude is not logged in; run claude auth login')
        self.assertEqual(self.probe.call_count, 2)

    def test_pass_cache_expires_after_five_minutes_and_failure_retries_next_poll(self):
        self.probe.return_value = None
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.probe.return_value = 'Logged out'
        self.now += 299
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.assertEqual(self.probe.call_count, 1)
        self.now += 1
        self.assertTrue(all(p.state == 'waiting' for p in self.loop.plans()))
        self.assertEqual(self.probe.call_count, 2)
        self.probe.return_value = None
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.assertEqual(self.probe.call_count, 3)

    def test_only_ready_runtime_work_probes(self):
        self.github.items.pop(2)
        co = self.loop.coordinator
        lease = co.claim(co.plan(self.github.item(1), self.role, ()))
        co.update(lease, state='running', started=True)
        self.assertEqual(self.loop.plans()[0].state, 'owned')
        co.release(lease, 'retry', 'Transient', backoff=60)
        self.assertEqual(self.loop.plans()[0].state, 'backoff')
        self.now += 61
        self.github.change(1, labels=frozenset({'ready', 'needs-human'}))
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.github.timelines[1] = []
        self.github.change(1, labels=frozenset({'ready'}))
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.github.items.clear()
        self.github.store.clear()
        self.assertEqual(self.loop.plans(), [])
        self.probe.assert_not_called()

    def test_command_usage_pause_and_failed_project_health_do_not_probe(self):
        self.loop.config = config(self.root, agent(self.root))
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.loop.config = config(self.root, self.role)
        self.loop.usage.limit('codex')
        self.assertTrue(all(p.state == 'waiting' and not p.health_wait for p in self.loop.plans()))
        self.loop.usage.reset()
        self.loop.config = config(self.root, replace(self.role, health_check=('health',)))
        with patch('ub_agents.agent_health.run_check', return_value='Unhealthy'):
            self.assertTrue(all(p.state == 'waiting' for p in self.loop.plans()))
        self.probe.assert_not_called()

    def test_expired_recorded_outcome_recovers_without_probe(self):
        self.github.items.pop(2)
        co = self.loop.coordinator
        lease = co.claim(co.plan(self.github.item(1), self.role, ()))
        co.update(lease, state='running', started=True)
        co.report(lease, 'success', 'Already finished', outcome='done')
        self.now += 61
        with patch('ub_agents.assignment_run.supervise', side_effect=AssertionError('recovery only')):
            self.assertTrue(self.loop.tick())
        self.probe.assert_not_called()
        self.assertTrue(co.history(1)[1]['accepted'])

    def test_cache_expiry_during_refresh_holds_before_first_write(self):
        self.github.items.pop(2)
        self.probe.side_effect = [None, 'Logged out']
        self.loop._refresh_policy = Mock(side_effect=self.advance_refresh)
        self.assertFalse(self.loop.tick())
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.github.writes, [])

    def test_login_hold_suppresses_old_blocked_notice_on_new_pr_head(self):
        role = replace(self.role, kind='pr')
        github = FakeGitHub(pr(labels=('ready',), body=''))
        loop = Loop(config(self.root, role), github, 'operator', output=self.lines.append)
        co = loop.coordinator
        co.runtime_available = lambda cli: True
        lease = co.claim(co.plan(github.item(2), role, ()))
        co.update(lease, state='running', started=True)
        co.report(lease, 'blocked', 'Need a decision', action='Maintainer: choose the approach.')
        co.release(lease, 'blocked', 'Need a decision')
        github.change(2, head='b' * 40)
        github.writes.clear()
        with patch.object(loop.maintenance, 'available', return_value=True):
            self.assertFalse(loop.tick())
        self.assertEqual(github.writes, [])

    def advance_refresh(self, *args):
        self.now += 300
        return '', {'worker': ''}, ''

    def test_background_planner_shares_cache_without_announcing(self):
        self.probe.return_value = None
        self.loop.plans()
        planner = RunPlanning(self.loop, self.now)
        self.assertIs(planner.planner.runtime_auth, self.loop.runtime_auth)
        list(planner.planner.iter_plans())
        self.assertEqual(self.probe.call_count, 1)
        self.now += 300
        self.probe.return_value = 'Logged out'
        self.assertTrue(all(p.health_wait for p in planner.planner.iter_plans()))
        self.assertEqual(self.lines, [])
        self.loop.tick()
        self.assertEqual(sum('worker: waiting' in line for line in self.lines), 1)

    def test_status_plain_and_json_show_hold_without_a_launcher(self):
        for scoped in ([], ['1']):
            with self.subTest(scoped=scoped):
                stdout = io.StringIO()
                with patch('ub_agents.cli._runtime.load_config', return_value=self.loop.config), \
                        patch('ub_agents.cli._runtime.GitHub', return_value=self.github), \
                        patch('ub_agents.updates.runtime_updates.RuntimeMaintenance.available', return_value=True), \
                        redirect_stdout(stdout):
                    self.assertEqual(main(['status', *scoped, '--json']), 0)
                    rows = json.loads(stdout.getvalue())['assignments']
                    self.assertEqual(len(rows), 1 if scoped else 2)
                    self.assertTrue(all(r['state'] == 'waiting' and r['attempts'] == 0 for r in rows))
                    stdout.seek(0)
                    stdout.truncate()
                    self.assertEqual(main(['status', *scoped]), 0)
                self.assertIn('#1 worker: waiting', stdout.getvalue())
                self.assertIn('codex is not logged in; run codex login', stdout.getvalue())
        self.assertEqual(self.github.writes, [])

    def test_launch_writes_the_login_notice_to_its_log(self):
        path = self.root / 'ub-agents.yaml'
        path.touch()
        stdout = io.StringIO()
        with patch('ub_agents.cli._runtime.load_config', return_value=self.loop.config), \
                patch('ub_agents.cli._runtime.GitHub', return_value=self.github), \
                patch('ub_agents.updates.runtime_updates.RuntimeMaintenance.available', return_value=True), \
                patch('ub_agents.cli.launch.repository_checks', return_value=[]), \
                patch('ub_agents.cleanup.Cleaner.inventory', return_value=[]), \
                patch('ub_agents.cli.launch.launch_checks'), redirect_stdout(stdout):
            self.assertEqual(main(['--config', str(path), 'launch', '--once', '--no-ui']), 0)
        notice = 'worker: waiting — codex is not logged in; run codex login'
        self.assertEqual(stdout.getvalue().count(notice), 1)
        self.assertEqual((self.root / '.ub-agents/launch.log').read_text().count(notice), 1)
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.github.writes, [])


class LoginProbeTests(unittest.TestCase):
    def test_nonzero_timeout_and_start_failure_hold_with_private_output(self):
        with tempfile.TemporaryDirectory() as folder:
            for cli, command in AUTH_PROBES.items():
                for failure in ('exit', 'timeout', 'start'):
                    with self.subTest(cli=cli, failure=failure):
                        process = Mock(returncode=1)
                        process.communicate.return_value = ('private stdout', 'private stderr')
                        if failure == 'timeout':
                            process.communicate.side_effect = subprocess.TimeoutExpired(command, 60)
                        with patch('ub_agents.agent_health.subprocess.Popen', return_value=process,
                                   side_effect=OSError('private error') if failure == 'start' else None) as start, \
                                patch('ub_agents.agent_health.stop_group'):
                            reason = RuntimeAuth().check(Path(folder), cli)
                        self.assertEqual(reason, f'{cli} is not logged in; run ' +
                                         ('codex login' if cli == 'codex' else 'claude auth login'))
                        self.assertEqual(start.call_args.args[0], command)
