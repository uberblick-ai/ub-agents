from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ub_agents.agent_health import run_check
from ub_agents.cli import main, status_rows
from ub_agents.config import Runtime
from ub_agents.errors import GitHubError
from ub_agents.execution import group_members
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import timestamp
from ub_agents.run_planning import RunPlanning
from ub_agents.view_data import plan_group
from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue, pr, stub_refresh


class HealthCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_failure_uses_last_nonempty_stderr_then_stdout_then_exit_code(self):
        for script, error in (
                ("print('ignored'); import sys; sys.stderr.write('first\\n last error \\n\\n'); sys.exit(1)",
                 'last error'),
                ("import sys; sys.stderr.write(' \\n'); print('first\\n last stdout \\n'); sys.exit(2)",
                 'last stdout'),
                ('raise SystemExit(3)', 'Exited 3')):
            with self.subTest(error=error):
                self.assertEqual(run_check((sys.executable, '-c', script), self.root), error)

    def test_check_runs_argv_without_shell_in_control_checkout_and_pass_ignores_output(self):
        command = (sys.executable, '-c', "import os,sys; print(os.getcwd()); print(sys.argv[1]); sys.exit(1)",
                   'literal argument; exit 0')
        self.assertEqual(run_check(command, self.root), 'literal argument; exit 0')
        self.assertEqual(run_check((sys.executable, '-c', 'import os; print(os.getcwd()); exit(1)'), self.root),
                         str(self.root.resolve()))
        self.assertIsNone(run_check((sys.executable, '-c', "import sys; sys.stderr.write('diagnostic')"), self.root))

    def test_start_failure_is_a_failed_check(self):
        self.assertIn('No such file', run_check((str(self.root / 'missing'),), self.root))

    def test_timeout_has_fixed_bound_and_stops_process_group(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(['probe'], 60)
        with patch('ub_agents.agent_health.subprocess.Popen', return_value=process) as start, \
                patch('ub_agents.agent_health.stop_group') as stop:
            self.assertEqual(run_check(('probe',), self.root), 'Timed out after 60 seconds')
        process.communicate.assert_called_once_with(timeout=60)
        stop.assert_called_once_with(process)
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertEqual(start.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', start.call_args.kwargs)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_timeout_cleans_helpers_holding_output_pipes(self):
        script = ("import os,subprocess,sys; from pathlib import Path; "
                  "Path('group').write_text(str(os.getpid())); "
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])")
        with patch('ub_agents.agent_health.TIMEOUT_SECONDS', 3):
            self.assertEqual(run_check((sys.executable, '-c', script), self.root), 'Timed out after 3 seconds')
        self.assertEqual(group_members(int((self.root / 'group').read_text())), [])


class HealthGateTests(unittest.TestCase):
    def setUp(self):
        self.refresh = stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = agent(self.root, kind='issue', health_check=('/check-corpus', '--cheap'))
        self.github = FakeGitHub(issue(1), issue(2))
        self.lines = []
        self.loop = Loop(config(self.root, self.agent), self.github, 'operator', output=self.lines.append)
        self.now = timestamp()
        self.loop.coordinator.clock = lambda: self.now
        self.loop.health.clock = lambda: self.now
        self.check = self.enterContext(patch('ub_agents.agent_health.run_check', return_value='Corpus unavailable'))

    def complete(self, *args, **kwargs):
        lease = self.loop.github.lease
        self.loop.coordinator.report(lease, 'success', 'Work completed', outcome='done')
        return 0

    def test_failures_recheck_each_poll_without_claims_attempts_or_per_item_notices(self):
        for _ in range(3):
            self.assertFalse(self.loop.tick())
        self.assertEqual(self.check.call_count, 3)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.lines, ['worker: waiting — health check /check-corpus --cheap: Corpus unavailable'])
        self.check.return_value = 'Wrong workspace'
        self.loop.tick()
        self.loop.tick()
        self.assertEqual(self.lines[-1], 'worker: waiting — health check /check-corpus --cheap: Wrong workspace')
        self.assertEqual(len(self.lines), 2)
        rows = status_rows(self.loop)
        self.assertEqual([(r['state'], r['attempts']) for r in rows], [('waiting', 0), ('waiting', 0)])
        self.assertTrue(all('Wrong workspace' in r['reason'] for r in rows))
        self.assertEqual(self.github.writes, [])

    def test_other_agents_claim_in_same_poll_and_recovery_resumes_without_reset(self):
        other = agent(self.root, name='other', kind='issue', triggers=('other',))
        self.loop.config = config(self.root, self.agent, other)
        self.github.change(2, labels=frozenset({'other'}))
        with patch('ub_agents.loop.supervise', side_effect=self.complete):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.github.comments(1), [])
        self.assertEqual(self.github.item(1).labels, frozenset({'ready'}))
        self.assertEqual(self.loop.coordinator.history(2)[0]['agent'], 'other')
        self.check.return_value = None
        with patch('ub_agents.loop.supervise', side_effect=self.complete):
            self.assertTrue(self.loop.tick())
        history = self.loop.coordinator.history(1)
        self.assertEqual(history[0]['attempt'], 1)
        self.assertEqual(history[0]['result'], 'success')
        self.assertFalse(any(r['kind'] == 'reset' for r in history))
        self.assertEqual(self.lines.count('worker: health check /check-corpus --cheap passed — claiming resumes'), 1)

    def test_pass_cached_for_five_minutes_then_failure_rechecked(self):
        self.check.return_value = None
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.check.return_value = 'Corpus unavailable'
        self.now += 299
        self.assertTrue(all(p.state == 'ready' for p in self.loop.plans()))
        self.assertEqual(self.check.call_count, 1)
        self.now += 1
        self.assertTrue(all(p.state == 'waiting' for p in self.loop.plans()))
        self.assertEqual(self.check.call_count, 2)
        self.assertTrue(all(p.state == 'waiting' for p in self.loop.plans()))
        self.assertEqual(self.check.call_count, 3)

    def test_same_command_is_cached_separately_for_each_agent_and_config_change_invalidates_it(self):
        other = replace(self.agent, name='other', triggers=('other',))
        self.loop.config = config(self.root, self.agent, other)
        self.github.change(2, labels=frozenset({'other'}))
        self.check.side_effect = [None, 'Unavailable', 'New check failed']
        self.assertEqual([p.state for p in self.loop.plans()], ['ready', 'waiting'])
        self.assertEqual(self.check.call_count, 2)
        self.loop.config = config(self.root, replace(self.agent, health_check=('/different-check',)))
        self.assertEqual(self.loop.plans()[0].state, 'waiting')
        self.check.assert_called_with(('/different-check',), self.root)

    def test_check_is_not_run_for_empty_owned_parked_backoff_or_unapproved_work(self):
        co = self.loop.coordinator
        lease = co.claim(co.plan(self.github.item(1), self.agent, ()))
        co.update(lease, state='running', started=True)
        self.github.change(2, labels=frozenset({'ready', 'needs-human'}))
        self.assertEqual([p.state for p in self.loop.plans()], ['owned', 'parked'])
        co.release(lease, 'retry', 'Transient', backoff=60)
        self.assertEqual(self.loop.plans()[0].state, 'backoff')
        self.github.change(1, labels=frozenset())
        self.loop.plans()
        self.github.items.clear()
        self.github.store.clear()
        self.assertEqual(self.loop.plans(), [])
        self.github.items[3] = issue(3)
        self.github.timelines[3] = []
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.check.assert_not_called()

    def test_runtime_usage_wait_does_not_probe_health(self):
        runtime_agent = replace(self.agent, command=(), runtimes=(Runtime('codex', 'model', 'high'),))
        self.loop.config = config(self.root, runtime_agent)
        self.loop.usage.limit('codex')
        with patch.object(self.loop.maintenance, 'available', return_value=True):
            self.assertTrue(all(p.state == 'waiting' and not p.health_wait for p in self.loop.plans()))
        self.check.assert_not_called()

    def test_expired_recorded_outcome_recovers_without_health_check_or_execution(self):
        self.github.items.pop(2)
        co = self.loop.coordinator
        lease = co.claim(co.plan(self.github.item(1), self.agent, ()))
        co.update(lease, state='running', started=True)
        co.report(lease, 'success', 'Already finished', outcome='done')
        self.now += 61
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('recovery only')):
            self.assertTrue(self.loop.tick())
        self.check.assert_not_called()
        self.assertTrue(co.history(1)[1]['accepted'])
        recovery = next(r for r in co.history(1) if r.get('mode') == 'recovery')
        self.assertEqual(recovery['result'], 'success')

    def test_failed_check_suppresses_old_blocked_notice_on_newly_ready_pr_head(self):
        role = replace(self.agent, kind='pr')
        github = FakeGitHub(pr(labels=('ready',), body=''))
        loop = Loop(config(self.root, role), github, 'operator', output=self.lines.append)
        co = loop.coordinator
        lease = co.claim(co.plan(github.item(2), role, ()))
        co.update(lease, state='running', started=True)
        co.report(lease, 'blocked', 'Need a decision', action='Maintainer: choose the next step.')
        with patch.object(co.notices, 'post_action', side_effect=GitHubError('POST', 'notice', 'Unavailable')):
            co.release(lease, 'blocked', 'Need a decision')
        github.change(2, head='b' * 40)
        writes = list(github.writes)
        self.assertFalse(loop.tick())
        self.assertEqual(github.writes, writes)
        self.check.assert_called_once()
        self.assertEqual(loop.plans()[0].state, 'waiting')

    def test_stale_ready_plan_is_gated_before_refresh_and_any_assignment_write(self):
        plan = self.loop.coordinator.plan(self.github.item(1), self.agent, ())
        with patch('ub_agents.loop.supervise') as execute:
            self.assertFalse(self.loop.execute(plan))
        self.refresh.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_cache_expiry_during_refresh_rechecks_before_claim_and_publishes_waiting(self):
        self.github.items.pop(2)
        self.check.side_effect = [None, 'Corpus unavailable']
        def refresh(*args, **kwargs):
            self.now += 300
            return ''
        self.refresh.side_effect = refresh
        publisher = MemoryPublisher()
        self.loop.observer = Observations(self.loop.config, 'operator', None, publisher)
        self.assertFalse(self.loop.tick())
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(self.github.writes, [])
        row = publisher.snapshots[-1]['latest_pass']['rows'][0]
        self.assertEqual(row['state'], 'waiting')
        self.assertIn('Corpus unavailable', row['reason'])

    def test_refreshed_configuration_checks_new_health_command_before_claim(self):
        self.check.side_effect = [None, 'Wrong workspace']
        current = self.loop.config
        updated = config(self.root, replace(self.agent, health_check=('/new-check',)))
        self.loop.config_path = self.root / 'ub-agents.yaml'
        with patch('ub_agents.loop.refresh_checkout'), patch('ub_agents.loop.load_config', return_value=updated):
            self.assertFalse(self.loop.execute(self.loop.plans()[0]))
        self.assertNotEqual(self.loop.config, current)
        self.check.assert_called_with(('/new-check',), self.root)
        self.assertEqual(self.github.writes, [])
        self.assertIn('health check /new-check: Wrong workspace', self.lines[-1])

    def test_background_planner_shares_pass_cache_but_does_not_emit_health_notices(self):
        self.check.return_value = None
        self.loop.plans()
        planner = RunPlanning(self.loop, self.now)
        self.assertIs(planner.planner.health, self.loop.health)
        list(planner.planner.iter_plans())
        self.assertEqual(self.check.call_count, 1)
        self.now += 300
        self.check.return_value = 'Unavailable'
        self.assertTrue(all(p.health_wait for p in planner.planner.iter_plans()))
        self.assertEqual(self.lines, [])
        self.loop.tick()
        self.assertEqual(len(self.lines), 1)

    def test_waiting_rows_in_terminal_view_stay_out_of_needs_attention(self):
        publisher = MemoryPublisher()
        self.loop.observer = Observations(self.loop.config, 'operator', None, publisher)
        self.loop.tick()
        rows = publisher.snapshots[-1]['latest_pass']['rows']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['state'], 'waiting')
            self.assertEqual(plan_group(row), 'Eligible')
            self.assertIn('/check-corpus --cheap: Corpus unavailable', row['reason'])
            self.assertEqual(row['failures'], 0)

    def test_status_checks_health_without_launcher_and_keeps_json_and_plain_output_valid(self):
        for scoped in ([], ['1']):
            with self.subTest(scoped=scoped):
                stdout = io.StringIO()
                with patch('ub_agents.cli.load_config', return_value=self.loop.config), \
                        patch('ub_agents.cli.GitHub', return_value=self.github), redirect_stdout(stdout):
                    self.assertEqual(main(['status', *scoped, '--json']), 0)
                    rows = json.loads(stdout.getvalue())['assignments']
                    self.assertEqual(len(rows), 1 if scoped else 2)
                    self.assertTrue(all(r['state'] == 'waiting' and r['attempts'] == 0 for r in rows))
                    stdout.seek(0)
                    stdout.truncate()
                    self.assertEqual(main(['status', *scoped]), 0)
                    self.assertIn('#1 worker: waiting', stdout.getvalue())
                    self.assertIn('/check-corpus --cheap: Corpus unavailable', stdout.getvalue())
        self.assertEqual(self.check.call_count, 4)
        self.assertEqual(self.github.writes, [])

    def test_launch_once_exits_normally_and_scoped_launch_refuses_with_one_line_and_launch_log(self):
        path = self.root / 'ub-agents.yaml'
        path.touch()
        for scoped in ([], ['1']):
            with self.subTest(scoped=scoped):
                stdout = io.StringIO()
                with patch('ub_agents.cli.load_config', return_value=self.loop.config), \
                        patch('ub_agents.cli.GitHub', return_value=self.github), \
                        patch('ub_agents.cli.repository_checks', return_value=[]), \
                        patch('ub_agents.cli.launch_checks'), redirect_stdout(stdout):
                    self.assertEqual(main(['--config', str(path), 'launch', *scoped, '--once', '--no-ui']),
                                     1 if scoped else 0)
                self.assertEqual(stdout.getvalue(),
                                 'worker: waiting — health check /check-corpus --cheap: Corpus unavailable\n')
        lines = (self.root / '.ub-agents/launch.log').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith(stdout.getvalue().strip()) for line in lines))
        self.assertEqual(self.github.writes, [])
