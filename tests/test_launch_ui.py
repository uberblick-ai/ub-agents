"""Launcher selection, exact attachment and owned real-terminal lifecycle checks."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ub_agents import __version__
from ub_agents.launch_log import launch_output
from ub_agents.launch_ui import ViewProcess, open_view
from ub_agents.view import attach
from tests.test_view_data import fixture

HAS_UI = importlib.util.find_spec('textual') is not None


class Tty(io.StringIO):
    def isatty(self):
        return True


class LaunchSelectionTests(unittest.TestCase):
    def test_terminal_startup_failure_keeps_plain_output(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = Tty()
            with redirect_stdout(terminal), patch('sys.stdin', Tty()), launch_output(Path(directory)) as output, \
                    patch('ub_agents.launch_ui.ui_command', return_value=['ub-agents-ui']), \
                    patch('ub_agents.launch_ui.subprocess.run', return_value=Mock(returncode=0)), \
                    patch('ub_agents.launch_ui.ViewProcess.start', side_effect=termios.error('terminal unavailable')):
                self.assertIsNone(open_view(Path(directory), 'own', output, Mock()))
                print('plain launch continues')
            self.assertEqual(terminal.getvalue().splitlines(),
                             ['Terminal view unavailable: terminal unavailable; continuing with plain output',
                              'plain launch continues'])

    def test_plain_selection_never_discovers_or_imports_ui(self):
        for no_ui, stdin_tty, stdout_tty in ((True, True, True), (False, False, True), (False, True, False)):
            with self.subTest(no_ui=no_ui, stdin=stdin_tty, stdout=stdout_tty), tempfile.TemporaryDirectory() as directory:
                terminal = Tty() if stdout_tty else io.StringIO()
                stdin = Tty() if stdin_tty else io.StringIO()
                with redirect_stdout(terminal), patch('sys.stdin', stdin), launch_output(Path(directory)) as output, \
                        patch('ub_agents.launch_ui.ui_command', side_effect=AssertionError('UI discovery')):
                    self.assertIsNone(open_view(Path(directory), 'own', output, Mock(), no_ui=no_ui))
                self.assertEqual(terminal.getvalue(), '')

    def test_missing_notice_once_and_matching_external_ui_probe(self):
        for command, probe in ((None, None), (['ub-agents-ui'], Mock(returncode=3)),
                               (['ub-agents-ui'], Mock(returncode=2, stdout='UI/base version mismatch')),
                               (['ub-agents-ui'], Mock(returncode=0))):
            with self.subTest(command=command, probe=probe), tempfile.TemporaryDirectory() as directory:
                terminal = Tty()
                with redirect_stdout(terminal), patch('sys.stdin', Tty()), launch_output(Path(directory)) as output, \
                        patch('ub_agents.launch_ui.ui_command', return_value=command), \
                        patch('ub_agents.launch_ui.subprocess.run', return_value=probe), \
                        patch('ub_agents.launch_ui.ViewProcess') as view:
                    result = open_view(Path(directory), 'own-session', output, Mock())
                    if probe and probe.returncode == 0:
                        self.assertEqual(result, view.return_value)
                        self.assertEqual(view.call_args.args[2], 'own-session')
                        view.return_value.start.assert_called_once()
                    else:
                        self.assertIsNone(result)
                        view.assert_not_called()
                        self.assertEqual(len(terminal.getvalue().splitlines()), 1)
                if not probe or probe.returncode == 3:
                    self.assertIn('brew install uberblick-ai/tap/ub-agents-ui', terminal.getvalue())

    def test_strict_own_session_errors_never_choose_another_live_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, state = fixture(root)
            state['base_version'] = __version__
            path.write_text(json.dumps(state))
            self.assertEqual(attach(root, 'launcher', __version__, wait=0), path)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                attach(root, 'missing', __version__, wait=0)
            for changes, message in (({'version': 42}, 'snapshot version'),
                                     ({'base_version': 'older'}, 'version mismatch'),
                                     ({'session': 'other'}, 'identity mismatch'),
                                     ({'ended': True}, 'ended'),
                                     ({'published_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}, 'stale')):
                path.write_text(json.dumps(state | changes))
                with self.assertRaisesRegex(ValueError, message):
                    attach(root, 'launcher', __version__, wait=0)
            with self.assertRaisesRegex(ValueError, 'UI/base version mismatch'):
                attach(root, 'launcher', 'older', wait=0)

    def test_hidden_output_log_is_unchanged_and_final_line_is_visible_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root, terminal = Path(directory), io.StringIO()
            with redirect_stdout(terminal), launch_output(root) as output:
                print('before')
                output.hide()
                print('hidden')
                print('final')
                output.resume(final=True)
            self.assertEqual(terminal.getvalue(), 'before\nfinal\n')
            self.assertEqual([line.split(' ', 1)[1] for line in (root / '.ub-agents/launch.log').read_text().splitlines()],
                             ['before', 'hidden', 'final'])

    def test_child_attachment_errors_restore_terminal_and_continue_plain(self):
        for change, expected in (({'version': 42}, 'snapshot version'),
                                 ({'base_version': 'older'}, 'version mismatch'),
                                 ({'published_at': '2020-01-01T00:00:00Z'}, 'stale'),
                                 (None, 'unavailable')):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, _, state = fixture(root)
                state['base_version'] = __version__
                if change is None:
                    path.unlink()
                else:
                    path.write_text(json.dumps(state | change))
                master, slave = pty.openpty()
                initial = termios.tcgetattr(slave)
                stdin = os.fdopen(os.dup(slave), 'r')
                terminal = os.fdopen(os.dup(slave), 'w')
                view = None
                try:
                    with patch('sys.stdin', stdin), redirect_stdout(terminal), launch_output(root) as output:
                        view = ViewProcess([sys.executable, '-P', '-m', 'ub_agents.view'], root, 'launcher', output)
                        view.start(threading.Event())
                        self.assertIn(expected, view.error)
                        view.close()
                        print('plain continues')
                    transcript = bytearray()
                    while select.select([master], [], [], 0.02)[0]:
                        transcript.extend(os.read(master, 65536))
                    self.assertEqual(bytes(transcript).count(b'Terminal view closed:'), 1)
                    self.assertIn(b'plain continues', transcript)
                    self.assertEqual(termios.tcgetattr(slave), initial)
                    self.assertEqual(view.process.returncode, 1)
                finally:
                    if view is not None:
                        view.close()
                    stdin.close()
                    terminal.close()
                    os.close(master)
                    os.close(slave)


HARNESS = '''
from dataclasses import replace
import json, os, pathlib, signal, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from tests.support import FakeGitHub, agent, config, issue
from ub_agents.cli import main
from ub_agents.config import Runtime
from ub_agents.execution import supervise
from ub_agents.loop import Loop
root = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
github = FakeGitHub(issue(116))
cfg = config(root, agent(root, kind='issue', command=(), runtimes=(Runtime('claude', 'synthetic', 'high'),)))
# One real supervised owned agent replays the sanitized captured Claude fixture.
source = pathlib.Path(sys.argv[1]) / 'tests/fixtures/runtime_logs/claude.log'
loop = None
def create(*args, **kwargs):
    global loop
    loop = Loop(*args, **kwargs)
    return loop
def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
    script = """import pathlib, sys, time
root, source = map(pathlib.Path, sys.argv[1:])
for line in source.read_text().splitlines():
    print(line, flush=True)
while not (root / 'finish').exists():
    print('{"type":"assistant","message":{"content":[{"type":"text","text":"Replay active"}]}}', flush=True)
    time.sleep(0.03)
"""
    code = supervise([sys.executable, '-c', script, str(root), str(source)], cwd, env, run_dir, 30, stop, prompt, **kwargs)
    lease = next(r for r in reversed(loop.coordinator.history(116)) if r['kind'] == 'lease')
    if code == 0:
        loop.coordinator.report(lease, 'success', 'Captured replay finished', outcome='done')
    return code
with patch('ub_agents.cli.load_config', return_value=cfg), patch('ub_agents.loop.load_config', return_value=cfg), \\
     patch('ub_agents.cli.GitHub', return_value=github), patch('ub_agents.cli.Loop', side_effect=create), \\
     patch('ub_agents.cli.repository_checks', return_value=[]), patch('ub_agents.loop.refresh_checkout'), \\
     patch('ub_agents.loop.supervise', side_effect=run):
    result = main(['--config', str(root / 'ub-agents.yaml'), 'launch', *sys.argv[4:]])
(root / 'result.json').write_text(json.dumps({'exit': result, 'history': loop.coordinator.history(116)}))
sys.exit(result)
'''


@unittest.skipUnless(HAS_UI, 'install the opt-in UI extra')
class LaunchTerminalTests(unittest.TestCase):
    def test_launch_forms_q_crash_interrupt_drain_exit_and_restart(self):
        # The clean-wheel interpreter is also used for the recorded acceptance run.
        python = os.environ.get('UB_UI_TEST_PYTHON', sys.executable)
        for mode, arguments in (('q', []), ('crash', ['--once']), ('interrupt', ['116']),
                                ('drain', []), ('once', ['--once']), ('hup', []), ('no-ui', ['--no-ui', '--once'])):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shim = root / 'claude'
                shim.write_text('#!/bin/sh\nexit 0\n')
                shim.chmod(0o700)
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                modes = termios.tcgetattr(slave)
                env = dict(os.environ, TERM='xterm-256color', XDG_STATE_HOME=str(root / 'state'),
                           PATH=str(root) + os.pathsep + os.environ['PATH'])
                env.pop('NO_COLOR', None)
                process = subprocess.Popen([python, '-P', '-c', HARNESS, str(Path(__file__).resolve().parents[1]),
                                            str(root), mode, *arguments], stdin=slave, stdout=slave, stderr=slave,
                                           start_new_session=True, env=env)
                transcript = bytearray()
                view_pid = None
                agent_pid = None
                def drain(seconds=0.1):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.02)[0]:
                            transcript.extend(os.read(master, 65536))
                def until(predicate, timeout=8):
                    deadline = time.monotonic() + timeout
                    while not predicate() and time.monotonic() < deadline:
                        drain()
                    self.assertTrue(predicate(), bytes(transcript[-3000:]))
                try:
                    until(lambda: b'Logs:' in transcript if mode == 'no-ui' else b'Replay active' in transcript)
                    # The launcher passes its own session even when a second fresh
                    # local session exists; another session never appears in its view.
                    until(lambda: bool(list((root / '.ub-agents/sessions').glob('*.json'))))
                    paths = list((root / '.ub-agents/sessions').glob('*.json'))
                    self.assertEqual(len(paths), 1)
                    first_session = paths[0].stem
                    foreign = json.loads(paths[0].read_text()) | {'session': 'foreign', 'assignment': None}
                    (paths[0].parent / 'foreign.json').write_text(json.dumps(foreign))
                    pids = list((root / '.ub-agents/runs').glob('*/pid'))
                    until(lambda: bool(pids or list((root / '.ub-agents/runs').glob('*/pid'))))
                    agent_pid = int(list((root / '.ub-agents/runs').glob('*/pid'))[0].read_text())
                    if mode != 'no-ui':
                        table = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,command='], text=True)
                        view_pid = next(int(line.split()[0]) for line in table.splitlines()
                                        if len(line.split()) > 2 and line.split()[1] == str(process.pid)
                                        and ('ub_agents.view ' in line or 'ub-agents-ui ' in line))
                        self.assertIn(b'--session', table.encode())
                        self.assertIn(first_session, next(line for line in table.splitlines() if line.split()[0] == str(view_pid)))
                    if mode == 'q':
                        os.write(master, b'f')
                        until(lambda: b'PAUSED' in transcript)
                        os.write(master, b'\x1b[5~h')
                        drain(0.2)
                        os.write(master, b'u')
                        until(lambda: b'RAW' in transcript)
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 25, 100, 0, 0))
                        os.kill(process.pid, signal.SIGWINCH)
                        until(lambda: b'minimum 110' in transcript)
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                        os.kill(process.pid, signal.SIGWINCH)
                        drain(0.15)
                        os.write(master, b'2')
                        until(lambda: b'Acceptance criteria' in transcript)
                        os.write(master, b'3')
                        drain()
                        os.write(master, b'1p')
                        until(lambda: b'process.log' in transcript)
                        os.write(master, b'\x1b')
                        drain(0.15)
                        os.write(master, b'q')
                        until(lambda: b'\x1b[?1049l' in transcript)
                        self.assertIsNone(process.poll())
                        os.kill(agent_pid, 0)
                        os.kill(process.pid, signal.SIGTERM)
                        (root / 'finish').touch()
                    elif mode == 'crash':
                        os.kill(view_pid, signal.SIGKILL)
                        until(lambda: b'Terminal view closed:' in transcript)
                        self.assertIsNone(process.poll())
                        self.assertEqual(termios.tcgetattr(slave), modes)
                        (root / 'finish').touch()
                    elif mode == 'interrupt':
                        os.write(master, b'\x03')
                    elif mode == 'hup':
                        os.kill(process.pid, signal.SIGHUP)
                    elif mode == 'drain':
                        os.kill(process.pid, signal.SIGTERM)
                        drain(0.2)
                        self.assertIsNone(process.poll())
                        os.kill(agent_pid, 0)
                        (root / 'finish').touch()
                    else:
                        (root / 'finish').touch()
                    until(lambda: process.poll() is not None)
                    expected = 130 if mode in {'interrupt', 'hup'} else 0
                    self.assertEqual(process.wait(), expected)
                    drain()
                    self.assertEqual(termios.tcgetattr(slave), modes)
                    if mode != 'no-ui':
                        self.assertIn(b'\x1b[?1049l', transcript)
                        self.assertIn(b'\x1b[?25h', transcript)
                    else:
                        self.assertNotIn(b'\x1b[?1049h', transcript)
                    self.assertIn(b'Stopped; supervised execution terminated' if expected else b'Captured replay finished', transcript)
                    history = json.loads((root / 'result.json').read_text())['history']
                    self.assertEqual(history[0]['state'], 'released')
                    for pid in (view_pid, agent_pid):
                        if pid is not None:
                            with self.assertRaises(ProcessLookupError):
                                os.kill(pid, 0)
                    self.assertFalse(list((root / '.ub-agents/runs').glob('*/scratch')))
                    if mode == 'once':
                        # Restart the same control root with two old snapshots;
                        # the new launcher still attaches to its own new ID.
                        (root / 'finish').unlink()
                        process = subprocess.Popen([python, '-P', '-c', HARNESS,
                                                    str(Path(__file__).resolve().parents[1]), str(root), mode, '--once'],
                                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
                        transcript.clear()
                        until(lambda: b'Replay active' in transcript)
                        self.assertEqual(len(list((root / '.ub-agents/sessions').glob('*.json'))), 3)
                        (root / 'finish').touch()
                        until(lambda: process.poll() is not None)
                        self.assertEqual(process.wait(), 0)
                        drain()
                        self.assertEqual(termios.tcgetattr(slave), modes)
                finally:
                    if process.poll() is None:
                        os.kill(process.pid, signal.SIGINT)
                        (root / 'finish').touch()
                        until(lambda: process.poll() is not None)
                        process.wait()
                    os.close(master)
                    os.close(slave)
