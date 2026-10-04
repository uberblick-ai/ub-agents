"""Actual owned 110x32 PTY acceptance, separate from Textual headless pilots."""

import fcntl
import importlib.util
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
import time
import unittest

from tests.test_view_data import fixture

HAS_UI = importlib.util.find_spec('textual') is not None


@unittest.skipUnless(HAS_UI, 'install the opt-in UI extra')
class TerminalViewTests(unittest.TestCase):
    def test_real_terminal_explicit_load_cache_and_quit_during_hung_request(self):
        for quit_key in (b'q', b'\x03'):
            with self.subTest(quit_key=quit_key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, log, state = fixture(root, count=100)
                (log.parent / 'context.json').unlink()
                calls, release, mode = root / 'calls.jsonl', root / 'release', root / 'mode'
                mode.write_text('success')
                shim = root / 'gh'
                shim.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while (root / 'mode').read_text() == 'hang' or not (root / 'release').exists():
    time.sleep(0.02)
print('HTTP/2.0 200 OK\\nX-Ratelimit-Remaining: 500\\n')
print(json.dumps({'data': {'repository': {'issueOrPullRequest': {
    'title': 'Terminal loaded title', 'body': 'Terminal loaded body'}}}}))
''')
                shim.chmod(0o700)
                replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream:
        stream.write(b'owned replay output\\n')
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                master, slave = pty.openpty()
                app = None
                transcript = bytearray()
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    initial_modes = termios.tcgetattr(slave)
                    env = dict(os.environ, TERM='xterm-256color', PATH=str(root) + os.pathsep + os.environ['PATH'])
                    env.pop('NO_COLOR', None)
                    app = subprocess.Popen([sys.executable, '-m', 'ub_agents.view', str(root)],
                                           stdin=slave, stdout=slave, stderr=slave,
                                           start_new_session=True, env=env)
                    def drain(seconds=0.3):
                        deadline = time.monotonic() + seconds
                        chunk = bytearray()
                        while time.monotonic() < deadline:
                            if select.select([master], [], [], min(0.05, max(0, deadline - time.monotonic())))[0]:
                                data = os.read(master, 65536)
                                transcript.extend(data)
                                chunk.extend(data)
                        return bytes(chunk)
                    def recorded():
                        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
                    drain(1)
                    os.write(master, b'2')
                    self.assertIn(b'Press g on Issue', drain())
                    self.assertEqual(recorded(), [])
                    os.write(master, b'g')
                    self.assertIn(b'Loading title/body', drain())
                    self.assertEqual(len(recorded()), 1)
                    os.write(master, b'g3g1g2g')
                    drain()
                    self.assertEqual(len(recorded()), 1)
                    release.touch()
                    loaded = drain(0.8)
                    self.assertIn(b'Terminal loaded body', loaded)
                    self.assertIn(b'Source: GitHub', loaded)
                    os.write(master, b'3g1g2g')
                    drain()
                    self.assertEqual(len(recorded()), 1)
                    # A new selected item has no local or in-memory description.
                    state['assignment']['item'] = 116
                    path.write_text(json.dumps(state))
                    self.assertIn(b'Press g on Issue', drain(0.5))
                    mode.write_text('hang')
                    os.write(master, b'g')
                    self.assertIn(b'Loading title/body', drain())
                    self.assertEqual(len(recorded()), 2)
                    owned_pid = recorded()[-1]['pid']
                    os.kill(owned_pid, 0)
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
                    os.kill(app.pid, signal.SIGWINCH)
                    os.write(master, b'g1f2g')
                    drain()
                    self.assertEqual(len(recorded()), 2)
                    before = log.stat().st_size
                    started = time.monotonic()
                    os.write(master, quit_key)
                    while app.poll() is None and time.monotonic() - started < 2:
                        drain(0.05)
                    self.assertEqual(app.wait(timeout=0.5), 0, bytes(transcript[-2000:]))
                    self.assertLess(time.monotonic() - started, 2)
                    drain(0.05)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned_pid, 0)
                    self.assertEqual(termios.tcgetattr(slave), initial_modes)
                    self.assertIn(b'\x1b[?1049l', transcript)
                    self.assertIn(b'\x1b[?25h', transcript)
                    self.assertIsNone(replay.poll())
                    self.assertGreater(log.stat().st_size, before)
                finally:
                    if app and app.poll() is None:
                        app.terminate()
                        app.wait(timeout=5)
                    # Clean only processes recorded by this owned acceptance check,
                    # even if an assertion exposed a request cleanup regression.
                    for record in recorded():
                        try:
                            os.kill(record['pid'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    os.close(master)
                    os.close(slave)
                    replay.communicate(b'stop\n', timeout=5)
                    self.assertEqual(replay.returncode, 0)

    def test_real_terminal_controls_restoration_and_replay_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, _ = fixture(root, count=900)
            # This owned process stands in for a launcher publishing live output.
            replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
index = 900
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream:
        stream.write((f'replay output {index} ' + 'x' * 500 + '\\n').encode())
    index += 1
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for quit_key in (b'q', b'\x03', b'x'):
                    master, slave = pty.openpty()
                    app = None
                    transcript = bytearray()
                    try:
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                        initial_modes = termios.tcgetattr(slave)
                        env = dict(os.environ, TERM='xterm-256color')
                        env.pop('NO_COLOR', None)
                        command = [sys.executable, '-m', 'ub_agents.view', str(root), '--session', 'launcher']
                        if quit_key == b'x':
                            command = [sys.executable, '-c', '''
from pathlib import Path
import sys
from textual.binding import Binding
from ub_agents.view_ui import LogPane, View
class BrokenView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'break_render', priority=True)]
    def action_break_render(self):
        def broken(y):
            raise RuntimeError('Intentional rendering failure')
        pane = self.query_one(LogPane)
        pane.render_line = broken
        pane.refresh()
app = BrokenView(Path(sys.argv[1]), Path(sys.argv[2]))
app.run()
sys.exit(app.return_code or 1)
''', str(root), str(path)]
                        app = subprocess.Popen(command,
                                               stdin=slave, stdout=slave, stderr=slave,
                                               start_new_session=True, env=env)
                        def drain(seconds=0.3):
                            deadline = time.monotonic() + seconds
                            chunk = bytearray()
                            while time.monotonic() < deadline:
                                if select.select([master], [], [], min(0.05, max(0, deadline - time.monotonic())))[0]:
                                    try:
                                        data = os.read(master, 65536)
                                    except OSError:
                                        break
                                    transcript.extend(data)
                                    chunk.extend(data)
                            return bytes(chunk)
                        drain(1.2)
                        self.assertIsNone(app.poll(), bytes(transcript[-1000:]))
                        self.assertIn(b'FOLLOW', transcript)
                        self.assertIn(b'FORMATTED', transcript)
                        self.assertIn(b'Latest pass', transcript)
                        self.assertIn(b'\x1b[?1049h', transcript)
                        os.write(master, b'f')
                        paused = drain()
                        self.assertIn(b'PAUSED', paused)
                        os.write(master, b'\x1b[5~')  # Page Up
                        drain()
                        os.write(master, b'h')
                        older = drain(0.5)
                        self.assertTrue(b'Older page' in older or b'Page byte boundary' in older,
                                        'Older page or split-record boundary not displayed')
                        os.write(master, b'u')
                        self.assertIn(b'RAW', drain())
                        os.write(master, b'2')
                        self.assertIn(b'Cached description', drain())
                        os.write(master, b'3')
                        self.assertIn(b'needs-human', drain())
                        os.write(master, b'1p')
                        self.assertIn(b'process.log', drain())
                        os.write(master, b'\x1b')
                        drain()
                        os.write(master, b'f')
                        self.assertIn(b'FOLLOW', drain())
                        before_size = log.stat().st_size
                        os.write(master, quit_key)
                        # Keep draining until exit. A rich crash traceback can fill
                        # a small CI PTY buffer and block if wait() stops reading.
                        deadline = time.monotonic() + 5
                        while app.poll() is None and time.monotonic() < deadline:
                            drain(0.05)
                        self.assertEqual(app.wait(timeout=1), 1 if quit_key == b'x' else 0, bytes(transcript[-2000:]))
                        drain(0.05)
                        if quit_key == b'x':
                            self.assertIn(b'Intentional rendering failure', transcript)
                        self.assertIn(b'\x1b[?1049l', transcript)
                        self.assertIn(b'\x1b[?25h', transcript)
                        self.assertEqual(termios.tcgetattr(slave), initial_modes)
                        self.assertIsNone(replay.poll(), 'View stopped replay process')
                        drain(0.05)
                        self.assertGreater(log.stat().st_size, before_size)
                    finally:
                        if app and app.poll() is None:
                            app.terminate()
                            app.wait(timeout=5)
                        os.close(master)
                        os.close(slave)
            finally:
                replay.communicate(b'stop\n', timeout=5)
                self.assertEqual(replay.returncode, 0)


if __name__ == '__main__':
    unittest.main()
