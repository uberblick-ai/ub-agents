from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import signal
import threading
import unittest
from unittest.mock import Mock, patch

from ub_agents.cli import main
from ub_agents.errors import AgentError, CheckoutRefreshError
from ub_agents.execution import git
from ub_agents.launch_log import launch_output
from ub_agents.launcher_lock import launcher_lock, running_launcher
from ub_agents.observations import Observations
from ub_agents.records import attempts, timestamp
from tests.support import MemoryPublisher
from tests import test_refresh


class ConfigurationPauseTests(unittest.TestCase):
    commit = test_refresh.RefreshTests.commit
    push_policy = test_refresh.RefreshTests.push_policy

    def setUp(self):
        test_refresh.RefreshTests.setUp(self)
        test_refresh.RefreshTests.enable_reload(self)
        self.loop.interrupt_event = threading.Event()
        self.lines = []
        self.loop.output = self.lines.append
        self.publisher = MemoryPublisher()
        self.loop.observer = Observations(self.loop.config, 'operator', self.loop.config_path, self.publisher)
        self.valid = (self.upstream / 'ub-agents.yaml').read_text()

    def publish(self, content):
        (self.upstream / 'ub-agents.yaml').write_text(content)
        self.commit(self.upstream)
        git(self.upstream, 'push', 'origin', 'main')
        return git(self.upstream, 'rev-parse', 'HEAD')

    def assert_no_claims(self):
        self.assertEqual(self.github.writes, [])
        self.assertEqual(attempts(self.loop.coordinator.history(1), 'worker', timestamp()), [])

    def launch(self, wait, run=None, agent_name=None):
        with patch('ub_agents.coordination.shutil.which', return_value='installed'), \
                patch.object(self.loop, 'wait', side_effect=wait) as waits, \
                patch('ub_agents.loop.supervise', side_effect=run) as execution:
            self.loop.launch(agent_name=agent_name)
        return waits, execution

    def test_unknown_key_pauses_empty_polls_log_only_changes_and_later_commit_resumes(self):
        first = self.publish(self.valid + 'future-key: true\n')
        commits = [first]
        polls = []

        def wait(event, delay, reason):
            polls.append(reason)
            self.assert_no_claims()
            pause = self.loop.configuration_pause
            self.assertEqual(pause['commit'], commits[-1])
            self.assertIn('Unknown configuration keys:', pause['error'])
            self.assertIn('Correct', pause['next_step'])
            self.assertIn('upgrade ub-agents', pause['next_step'])
            self.assertEqual(self.loop.observer.state['configuration_pause'], pause)
            if len(polls) == 1:
                self.github.change(1, labels=frozenset())
                self.github.change(3, labels=frozenset())
            elif len(polls) == 2:
                self.push_policy('Unrelated change while still invalid')
                commits.append(git(self.upstream, 'rev-parse', 'HEAD'))
            elif len(polls) == 3:
                commits.append(self.publish(self.valid + 'another-key: true\n'))
            elif len(polls) == 4:
                self.publish(self.valid + 'poll-seconds: 17\n')
                self.github.change(1, labels=frozenset({'ready'}))
            else:
                self.fail('Launcher did not resume on the next poll')

        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            self.assertIsNone(self.loop.configuration_pause)
            self.assertIsNone(self.loop.observer.state['configuration_pause'])
            self.assertEqual(self.loop.config.poll_seconds, 17)
            self.assertNotIn('configuration_pause', running_launcher(self.root))
            self.assertIn('Unrelated change while still invalid', prompt)
            self.loop.coordinator.report(self.loop.coordinator.history(1)[0], 'success', 'Done', outcome='done')
            self.loop.stop_event.set()
            return 0

        def output(line):
            self.lines.append(line)
            print(line)

        self.loop.output = output
        with launcher_lock(self.root), redirect_stdout(io.StringIO()) as terminal, launch_output(self.root):
            waits, execution = self.launch(wait, run, agent_name='worker')
        self.assertEqual(waits.call_count, 4)
        execution.assert_called_once()
        pauses = [line for line in self.lines if line.startswith('Claims paused at')]
        self.assertEqual(len(pauses), 3)
        for line, commit in zip(pauses, commits):
            self.assertIn(commit, line)
        log = (self.root / '.ub-agents/launch.log').read_text()
        self.assertEqual(log.count('Claims paused at'), 3)
        self.assertEqual(log.count('resuming claims'), 1)
        self.assertIn('upgrade ub-agents', terminal.getvalue())
        self.assertTrue(self.loop.coordinator.outcome(self.loop.coordinator.history(1)[0])['accepted'])

    def test_local_configuration_and_unreadable_instructions_pause_without_upgrade_advice(self):
        for problem in ('local', 'role', 'shared', 'missing-role', 'permissions'):
            with self.subTest(problem=problem):
                self.setUp()
                if problem == 'local':
                    (self.root / 'ub-agents.local.yaml').write_text('poll-seconds: zero\n')
                elif problem == 'shared':
                    (self.upstream / 'shared.md').write_bytes(b'\xff')
                    self.publish(self.valid + 'shared-instructions: shared.md\n')
                elif problem == 'missing-role':
                    self.publish(self.valid.replace('role.md', 'moved.md'))
                elif problem == 'permissions':
                    self.push_policy()
                else:
                    (self.upstream / 'role.md').write_bytes(b'\xff')
                    self.commit(self.upstream)
                    git(self.upstream, 'push', 'origin', 'main')
                original = Path.read_text

                def read(path, *args, **kwargs):
                    if problem == 'permissions' and path == self.root / 'role.md':
                        raise PermissionError('Synthetic unreadable instruction file')
                    return original(path, *args, **kwargs)

                def wait(event, delay, reason):
                    self.assert_no_claims()
                    pause = self.loop.configuration_pause
                    self.assertEqual(pause['commit'], git(self.root, 'rev-parse', 'HEAD'))
                    expected = 'ub-agents.local.yaml' if problem == 'local' else 'instruction'
                    self.assertIn(expected, pause['error'])
                    self.assertNotIn('upgrade', pause['next_step'])
                    event.set()

                with patch.object(Path, 'read_text', read):
                    _, execution = self.launch(wait)
                execution.assert_not_called()

    def test_unknown_values_offer_file_correction_and_upgrade(self):
        for setting in ('queue: {milestones: future}', 'approvals: future',
                        'runtime-updates: {codex: future}', 'runtime-updates: {ub-agents: future}'):
            with self.subTest(setting=setting):
                self.setUp()
                self.publish(self.valid + setting + '\n')

                def wait(event, delay, reason):
                    self.assert_no_claims()
                    self.assertIn('upgrade ub-agents', self.loop.configuration_pause['next_step'])
                    event.set()

                self.launch(wait)

    def test_r_rereads_local_fix_at_the_same_commit_before_empty_queue_discovery(self):
        local = self.root / 'ub-agents.local.yaml'
        local.write_text('poll-seconds: invalid\n')
        self.loop.enable_poll_now()
        wait = self.loop.poll_now.wait
        wakes = []

        def poll_wait(stop, delay, update=None, on_request=None):
            wake = threading.Event()
            wakes.append(wake)

            def wake_wait(interval):
                self.assert_no_claims()
                if len(wakes) == 1:
                    self.assertIsNotNone(self.loop.configuration_pause)
                    local.write_text('poll-seconds: 17\n')
                    self.github.change(1, labels=frozenset())
                    self.github.change(3, labels=frozenset())
                    self.loop.request_poll()
                    self.assertTrue(wake.is_set())
                else:
                    self.assertIsNone(self.loop.configuration_pause)
                    self.assertEqual(self.loop.config.poll_seconds, 17)
                    stop.set()
                return wake.is_set()

            with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                    patch.object(wake, 'wait', side_effect=wake_wait):
                return wait(stop, delay, update, on_request)

        with patch('ub_agents.coordination.shutil.which', return_value='installed'), \
                patch.object(self.loop.poll_now, 'wait', side_effect=poll_wait):
            self.loop.launch()
        self.assertEqual(len(wakes), 2)
        self.assertEqual(sum('resuming claims' in line for line in self.lines), 1)

    def test_paused_launcher_never_discovers_or_recovers_work(self):
        self.publish(self.valid + 'unknown: true\n')
        polls = []

        def wait(event, delay, reason):
            polls.append(reason)
            self.assert_no_claims()
            if len(polls) == 1:
                self.loop.tick = Mock(side_effect=AssertionError('No discovery while paused'))
                self.loop.recover = Mock(side_effect=AssertionError('No recoveries while paused'))
            else:
                event.set()

        self.launch(wait)
        self.assertEqual(len(polls), 2)

    def test_status_text_and_json_work_with_invalid_files_without_loading_or_github(self):
        self.publish(self.valid + 'unknown: true\n')

        def wait(event, delay, reason):
            with patch('ub_agents.cli.load_config', side_effect=AssertionError('Cannot load invalid files')), \
                    patch('ub_agents.cli.GitHub', side_effect=AssertionError('No GitHub discovery')):
                for number in ([], ['1']):
                    argv = ['--config', str(self.loop.config_path), 'status', *number]
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(argv + ['--json']), 0)
                    value = json.loads(output.getvalue())
                    self.assertEqual(value['assignments'], [])
                    self.assertEqual(value['launcher']['configuration_pause'], self.loop.configuration_pause)
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(argv), 0)
                    for field in self.loop.configuration_pause.values():
                        self.assertIn(field, output.getvalue())
            event.set()

        with launcher_lock(self.root):
            self.launch(wait)
        self.assertIsNone(running_launcher(self.root))

    def test_status_reports_pause_when_refresh_removed_the_configuration_file(self):
        (self.upstream / 'ub-agents.yaml').unlink()
        self.commit(self.upstream)
        git(self.upstream, 'push', 'origin', 'main')

        def wait(event, delay, reason):
            self.assertFalse(self.loop.config_path.exists())
            self.assert_no_claims()
            argv = ['--config', str(self.loop.config_path), 'status']
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(argv + ['--json']), 0)
            self.assertEqual(json.loads(output.getvalue())['launcher']['configuration_pause'],
                             self.loop.configuration_pause)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(argv), 0)
            self.assertIn('Cannot read configuration', output.getvalue())
            self.assertIn('Claims paused at', output.getvalue())
            event.set()

        with launcher_lock(self.root):
            self.launch(wait)

    def test_q_drain_and_sigterm_stop_a_paused_launcher_without_another_poll(self):
        self.publish(self.valid + 'unknown: true\n')

        def wait(event, delay, reason):
            self.assertIsNotNone(self.loop.configuration_pause)
            self.assert_no_claims()
            # The terminal's q sends drain, which is relayed as SIGTERM.
            signal.raise_signal(signal.SIGTERM)

        with patch('ub_agents.cli.Loop', return_value=self.loop), \
                patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('ub_agents.cli.launch_checks'), \
                patch('ub_agents.cli.repository_checks', return_value=[]), \
                patch('ub_agents.cli.StartupCleanup'), \
                patch('ub_agents.cli._start_updates'), \
                patch('ub_agents.coordination.shutil.which', return_value='installed'), \
                patch.object(self.loop, 'wait', side_effect=wait) as waits, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(self.loop.config_path), 'launch', '--no-ui']), 0)
        waits.assert_called_once()

    def test_git_or_checkout_failure_while_paused_still_stops(self):
        self.publish(self.valid + 'unknown: true\n')

        def wait(event, delay, reason):
            self.assert_no_claims()
            (self.root / 'untracked').write_text('Unsafe checkout')

        with self.assertRaises(AgentError) as failure:
            self.launch(wait)
        self.assertIsInstance(failure.exception.__cause__, CheckoutRefreshError)
        self.assertIn('local changes', str(failure.exception))
        self.assert_no_claims()

    def test_numbered_and_once_launch_still_fail_on_invalid_reread(self):
        for options in (['--once'], ['1']):
            with self.subTest(options=options):
                self.setUp()
                self.publish(self.valid + 'unknown: true\n')
                with patch('ub_agents.coordination.shutil.which', return_value='installed'), \
                        self.assertRaisesRegex(AgentError, 'Unknown configuration keys: unknown'):
                    self.loop.launch(once='--once' in options, number=1 if '1' in options else None)
                self.assertIsNone(self.loop.configuration_pause)
                self.assert_no_claims()
