from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Runtime
from ub_agents.doctor.doctor import Doctor
from ub_agents.execution import command_for, group_members, supervise
from ub_agents.loop import Loop
from ub_agents.monitor.local import runtime_blocks
from ub_agents.notices import ACTION_MARKER
from ub_agents.observations import Observations
from ub_agents.records import attempts, iso
from ub_agents.runtime_rejections import block_reason, rejected_setting
from ub_agents.usage.runtime_usage import RuntimeUsage
from ub_agents.usage.usage_output import UsageOutput
from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue, stub_refresh


REJECTIONS = (
    ('claude', 1, "error: unknown option '--removed'", '--removed'),
    ('claude', 1, "error: option '--model <model>' argument 'gone' is invalid.", '--model <model>: gone'),
    ('codex', 2, "error: unexpected argument '--removed' found", '--removed'),
    ('codex', 2, "error: invalid value 'gone' for '--model <MODEL>'", '--model <MODEL>: gone'),
    ('codex', 1, 'Error loading config.toml: unknown configuration field `removed` in -c/--config override', 'removed'),
)


class RuntimeRejectionTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1_800_000_000
        self.version = '1.0'
        self.lines = []
        self.enterContext(patch('ub_agents.coordination.shutil.which', return_value='installed'))

    def loop(self, cli='claude', alternatives=True):
        runtimes = (Runtime(cli, 'first', 'high'),)
        if alternatives:
            runtimes += (Runtime(cli, 'second', 'high'),)
        role = agent(self.root, command=(), kind='issue', runtimes=runtimes,
                     runtime_args=('--removed', '{scratch}', '{report_command}'),
                     backoff_seconds=60, max_backoff_seconds=3600, max_attempts=1)
        other = replace(role, name='other', triggers=('other',))
        cfg = config(self.root, role, other)
        memory = MemoryPublisher()
        observer = Observations(cfg, 'operator', self.root / 'ub-agents.yaml', memory)
        loop = Loop(cfg, FakeGitHub(issue(), issue(2, labels=('other',))), 'operator',
                    output=self.lines.append, observer=observer)
        loop.coordinator.clock = lambda: self.now
        self.enterContext(patch.object(loop.maintenance, 'version', side_effect=lambda *args: self.version))
        return loop, role, memory

    def process(self, code, stderr, stdout=''):
        def run(command, cwd, env, run_dir, *args, **kwargs):
            self.assertTrue(kwargs['separate_output'])
            (run_dir / 'stdout.log').write_text(stdout)
            (run_dir / 'stderr.log').write_text(stderr)
            (run_dir / 'process.log').write_text(stdout + stderr)
            kwargs['observe_output'](final=True)
            return code
        return run

    def test_each_rejection_preserves_attempt_labels_and_notices_and_uses_same_cli_alternative(self):
        for cli, code, diagnostic, setting in REJECTIONS:
            with self.subTest(diagnostic=diagnostic):
                loop, role, memory = self.loop(cli)
                with patch('ub_agents.assignment_run.supervise', side_effect=self.process(code, diagnostic + '\n')):
                    self.assertTrue(loop.tick())
                history = loop.coordinator.history(1)
                lease = history[0]
                self.assertEqual((lease['result'], lease['attempt_effect'], lease.get('retry_after')),
                                 ('retry', 'unchanged', None))
                self.assertEqual(attempts(history, role.name, self.now), [])
                self.assertIn(setting, lease['summary'])
                self.assertEqual(loop.github.item(1).labels, frozenset({'ready'}))
                self.assertFalse(any(comment['body'].startswith(ACTION_MARKER) for comment in loop.github.comments(1)))
                self.assertEqual(loop.usage.pauses, {})
                blocks = memory.snapshots[-1]['runtime_blocks']
                self.assertEqual([(b['agent'], b['runtime'], b['setting']) for b in blocks],
                                 [(role.name, role.runtimes[0].name, setting)])
                plans = loop.plans()
                self.assertEqual([(p.state, p.runtime) for p in plans],
                                 [('ready', role.runtimes[1]), ('ready', role.runtimes[0])])
                with patch('ub_agents.assignment_run.supervise', side_effect=self.process(1, 'ordinary failure')) as run:
                    self.assertTrue(loop.tick())
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index('--model') + 1], 'second')
                self.assertEqual(len(attempts(loop.coordinator.history(1), role.name, self.now)), 1)

    def test_all_blocked_wait_without_timer_attempt_limit_or_new_writes(self):
        loop, role, _ = self.loop()
        with patch('ub_agents.assignment_run.supervise', side_effect=self.process(1, REJECTIONS[0][2])):
            self.assertTrue(loop.tick_item(1))
        with patch('ub_agents.assignment_run.supervise', side_effect=self.process(1, "error: unknown option '--second'")):
            self.assertTrue(loop.tick_item(1))
        writes = list(loop.github.writes)
        self.now += 100_000
        with patch('ub_agents.assignment_run.supervise') as run:
            self.assertFalse(loop.tick_item(1))
        run.assert_not_called()
        self.assertEqual(loop.github.writes, writes)
        plan = loop.plans()[0]
        self.assertEqual((plan.state, plan.attempt), ('waiting', 1))
        for text in (role.name, role.runtimes[0].name, role.runtimes[1].name, '--removed', '--second', 'restart this launcher'):
            self.assertIn(text, plan.reason)

    def test_command_and_version_changes_clear_blocks_but_substitutions_and_same_version_do_not(self):
        loop, role, memory = self.loop(alternatives=False)
        with patch('ub_agents.assignment_run.supervise', side_effect=self.process(1, REJECTIONS[0][2])):
            loop.tick_item(1)
        runtime = role.runtimes[0]
        self.assertNotEqual(command_for(role, runtime, '/first', 'first report'),
                            command_for(role, runtime, '/second', 'second report'))
        self.assertIsNotNone(loop.runtime_blocks.blocked(role, runtime))
        loop.maintain_runtimes()
        self.assertIsNotNone(loop.runtime_blocks.blocked(role, runtime))
        self.version = '2.0'
        loop.maintain_runtimes()
        self.assertIsNone(loop.runtime_blocks.blocked(role, runtime))
        self.assertEqual(memory.snapshots[-1]['runtime_blocks'], [])
        for changed in (replace(role, runtime_args=('--new',)),
                        replace(role, runtimes=(replace(runtime, model='new-model'),)),
                        replace(role, runtimes=(replace(runtime, effort='low'),))):
            with self.subTest(changed=changed):
                loop.runtime_blocks.reject(role, runtime, '--removed', self.version)
                loop.config = config(self.root, changed)
                loop.maintain_runtimes()
                self.assertEqual(loop.runtime_blocks.blocks, {})
        loop.config = config(self.root, role)
        loop.runtime_blocks.reject(role, runtime, '--removed', self.version)
        with patch('ub_agents.runtime_rejections.command_template', return_value=['claude', '--new-fixed-flag']):
            self.assertIsNone(loop.runtime_blocks.blocked(role, runtime))
        loop.runtime_blocks.reject(role, runtime, '--removed', self.version)
        with patch.object(loop, 'tick', side_effect=lambda: self.assertEqual(loop.runtime_blocks.blocks, {}) or False):
            loop.launch(once=True)

    def test_unrecognised_wrong_stream_wrong_code_and_json_output_are_ordinary_failures(self):
        cases = [('claude', 1, 'unrecognised startup failure', ''),
                 ('claude', 2, REJECTIONS[0][2], ''),
                 ('claude', 1, '', REJECTIONS[0][2]),
                 ('claude', 1, 'prefix\n' + REJECTIONS[0][2], ''),
                 ('claude', 1, 'Warning: Unknown --effort value', ''),
                 ('claude', 1, REJECTIONS[0][2], '{}')]
        cases += [(cli, code, diagnostic, '{"type":"started"}\n') for cli, code, diagnostic, _ in REJECTIONS]
        for cli, code, stderr, stdout in cases:
            with self.subTest(code=code, stderr=stderr, stdout=stdout):
                loop, role, _ = self.loop(cli, alternatives=False)
                with patch('ub_agents.assignment_run.supervise', side_effect=self.process(code, stderr, stdout)):
                    loop.tick_item(1)
                lease = loop.coordinator.history(1)[0]
                self.assertEqual((lease['result'], lease['attempt_effect']), ('retry', 'failure'))
                self.assertEqual(len(attempts(loop.coordinator.history(1), role.name, self.now)), 1)
                self.assertEqual(loop.runtime_blocks.blocks, {})

    def test_configuration_reload_drops_changed_or_removed_alternatives(self):
        loop, role, _ = self.loop(alternatives=False)
        path = self.root / 'ub-agents.yaml'
        loop.config_path = path
        loop.default_config = False
        (self.root / 'instructions.md').write_text('Do the task')
        document = '''repository: org/project
agents:
  worker:
    runtime: claude:first:high
    runtime-args: [--new]
    instructions: instructions.md
    kind: issue
    trigger: ready
    outcomes: {done: {}}
'''
        path.write_text(document)
        loop.runtime_blocks.reject(role, role.runtimes[0], '--removed', self.version)
        loop._reload_configuration()
        self.assertEqual(loop.runtime_blocks.blocks, {})
        current = loop.config.agents[0]
        loop.runtime_blocks.reject(current, current.runtimes[0], '--new', self.version)
        loop._reload_configuration()
        self.assertIsNotNone(loop.runtime_blocks.blocked(current, current.runtimes[0]))
        path.write_text(document.replace('claude:first:high', 'codex:second:high'))
        loop._reload_configuration()
        self.assertEqual(loop.runtime_blocks.blocks, {})

    def test_rejection_does_not_write_shared_health_or_block_another_launcher(self):
        loop, role, _ = self.loop(alternatives=False)
        with patch.object(loop.maintenance, 'write', side_effect=AssertionError('shared state mutation')), \
                patch('ub_agents.assignment_run.supervise', side_effect=self.process(1, REJECTIONS[0][2])):
            self.assertTrue(loop.tick_item(1))
        self.assertEqual(loop.plans()[0].state, 'waiting')
        other = Loop(loop.config, FakeGitHub(issue()), 'operator', output=self.lines.append)
        self.assertEqual(other.plans()[0].state, 'ready')
        self.assertTrue(other.maintenance.available(role.runtimes[0].cli))

    def test_independence_still_limits_eligible_alternatives(self):
        loop, role, _ = self.loop()
        independent = replace(role, different_from=('author',))
        loop.config = config(self.root, independent)
        loop.runtime_blocks.reject(independent, independent.runtimes[0], '--removed', self.version)
        plan = loop.plans()[0]
        self.assertEqual(plan.state, 'waiting')
        self.assertNotIn(independent.runtimes[1].name, plan.reason)

    def test_real_process_captures_streams_keeps_combined_log_and_checks_every_diagnostic(self):
        for cli, code, diagnostic, setting in REJECTIONS:
            with self.subTest(cli=cli, diagnostic=diagnostic):
                run_dir = self.root / 'run'
                script = f'import sys; print({diagnostic!r}, file=sys.stderr); sys.exit({code})'
                result = supervise([sys.executable, '-c', script], self.root, os.environ.copy(), run_dir,
                                   5, threading.Event(), separate_output=True)
                self.assertEqual(result, code)
                self.assertEqual(rejected_setting(cli, result, run_dir), setting)
                self.assertEqual((run_dir / 'process.log').read_text(), diagnostic + '\n')
                script = 'print(\'{"type":"started"}\', flush=True); ' + script
                result = supervise([sys.executable, '-c', script], self.root, os.environ.copy(), run_dir,
                                   5, threading.Event(), separate_output=True)
                self.assertIsNone(rejected_setting(cli, result, run_dir))

    def test_separate_streams_keep_live_usage_observation_and_interrupt_cleanup(self):
        run_dir = self.root / 'live'
        stop = threading.Event()
        usage = RuntimeUsage(clock=lambda: self.now, output=self.lines.append)
        output = UsageOutput('claude', run_dir, usage, {})
        event = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected',
                                                                         'resetsAt': self.now + 300}})
        script = (f'import sys, time; print({event!r}, flush=True); '
                  'print("runtime warning", file=sys.stderr, flush=True); time.sleep(20)')

        def observe(final=False):
            output.poll(final)
            if output.reached:
                stop.set()

        with self.assertRaises(KeyboardInterrupt):
            supervise([sys.executable, '-c', script], self.root, os.environ.copy(), run_dir,
                      5, stop, separate_output=True, observe_output=observe)
        self.assertTrue(output.reached)
        self.assertIn('runtime warning', (run_dir / 'process.log').read_text())
        self.assertEqual(group_members(int((run_dir / 'pid').read_text())), [])

    def test_local_snapshot_status_and_doctor_show_blocks_even_without_queue_items(self):
        loop, role, memory = self.loop(alternatives=False)
        loop.runtime_blocks.reject(role, role.runtimes[0], '--removed', self.version)
        blocks = memory.snapshots[-1]['runtime_blocks']
        directory = self.root / '.ub-agents/sessions'
        directory.mkdir(parents=True, exist_ok=True)
        state = memory.snapshots[-1] | {'pid': 123, 'host': 'test', 'published_at': iso(self.now)}
        path = directory / 'local.json'
        path.write_text(json.dumps(state))
        owner = {'pid': 123, 'host': 'test'}
        with patch('ub_agents.monitor.local.running_launcher', return_value=owner):
            self.assertEqual(runtime_blocks(loop.config, self.now), blocks)
            for changes in ({'ended': True}, {'pid': 456}, {'host': 'other'}, {'published_at': iso(self.now - 31)}):
                path.write_text(json.dumps(state | changes))
                self.assertEqual(runtime_blocks(loop.config, self.now), [])
            path.write_text(json.dumps(state))
            for github in (FakeGitHub(), FakeGitHub(issue())):
                with patch('ub_agents.cli._runtime.load_config', return_value=loop.config), \
                        patch('ub_agents.cli._runtime.GitHub', return_value=github), \
                        patch('ub_agents.cli.status.timestamp', return_value=self.now):
                    for json_output in (False, True):
                        with redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(main(['status'] + (['--json'] if json_output else [])), 0)
                        if json_output:
                            result = json.loads(output.getvalue())
                            self.assertEqual(result['runtime_blocks'], blocks)
                            if result['assignments']:
                                self.assertEqual(result['assignments'][0]['state'], 'waiting')
                        else:
                            self.assertIn(block_reason(blocks[0]), output.getvalue())
            doctor = Doctor()
            with patch('ub_agents.doctor.doctor.timestamp', return_value=self.now):
                doctor._runtimes(loop.config, offline=True)
            warnings = [check for check in doctor.checks if check.id.startswith('runtime-command-block:')]
            self.assertEqual([(check.status, check.agent, check.runtime) for check in warnings],
                             [('warn', role.name, role.runtimes[0].name)])
            self.assertIn('--removed', warnings[0].message)
            self.assertIn('restart this launcher', warnings[0].remedy)
        with patch('ub_agents.monitor.local.running_launcher', return_value=None):
            self.assertEqual(runtime_blocks(loop.config, self.now), [])


class RuntimeBlockViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_shows_every_block_with_full_setting_and_recovery_then_clears(self):
        from tests.test_view_data import fixture, publish_snapshot
        from tests.view_pilot import RefreshPilot
        from ub_agents.view.ui import View
        from ub_agents.view.widgets import RuntimeBlockNotice
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _, state = fixture(root)
            blocks = [{'agent': 'worker', 'runtime': 'claude:first:high', 'setting': '--removed',
                       'remedy': 'Change runtime-args or restart this launcher'},
                      {'agent': 'reviewer', 'runtime': 'codex:second:high', 'setting': 'gone',
                       'remedy': 'Install a different CLI version or restart this launcher'}]
            state['runtime_blocks'] = blocks
            publish_snapshot(path, state)
            app = View(root, path)
            async with app.run_test(size=(110, 32)):
                pilot = RefreshPilot(app)
                await pilot.pause()
                notice = app.query_one(RuntimeBlockNotice)
                self.assertTrue(notice.display)
                self.assertEqual(str(notice.render()), '\n'.join(block_reason(block) for block in blocks))
                notice.set_blocks([])
                await pilot.pause()
                self.assertFalse(notice.display)
