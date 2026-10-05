"""Actual owned 110x32 PTY acceptance, separate from Textual headless pilots."""

import fcntl
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import pty
import re
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

class TerminalViewTests(unittest.TestCase):
    def test_update_banners_in_real_terminal_with_live_replay_and_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=100)
            replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream: stream.write(b'owned update replay\\n')
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            proof = root / 'proof.json'
            script = '''
import json, pathlib, sys
from textual.binding import Binding
from textual.widgets import TabbedContent
from ub_agents.view_ui import LogPane, UpdateBanner, View
class ProofView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'checkpoint', priority=True)]
    def action_checkpoint(self):
        self.call_after_refresh(lambda: self.query_one(LogPane).call_after_refresh(self.checkpoint))
    def checkpoint(self):
        banner = self.query_one(UpdateBanner)
        header = self.query_one('#item_header')
        output = self.query_one(LogPane)
        footer = self.query_one('#status')
        run_status = self.query_one('#run_status')
        value = {'display': banner.display, 'height': banner.size.height,
                 'width': banner.content_size.width, 'line': banner.render().plain,
                 'text': banner.banner.get('text'), 'terminal_width': self.size.width,
                 'cells': banner.render().cell_len, 'yellow': str(banner.styles.background),
                 'banner_y': banner.region.y, 'body_y': self.query_one('#body').region.y,
                 'work_y': self.query_one('#work_pane').region.y,
                 'header_y': header.region.y, 'header_height': header.size.height,
                 'header': header.render().plain, 'output_y': output.region.y,
                 'footer': footer.render().plain, 'footer_height': footer.size.height,
                 'footer_y': footer.region.y,
                 'run_status_bottom': run_status.region.bottom,
                 'active': self.query_one(TabbedContent).active,
                 'selected': self.selected, 'focus': self.focused.id if self.focused else None,
                 'follow': self.reading.follow, 'anchor': output.anchor(),
                 'saved_anchor': self.reading.anchor,
                 'first_anchor': output.positions[0] if output.positions else None,
                 'starts': [r.start for r in self.reading.page.refs] if self.reading.page else []}
        proof = pathlib.Path(sys.argv[3])
        staging = proof.with_suffix('.new')
        staging.write_text(json.dumps(value))
        staging.replace(proof)
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
            modes = termios.tcgetattr(slave)
            env = dict(os.environ, TERM='xterm-256color')
            env.pop('NO_COLOR', None)
            app = subprocess.Popen([sys.executable, '-P', '-c', script, str(root), str(path), str(proof)],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
            transcript = bytearray()
            def drain(seconds=0.05):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.02)[0]:
                        transcript.extend(os.read(master, 65536))
            def checkpoint():
                proof.unlink(missing_ok=True)
                os.write(master, b'x')
                deadline = time.monotonic() + 5
                while not proof.exists() and time.monotonic() < deadline:
                    drain(0.05)
                self.assertTrue(proof.exists(), bytes(transcript[-1000:]))
                return json.loads(proof.read_text())
            def until(condition):
                # A snapshot read or resize can finish after a key is handled.
                # Wait for the rendered state instead of a fixed settling delay.
                deadline = time.monotonic() + 5
                current = checkpoint()
                while not condition(current) and time.monotonic() < deadline:
                    current = checkpoint()
                self.assertTrue(condition(current), (current, bytes(transcript[-1000:])))
                return current
            try:
                initial = until(lambda value: value['starts'] and value['focus'] is not None
                                and value['footer'].endswith('↑↓ select ⏎ open 1-3 tabs ? keys q quit'))
                window_title = 'ub-agents launch — example/repo'.encode()
                self.assertTrue(b'\x1b]0;' + window_title + b'\x07' in transcript,
                                'Terminal output is missing the window-title OSC sequence')
                self.assertNotIn(b'FOLLOW', transcript)
                self.assertFalse(initial['display'])
                os.write(master, b'f')
                until(lambda value: not value['follow'])
                os.write(master, b'\x1b[H')
                paused = until(lambda value: not value['follow'] and value['anchor'] is not None
                               and value['anchor'] == value['first_anchor'] == value['saved_anchor'])
                self.assertFalse(paused['follow'])
                from tests.test_updates import release
                from ub_agents.updates import release_banner
                cases = [release_banner(release(), 'brew'), release_banner(release(), 'pip'),
                         {'text': '⬆ This launcher runs code 2 commits behind origin/main · restart the launcher'}]
                for banner in cases:
                    if 'released_at' in banner:
                        banner['released_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                    state['update'] = banner
                    path.write_text(json.dumps(state))
                    for width in (110, 70, 170):
                        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, width, 0, 0))
                        os.kill(app.pid, signal.SIGWINCH)
                        keys = ('f follow h older u raw PgUp/PgDn scroll ? keys q quit'
                                if width >= 110 else '? keys q quit')
                        current = until(lambda value: value['text'] == banner['text']
                                        and value['display'] and value['height'] == 1
                                        and value['terminal_width'] == width
                                        and value['width'] == width - 2 and value['body_y'] == 1
                                        and value['anchor'] == paused['anchor']
                                        and value['footer'].endswith(keys))
                        self.assertTrue(current['display'])
                        self.assertEqual(current['height'], 1)
                        self.assertEqual(current['banner_y'], 0)
                        self.assertEqual(current['body_y'], 1)
                        self.assertEqual(current['work_y'], 1)
                        self.assertGreater(current['header_y'], current['banner_y'])
                        self.assertEqual(current['header_height'], 3)
                        self.assertLessEqual(current['header_y'] + current['header_height'], current['output_y'])
                        self.assertIn('#114', current['header'])
                        self.assertEqual(current['yellow'], 'Color(255, 139, 127)')
                        self.assertLessEqual(current['cells'], width - 2)
                        self.assertEqual(current['selected'], paused['selected'])
                        self.assertEqual(current['focus'], paused['focus'])
                        self.assertEqual(current['starts'], paused['starts'])
                        self.assertEqual(current['anchor'], paused['anchor'])
                        self.assertEqual(current['saved_anchor'], paused['saved_anchor'])
                        self.assertFalse(current['follow'])
                        self.assertEqual(current['footer_height'], 1)
                        self.assertEqual(current['footer_y'], 31)
                        self.assertLessEqual(current['run_status_bottom'], current['footer_y'])
                        self.assertTrue(current['footer'].endswith(keys))
                        if 'released_at' in banner:
                            self.assertTrue(current['line'].endswith('released 2 days ago'))
                            self.assertEqual(current['cells'], width - 2)
                        if width == 170:
                            self.assertIn(banner['text'], current['line'])
                self.assertIn(b'released 2 days ago', transcript)
                self.assertIn(b'restart the launcher', transcript)
                state['update'] = None
                path.write_text(json.dumps(state))
                cleared = until(lambda value: not value['display'] and value['body_y'] == 0
                                and value['anchor'] == paused['anchor'])
                self.assertFalse(cleared['display'])
                self.assertEqual(cleared['body_y'], 0)
                self.assertEqual(cleared['anchor'], paused['anchor'])
                # Repeated snapshot reads do not re-emit an unchanged title.
                self.assertEqual(transcript.count(b'\x1b]0;' + window_title + b'\x07'), 1)
                self.assertNotIn(b'\x1b]0;\x07', transcript)
                # Repository controls must remain inert inside the OSC payload.
                state['repository'] = 'other/repo\x07\x1b]0;injected\x1b\\\n'
                path.write_text(json.dumps(state))
                changed_title = ('ub-agents launch — ' + r'other/repo\x07\x1b]0;injected\x1b\\n').encode()
                until(lambda _: b'\x1b]0;' + changed_title + b'\x07' in transcript)
                self.assertNotIn(b'\x1b]0;injected', transcript)
                before = log.stat().st_size
                os.write(master, b'q')
                deadline = time.monotonic() + 5
                while app.poll() is None and time.monotonic() < deadline:
                    drain(0.05)
                self.assertEqual(app.wait(timeout=1), 0, bytes(transcript[-1000:]))
                deadline = time.monotonic() + 5
                while log.stat().st_size <= before and time.monotonic() < deadline:
                    drain()
                drain()
                self.assertEqual(termios.tcgetattr(slave), modes)
                self.assertIn(b'\x1b[?1049l', transcript)
                self.assertIn(b'\x1b[?25h', transcript)
                self.assertEqual(transcript.count(b'\x1b]0;' + changed_title + b'\x07'), 1)
                self.assertEqual(transcript.count(b'\x1b]0;\x07'), 1)
                self.assertIsNone(replay.poll())
                self.assertGreater(log.stat().st_size, before)
            finally:
                if app.poll() is None:
                    app.terminate()
                app.wait(timeout=3)
                replay.communicate(b'stop\n', timeout=3)
                self.assertEqual(replay.returncode, 0)
                os.close(master)
                os.close(slave)

    # One test per quit key, so a parallel run spreads them over cores.
    def test_real_terminal_explicit_load_cache_and_q_during_hung_request(self):
        self.check_load_cache_and_quit_during_hung_request(b'q')

    def test_real_terminal_explicit_load_cache_and_interrupt_during_hung_request(self):
        self.check_load_cache_and_quit_during_hung_request(b'\x03')

    def check_load_cache_and_quit_during_hung_request(self, quit_key):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=100)
            (log.parent / 'context.json').unlink()
            calls, release, mode = root / 'calls.jsonl', root / 'release', root / 'mode'
            mode.write_text('success')
            shim = root / 'gh'
            body = ('## Terminal Markdown\n\nTerminal loaded body\nSecond line\n\n'
                    '- **strong** and *emphasis* with `code`\n'
                    '- [bold]literal[/bold] \x1b[31m\n\n'
                    '```text\ncode\tline\n```\n\n'
                    '[web](https://example.invalid)\n'
                    '![pic](https://example.invalid/pic) <b>HTML</b>')
            shim.write_text(f'#!{sys.executable}\nbody = {body!r}\n' + '''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while (root / 'mode').read_text() == 'hang' or not (root / 'release').exists():
    time.sleep(0.02)
print('HTTP/2.0 200 OK\\nX-Ratelimit-Remaining: 500\\n')
print(json.dumps({'data': {'repository': {'issueOrPullRequest': {
    'title': 'Terminal loaded title', 'body': body}}}}))
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
                def until(*expected, timeout=5):
                    # Read until each expected output appeared (bytes) or check
                    # holds (callable taking the output), not a fixed delay.
                    chunk = bytearray()
                    deadline = time.monotonic() + timeout
                    def met():
                        return all(e(bytes(chunk)) if callable(e) else e in chunk for e in expected)
                    while not met() and time.monotonic() < deadline:
                        chunk.extend(drain(0.05))
                    self.assertTrue(met(), bytes(transcript[-2000:]))
                    return bytes(chunk)
                def recorded():
                    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
                until(b'running assignment', b'owned replay output')
                os.write(master, b'2')
                until(b'Press g on Issue')
                self.assertEqual(recorded(), [])
                os.write(master, b'g')
                until(b'Loading title/body', lambda _: len(recorded()) == 1)
                os.write(master, b'g3g1g2g')
                drain()
                self.assertEqual(len(recorded()), 1)
                release.touch()
                loaded = until(b'Terminal loaded body', b'Terminal Markdown', b'Second line',
                               b'[bold]literal[/bold]', br'\x1b[31m', b'Source: GitHub')
                self.assertNotIn(b'\x1b[31m', loaded)
                self.assertNotIn(b'**strong**', loaded)
                self.assertNotIn(b'## Terminal Markdown', loaded)
                os.write(master, b'3g1g2g')
                drain()
                self.assertEqual(len(recorded()), 1)
                state['latest_pass']['rows'][0]['description'] = {
                    'available': True, 'text': '## Snapshot Markdown\n\nSnapshot body\nSecond line\n\n- **strong**'}
                path.write_text(json.dumps(state))
                snapshot = until(b'Snapshot Markdown', b'Source: snapshot')
                self.assertNotIn(b'## Snapshot Markdown', snapshot)
                state['latest_pass']['rows'][0]['description'] = {
                    'available': True, 'text': '```text\n' + 'x' * 3000,
                    'omitted_characters': 1000}
                path.write_text(json.dumps(state))
                until(b'Description shortened')
                self.assertEqual(len(recorded()), 1)
                # A new selected item has no local or in-memory description.
                state['assignment']['item'] = 116
                path.write_text(json.dumps(state))
                until(b'Press g on Issue')
                mode.write_text('hang')
                os.write(master, b'g')
                until(b'Loading title/body', lambda _: len(recorded()) == 2)
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

    def test_real_terminal_controls_restoration_and_replay_isolation_on_q(self):
        self.check_controls_restoration_and_replay_isolation(b'q')

    def test_real_terminal_controls_restoration_and_replay_isolation_on_interrupt(self):
        self.check_controls_restoration_and_replay_isolation(b'\x03')

    def test_real_terminal_controls_restoration_and_replay_isolation_on_crash(self):
        self.check_controls_restoration_and_replay_isolation(b'x')

    def check_controls_restoration_and_replay_isolation(self, quit_key):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, _ = fixture(root, count=900)
            (log.parent / 'context.json').write_text(json.dumps({
                'title': 'Cached title [bold]literal[/bold]',
                'body': '## Cached Markdown\n\nCached description\r\nSecond line\rThird line\n\n'
                        '- **strong** and *emphasis* with `code`\n\n```text\ncode\tline\n```'}))
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
                    def until(*expected, timeout=5):
                        # Read until each expected output appeared (bytes) or check
                        # holds (callable taking the output), not a fixed delay.
                        chunk = bytearray()
                        deadline = time.monotonic() + timeout
                        def met():
                            return all(e(bytes(chunk)) if callable(e) else e in chunk for e in expected)
                        while not met() and time.monotonic() < deadline:
                            chunk.extend(drain(0.05))
                        self.assertTrue(met(), bytes(transcript[-2000:]))
                        return bytes(chunk)
                    # The page is loaded once live replay output is on screen.
                    until(b'running assignment', b'? keys q quit', b'Running', b'partial',
                          b'\x1b[?1049h', b'replay output')
                    self.assertNotIn(b'FOLLOW', transcript)
                    self.assertNotIn(b'FORMATTED', transcript)
                    self.assertIsNone(app.poll(), bytes(transcript[-1000:]))
                    os.write(master, b'f')
                    until(b'PAUSED')
                    os.write(master, b'\x1b[5~')  # Page Up
                    drain()
                    os.write(master, b'h')
                    # Older page or split-record boundary.
                    until(lambda out: b'Older page' in out or b'Page byte boundary' in out)
                    os.write(master, b'u')
                    until(b'RAW')
                    os.write(master, b'2')
                    cached = until(b'Cached description', b'Cached Markdown', b'Second line', b'Third line')
                    self.assertNotIn(b'## Cached Markdown', cached)
                    self.assertNotIn(b'**strong**', cached)
                    # Give the Runs table room to show complete history fields.
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 150, 0, 0))
                    os.kill(app.pid, signal.SIGWINCH)
                    drain()
                    os.write(master, b'3')
                    until(b'filed by bk-one', b'build-01', b'needs-human')
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    os.kill(app.pid, signal.SIGWINCH)
                    drain()
                    os.write(master, b'1p')
                    until(b'process.log', b'Displayed bytes', b'evicted', b'Rendered limit 400')
                    # A focus report right after Escape ends the escape sequence,
                    # so a busy machine cannot merge Escape and f into Alt+f.
                    os.write(master, b'\x1b\x1b[I')
                    drain()
                    os.write(master, b'f')
                    until('↑↓ select'.encode())
                    self.assertNotIn(b'FOLLOW', transcript)
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
                    self.assertTrue(b'\x1b]0;' + 'ub-agents launch — example/repo'.encode() + b'\x07' in transcript,
                                    'Terminal output is missing the window-title OSC sequence')
                    self.assertEqual(transcript.count(b'\x1b]0;\x07'), 1)
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

class TerminalRetentionTests(unittest.TestCase):
    def test_compact_claude_replay_hidden_and_failed_result_toggles_in_real_terminal(self):
        self.compact_runtime_replay('claude')

    def test_compact_codex_replay_hidden_and_failed_result_toggles_in_real_terminal(self):
        self.compact_runtime_replay('codex')

    def compact_runtime_replay(self, runtime):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, runtime=f'{runtime}:synthetic-model:high')
            tail_count = 5 if runtime == 'claude' else 40
            state['assignment'].update(kind='issue', attempt=2)
            state['outcomes'].append({'item': 114, 'run': 'earlier-run', 'handoff': 185})
            path.write_text(json.dumps(state))
            capture = Path(__file__).parent / f'fixtures/runtime_logs/{runtime}.log'
            proof = root / 'proof.json'
            script = '''
import json, pathlib, sys
from textual.binding import Binding
from ub_agents.view_ui import LogPane, View
class ProofView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'checkpoint', priority=True),
                Binding('y', 'seek_failure', priority=True)]
    def action_seek_failure(self):
        self.reading.follow = False
        self.reading.anchor = (next(ref.start for ref in self.reading.page.refs
                                   if ref.value.kind == 'tool ERROR'), 0)
        self.query_one(LogPane).reflow()
    def action_checkpoint(self):
        self.call_after_refresh(lambda: self.query_one(LogPane).call_after_refresh(self.checkpoint))
    def checkpoint(self):
        pane = self.query_one(LogPane)
        r = self.reading
        refs = r.page.refs if r.page else []
        value = {'ready': r.page is not None, 'raw': r.raw, 'anchor': pane.anchor(), 'entries': len(refs),
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'notice': self.query_one('#log_note').render().plain,
                 'raw_details': self.raw_details(),
                 'modal': self.screen.query_one('#raw_details').render().plain
                          if self.screen.query('#raw_details') else '',
                 'lines': [line.text for line in pane.lines],
                 'refs': [(ref.start, ref.value.kind) for ref in refs],
                 'positions': pane.positions}
        partial = pathlib.Path(sys.argv[3] + '.partial')
        partial.write_text(json.dumps(value))
        partial.replace(sys.argv[3])
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
            modes = termios.tcgetattr(slave)
            env = dict(os.environ, TERM='xterm-256color')
            env.pop('NO_COLOR', None)
            app = subprocess.Popen([sys.executable, '-P', '-c', script, str(root), str(path), str(proof)],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
            transcript = bytearray()
            def drain(seconds=0.2):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.02)[0]:
                        transcript.extend(os.read(master, 65536))
            def checkpoint(ready=lambda value: True):
                deadline = time.monotonic() + 5
                while True:
                    proof.unlink(missing_ok=True)
                    os.write(master, b'x')
                    written = time.monotonic() + 3
                    while not proof.exists() and time.monotonic() < written:
                        drain(0.05)
                    self.assertTrue(proof.exists(), bytes(transcript[-1000:]))
                    value = json.loads(proof.read_text())
                    if ready(value) or time.monotonic() >= deadline:
                        self.assertTrue(ready(value), value)
                        return value
            try:
                checkpoint(lambda value: value['ready'])
                # Appending after attachment replays every recorded input from
                # byte zero, preserving capture times as well as producer times.
                from tests.test_view_data import event
                with log.open('ab') as stream:
                    stream.write(capture.read_bytes())
                    if runtime == 'claude':
                        # Keep the first hidden record within the narrower raw render budget.
                        stream.write(b''.join(event(i, 20) for i in range(tail_count)))
                    else:
                        from tests.test_codex_logs import encoded
                        stream.write(b''.join(encoded({'type': 'item.completed', 'item': {
                            'id': f'after_{i}', 'type': 'agent_message', 'text': f'after {i}'}}) for i in range(tail_count)))
                formatted = checkpoint(lambda value: value['entries'] == len(capture.read_bytes().splitlines()) + tail_count)
                self.assertEqual(formatted['entries'], len(capture.read_bytes().splitlines()) + tail_count)
                text = '\n'.join(formatted['lines'])
                if runtime == 'claude':
                    self.assertIn('· thinking', text)
                    self.assertIn('▸ Read <fixture>/fixture_output.py', text)
                    self.assertIn('▸ Bash cat missing-owned.txt', text)
                    self.assertIn('✗ Exit code 1', text)
                else:
                    self.assertIn('▸ Bash /bin/zsh', text)
                    self.assertIn('▸ file add <fixture>/greeting.txt', text)
                    self.assertIn('▸ fixture.echo', text)
                    self.assertIn('✗ Exit code 7: owned failure', text)
                    self.assertTrue(any(line.startswith('~') for line in formatted['lines']))
                self.assertIn('✓ run finished', text)
                self.assertNotIn('thinking_tokens', text)
                self.assertNotIn('producer=', text)
                self.assertEqual(formatted['header'].splitlines()[:2],
                                 ['#114 Cached title',
                                  f'implementer · {runtime} synthetic-model high · attempt 2 · ⌥185'])
                # A runtime's success line does not establish a workflow report.
                self.assertIn('implementer running · no outcome reported', formatted['run_status'])
                self.assertTrue(formatted['run_status'].endswith('1 earlier run'))
                self.assertEqual(formatted['notice'], '')
                self.assertIn('bytes ', formatted['raw_details'])
                self.assertIn('Rendered limit 400', formatted['raw_details'])
                for tab in (b'2', b'3', b'1'):
                    os.write(master, tab)
                    drain()
                    self.assertEqual(checkpoint()['header'], formatted['header'])
                self.assertEqual(checkpoint()['lines'], formatted['lines'])
                os.write(master, b'f')
                drain()
                os.write(master, b'u')
                drain()
                os.write(master, b'\x1b[H')  # Home on a hidden raw record.
                drain()
                hidden = checkpoint()
                self.assertTrue(hidden['raw'])
                self.assertEqual(hidden['anchor'][0], 0)
                self.assertIn('task_started' if runtime == 'claude' else 'thread.started', '\n'.join(hidden['lines']))
                os.write(master, b'u')
                drain()
                self.assertEqual(checkpoint()['anchor'], hidden['anchor'])
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
                os.kill(app.pid, signal.SIGWINCH)
                drain()
                self.assertEqual(checkpoint()['anchor'], hidden['anchor'])
                os.write(master, b'u')
                drain()
                self.assertEqual(checkpoint()['anchor'][0], hidden['anchor'][0])
                os.write(master, b'uy')  # Format and seek the recorded failure.
                drain()
                failed = checkpoint()
                failed_start = next(start for start, kind in failed['refs'] if kind == 'tool ERROR')
                self.assertEqual(failed['anchor'][0], failed_start)
                os.write(master, b'u')
                drain()
                raw_failed = checkpoint()
                self.assertEqual(raw_failed['anchor'][0], failed_start)
                # Raw JSON wraps at the pane width, including within error text.
                self.assertIn('No such file' if runtime == 'claude' else 'owned failure',
                              ' '.join(' '.join(raw_failed['lines']).split()))
                os.write(master, b'u')
                drain()
                self.assertEqual(checkpoint()['anchor'][0], failed_start)
                if runtime == 'claude':
                    # Synthetic #192 replay: attach mid-init, then stream several
                    # progress records and verify the actual terminal projection.
                    from tests.test_log_reader import record, progress, result, tool
                    init = json.dumps({'type': 'system', 'subtype': 'init',
                                       'tools': ['private-tool'] * 5000}).encode() + b'\n'
                    call = record(content=[{**tool(name='Edit'), 'input': {
                        'file_path': 'long/' * 80, 'new_string': 'one\ntwo\n', 'old_string': 'old'}}])
                    os.write(master, b'f')
                    drain()
                    log.write_bytes(init + record(content=[{'type': 'thinking'}], timestamp='2026-10-03T12:00:00Z') +
                                    record(content=[{'type': 'text', 'text': 'unknown\ncontinuation'}]) + call)
                    skipped = checkpoint(lambda value: any('earlier output skipped' in line for line in value['lines']))
                    self.assertIn('          · earlier output skipped · h older', skipped['lines'])
                    self.assertNotIn('private-tool', '\n'.join(skipped['lines']))
                    thinking = next(line for line in skipped['lines'] if '· thinking' in line)
                    self.assertEqual(thinking[0], ' ')
                    self.assertEqual(thinking[9:], ' · thinking')
                    self.assertIn('          unknown', skipped['lines'])
                    self.assertIn('          continuation', skipped['lines'])
                    with log.open('ab') as stream:
                        stream.write(progress(45))
                    elapsed = checkpoint(lambda value: any(line.endswith(' · 45s') for line in value['lines']))
                    self.assertTrue(next(line for line in elapsed['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 45s'))
                    self.assertNotIn('tool_progress', '\n'.join(elapsed['lines']))
                    os.write(master, b'f')
                    drain()
                    with log.open('ab') as stream:
                        stream.write(progress(60) + progress(119) + record('user', [result()]) +
                                     record(content=[{'type': 'text', 'text': 'captured'}]))
                    self.assertEqual(checkpoint()['lines'], elapsed['lines'])
                    os.write(master, b'u')
                    drain()
                    raw_progress = checkpoint()
                    self.assertIn('tool_progress', '\n'.join(raw_progress['lines']))
                    self.assertIn('private-tool', '\n'.join(raw_progress['lines']))
                    self.assertNotIn('earlier output skipped', '\n'.join(raw_progress['lines']))
                    os.write(master, b'uf')
                    finished = checkpoint(lambda value: any(line.endswith(' · 1m') for line in value['lines']))
                    self.assertTrue(next(line for line in finished['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 1m'))
                    captured = next(line for line in finished['lines'] if 'captured' in line)
                    self.assertEqual(captured[0], '~')
                    self.assertEqual(captured[9:], ' captured')
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    os.kill(app.pid, signal.SIGWINCH)
                    drain()
                    self.assertTrue(next(line for line in checkpoint()['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 1m'))
                    os.write(master, b'fh')
                    older = checkpoint(lambda value: value['refs'][0][0] < finished['refs'][0][0])
                    self.assertEqual(older['lines'], ['          · earlier output skipped · h older'])
                    os.write(master, b'u')
                    drain()
                    self.assertIn('private-tool', '\n'.join(checkpoint()['lines']))
                os.write(master, b'p')
                details = checkpoint(lambda value: bool(value['modal']))['modal']
                self.assertIn('bytes ', details)
                self.assertIn('Rendered limit 400:', details)
                self.assertIn('process.log', details)
                os.write(master, b'\x1b')
                drain()
                # A standalone observer restores its terminal and leaves its
                # owned replay file unchanged on quit.
                before = log.read_bytes()
                os.write(master, b'q')
                deadline = time.monotonic() + 3
                while app.poll() is None and time.monotonic() < deadline:
                    drain(0.05)
                self.assertEqual(app.wait(timeout=1), 0, bytes(transcript[-1000:]))
                drain()
                self.assertEqual(termios.tcgetattr(slave), modes)
                self.assertIn(b'\x1b[?1049l', transcript)
                self.assertIn(b'\x1b[?25h', transcript)
                self.assertEqual(log.read_bytes(), before)
            finally:
                if app.poll() is None:
                    app.terminate()
                    app.wait(timeout=3)
                os.close(master)
                os.close(slave)

    def test_captured_log_pause_retention_and_generation_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root)
            state['assignment'].update(kind='issue', attempt=1)
            state['base_version'] = '9.8.7'
            capture = Path(__file__).parent / 'fixtures/runtime_logs/claude.log'
            log.write_bytes(capture.read_bytes() * 20)
            state['latest_pass']['rows'].extend([
                {'item': 20, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
                {'item': 21, 'agent': 'worker', 'state': 'recover', 'reason': 'Pending outcome'},
                {'item': 22, 'agent': 'worker', 'state': 'blocked', 'reason': 'Cleanup unconfirmed'},
                {'item': 23, 'agent': 'worker', 'state': 'parked', 'reason': 'Stop label needs-human is present'},
                {'item': 24, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required'},
                {'item': 25, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for blockers #31'},
                {'item': 26, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for active milestone #10'},
                {'item': 27, 'agent': 'worker', 'state': 'backoff', 'reason': 'Retry backoff'},
                {'item': 28, 'agent': 'worker', 'state': 'waiting', 'reason': 'Runtime paused'},
            ])
            now = datetime.now(timezone.utc)
            state['outcomes'][0]['time'] = now.isoformat()
            state['outcomes'].insert(0, dict(state['outcomes'][0], run='older-run',
                                             time=(now - timedelta(days=2)).isoformat()))
            path.write_text(json.dumps(state))
            previous = root / '.ub-agents' / 'runs' / 'previous-run'
            previous.mkdir()
            outcome_log = previous / 'process.log'
            outcome_log.write_bytes(capture.read_bytes() * 20)
            script = '''
import json, pathlib, sys
from textual.binding import Binding
from textual.widgets import Static, Tree
from ub_agents.view_ui import LogPane, View, RecentActivity
class ProofView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'checkpoint', priority=True),
                Binding('r', 'recent_cursor', priority=True), Binding('s', 'plan_cursor', priority=True),
                Binding('a', 'assignment_cursor', priority=True)]
    def cursor(self, node):
        tree = self.query_one(Tree)
        tree.focus()
        tree.move_cursor(node)
    def action_recent_cursor(self):
        recent = self.query_one(RecentActivity)
        recent.cursor = recent.visible_rows[0].key
        recent.focus()
        recent.refresh()
    def action_plan_cursor(self):
        self.cursor(self.nodes['plan:21:worker'])
    def action_assignment_cursor(self):
        self.cursor(self.nodes['assignment:owned-run'])
    def action_checkpoint(self):
        self.call_after_refresh(lambda: self.query_one(LogPane).call_after_refresh(self.checkpoint))
    def checkpoint(self):
        pane = self.query_one(LogPane)
        r = self.reading
        tree = self.query_one(Tree)
        tree.get_node_at_line(0)
        recent = self.query_one(RecentActivity)
        row = self.rows.get(self.selected)
        value = {'follow': r.follow, 'raw': r.raw, 'generation': r.page.generation if r.page else None,
                 'starts': [ref.start for ref in r.page.refs] if r.page else [], 'anchor': pane.anchor(),
                 'saved_anchor': r.anchor,
                 'first_anchor': pane.positions[0] if pane.positions else None,
                 'entries': r.log.total_entries if r.log else 0, 'lag': r.log.unread_bytes if r.log else 0,
                 'notice': self.query_one('#log_note').render().plain,
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'raw_details': self.raw_details(),
                 'footer': self.query_one('#status', Static).render().plain,
                 'footer_height': self.query_one('#status').size.height,
                 'terminal_size': [self.size.width, self.size.height],
                 'pill': self.query_one('#log_state', Static).render().plain,
                 'pill_visible': self.query_one('#log_state').display,
                 'screen': type(self.screen).__name__,
                 'modal': self.screen.query_one('#raw_details', Static).render().plain
                          if self.screen.query('#raw_details') else '',
                 'selected': self.selected, 'group': row.group if row else None,
                 'state': row.state if row else None,
                 'cursor': tree.cursor_node.data if tree.cursor_node else None,
                 'focus': self.focused.id if self.focused else None,
                 'title': self.query_one('#work_pane').border_title,
                 'sections': [node.label.plain for node in tree.root.children],
                 'nodes': list(self.nodes),
                 'work_lines': {key: tree.render_line(node._line - tree.scroll_offset.y).text
                                for key, node in self.nodes.items()},
                 'work_details': {key: tree.render_line(node._line + 1 - tree.scroll_offset.y).text
                                  for key, node in self.nodes.items()},
                 'work_width': tree.scrollable_content_region.width,
                 'idle': self.idle_node.label.plain if self.idle_node else None,
                 'idle_dim': self.idle_node.label.style == 'dim' if self.idle_node else False,
                 'eligible': [node.data for node in self.groups.get('Eligible', tree.root).children],
                 'recent': recent.render().plain,
                 'recent_rows': [row.key for row in recent.visible_rows],
                 'recent_bounds': [recent.region.y, recent.size.height],
                 'upper_bounds': [tree.region.y, tree.size.height],
                 'upper_scroll': tree.scroll_y, 'recent_scroll': recent.scroll_y}
        # Replace atomically: the test polls for this file.
        partial = pathlib.Path(sys.argv[3] + '.partial')
        partial.write_text(json.dumps(value))
        partial.replace(sys.argv[3])
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            proof = root / 'proof.json'
            assignment, latest_pass, outcomes = state['assignment'], state['latest_pass'], state['outcomes']
            state.update(assignment=None, latest_pass={'state': 'partial', 'rows': [latest_pass['rows'][2]]},
                         outcomes=[])
            path.write_text(json.dumps(state))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
            modes = termios.tcgetattr(slave)
            env = dict(os.environ, TERM='xterm-256color')
            env.pop('NO_COLOR', None)
            app = subprocess.Popen([sys.executable, '-P', '-c', script, str(root), str(path), str(proof)],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
            transcript = bytearray()
            def drain(seconds=0.2):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.02)[0]:
                        transcript.extend(os.read(master, 65536))
            def checkpoint(ready=lambda value: True, timeout=5):
                # Take checkpoints until ready() holds rather than waiting a
                # fixed time; the assertions after it report a timeout.
                deadline = time.monotonic() + timeout
                while True:
                    proof.unlink(missing_ok=True)
                    os.write(master, b'x')
                    written = time.monotonic() + 3
                    while not proof.exists() and time.monotonic() < written:
                        drain(0.02)
                    self.assertTrue(proof.exists(), bytes(transcript[-1000:]))
                    value = json.loads(proof.read_text())
                    if ready(value):
                        return value
                    if time.monotonic() >= deadline:
                        self.fail(('Rendered state did not become ready', value, bytes(transcript[-1000:])))
                    drain(0.05)
            def pause_at_top():
                os.write(master, b'f')
                paused = checkpoint(lambda value: not value['follow'])
                self.assertFalse(paused['follow'])
                os.write(master, b'\x1b[H')
                scrolled = checkpoint(lambda value: value['anchor'] is not None
                                      and value['anchor'] == value['first_anchor'] == value['saved_anchor'])
                self.assertFalse(scrolled['follow'])
                self.assertIsNotNone(scrolled['anchor'])
                self.assertEqual(scrolled['anchor'], scrolled['first_anchor'])
                self.assertEqual(scrolled['anchor'], scrolled['saved_anchor'])
                return scrolled
            try:
                idle = checkpoint(lambda value: value['sections'] == ['Running · 0'] and value['idle'])
                self.assertIsNone(idle['selected'])
                self.assertEqual(idle['nodes'], [])
                self.assertEqual(idle['idle'], '    Idle · nothing eligible for this launcher')
                self.assertTrue(idle['idle_dim'])
                self.assertIn('○ Idle · waiting for the next poll', idle['run_status'])
                self.assertIn(b'Idle', transcript)
                os.write(master, b'\x1b[B\x1b[B\r')
                self.assertIsNone(checkpoint()['selected'])
                state.update(latest_pass={'state': 'complete', 'rows': []}, outcomes=outcomes)
                path.write_text(json.dumps(state))
                newest = checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                    and value['anchor'] is not None)
                self.assertEqual(newest['sections'], ['Running · 0'])
                self.assertEqual(newest['focus'], 'recent')
                self.assertEqual(newest['recent_rows'], ['outcome:previous-run', 'outcome:older-run'])
                state.update(assignment=assignment, latest_pass=latest_pass)
                path.write_text(json.dumps(state))
                initial = checkpoint(lambda value: len(value['sections']) == 3 and value['anchor'] is not None
                                     and value['selected'] == 'assignment:owned-run'
                                     and '#114 Cached title' in value['header'] and not value['pill_visible'])
                self.assertNotIn(b'FORMATTED', transcript)
                self.assertNotIn(b'FOLLOW', transcript)
                self.assertIn('ub-agents v9.8.7 · running assignment', initial['footer'])
                self.assertTrue(initial['footer'].endswith('↑↓ select ⏎ open 1-3 tabs ? keys q quit'))
                self.assertEqual(initial['footer_height'], 1)
                self.assertFalse(initial['pill_visible'])
                self.assertEqual(initial['notice'], '')
                self.assertEqual(initial['sections'], ['Running · 1', 'Needs attention · 3',
                                                       'Eligible · 5'])
                self.assertEqual(initial['eligible'], ['plan:12:reviewer', 'plan:20:worker', 'plan:21:worker',
                                                      'plan:27:worker', 'plan:28:worker'])
                for item in (25, 26):
                    self.assertNotIn(f'plan:{item}:worker', initial['nodes'])
                self.assertTrue(initial['work_lines']['plan:12:reviewer'].endswith('next'))
                for item, status in ((27, 'backoff'), (28, 'waiting')):
                    line = initial['work_lines'][f'plan:{item}:worker']
                    self.assertTrue(line.startswith(f'◷ #{item}'), line)
                    self.assertTrue(line.endswith(status), line)
                    self.assertNotIn('next', line)
                self.assertNotIn('plan:13:reviewer', initial['nodes'])
                self.assertIsNone(initial['idle'])
                self.assertIn('partial', initial['title'])
                self.assertTrue(initial['recent'].splitlines()[0].startswith('Recent activity · 1 today'))
                self.assertEqual(initial['recent_rows'], ['outcome:previous-run', 'outcome:older-run'])
                self.assertLessEqual(abs(initial['upper_bounds'][1] - initial['recent_bounds'][1]), 1)
                self.assertEqual(sum(initial['upper_bounds']), initial['recent_bounds'][0])
                self.assertEqual(initial['header'].splitlines()[:2],
                                 ['#114 Cached title', 'implementer · claude synthetic-model high · attempt 1'])
                self.assertIn('implementer running · no outcome reported', initial['run_status'])
                self.assertNotIn('bytes ', initial['notice'])
                for tab in (b'2', b'3', b'1'):
                    os.write(master, tab)
                    drain()
                    self.assertEqual(checkpoint()['header'], initial['header'])
                state['activity'] = {'state': 'waiting', 'until': (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
                path.write_text(json.dumps(state))
                waiting = checkpoint(lambda value: re.search(r'next poll (\d+)s', value['footer']))
                remaining = int(re.search(r'next poll (\d+)s', waiting['footer']).group(1))
                counted = checkpoint(lambda value: re.search(r'next poll (\d+)s', value['footer'])
                                     and int(re.search(r'next poll (\d+)s', value['footer']).group(1)) < remaining)
                self.assertLess(int(re.search(r'next poll (\d+)s', counted['footer']).group(1)), remaining)
                state['activity'] = {'state': 'stopping'}
                path.write_text(json.dumps(state))
                stopping = checkpoint(lambda value: '· stopping' in value['footer']
                                      and 'Stopping after this run' in value['run_status'])
                self.assertEqual(stopping['sections'], ['Running · 1', 'Needs attention · 3',
                                                        'Eligible · 5 · not claimed while stopping'])
                self.assertTrue(stopping['work_lines']['assignment:owned-run'].startswith('■ #114'))
                self.assertTrue(stopping['work_lines']['assignment:owned-run'].endswith('stopping'))
                from ub_agents.view_ui import pane_line
                self.assertEqual(stopping['work_details']['assignment:owned-run'].rstrip(),
                                 pane_line('  implementer · this launcher · finishing run',
                                           stopping['work_width']).plain)
                self.assertNotIn('attempt', stopping['work_details']['assignment:owned-run'])
                self.assertEqual(stopping['run_status'].splitlines()[1],
                                 '■ Stopping after this run (SIGTERM) · no new claims')
                for key in ('plan:12:reviewer', 'plan:20:worker', 'plan:21:worker'):
                    self.assertTrue(stopping['work_lines'][key].endswith('held'))
                for item, status in ((27, 'backoff'), (28, 'waiting')):
                    self.assertTrue(stopping['work_lines'][f'plan:{item}:worker'].endswith(status))
                os.write(master, b's\r')
                other = checkpoint(lambda value: value['selected'] == 'plan:21:worker'
                                   and 'worker recover · no outcome reported' in value['run_status'])
                self.assertNotIn('Stopping after this run', other['run_status'])
                os.write(master, b'a\r')
                checkpoint(lambda value: value['selected'] == 'assignment:owned-run'
                           and 'Stopping after this run' in value['run_status'])
                state['published_at'] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
                path.write_text(json.dumps(state))
                self.assertIn('· stale', checkpoint(lambda value: '· stale' in value['footer'])['footer'])
                state['ended'] = True
                path.write_text(json.dumps(state))
                self.assertIn('· ended', checkpoint(lambda value: '· ended' in value['footer'])['footer'])
                path.write_text('{broken')
                malformed = checkpoint(lambda value: 'malformed: Expecting property' in value['footer'])
                self.assertIn('malformed: Expecting property', malformed['footer'])
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
                os.kill(app.pid, signal.SIGWINCH)
                smaller = checkpoint(lambda value: value['terminal_size'] == [80, 24]
                                     and 'minimum 110×32' in value['footer'])
                self.assertIn('minimum 110×32', smaller['footer'])
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                os.kill(app.pid, signal.SIGWINCH)
                state['ended'] = False
                state['published_at'] = datetime.now(timezone.utc).isoformat()
                state['activity'] = {'state': 'running assignment'}
                path.write_text(json.dumps(state))
                checkpoint(lambda value: value['terminal_size'] == [110, 32]
                           and '· running assignment' in value['footer']
                           and 'malformed' not in value['footer'])
                os.write(master, b'?')
                help_view = checkpoint(lambda value: value['screen'] == 'KeyHelp')
                self.assertEqual(help_view['screen'], 'KeyHelp')
                for key in ('Tab', 'arrows', 'Enter', '1 / 2 / 3', 'g on Issue', 'f   ', 'h   ',
                            'u   ', 'p   ', 'Page Up', 'Page Down', 'Home', 'End', 'Escape', 'q   ', 'Ctrl-C'):
                    self.assertIn(key, help_view['modal'])
                os.write(master, b'?')
                self.assertNotEqual(checkpoint(lambda value: value['screen'] != 'KeyHelp')['screen'], 'KeyHelp')
                os.write(master, b'?')
                checkpoint(lambda value: value['screen'] == 'KeyHelp')
                os.write(master, b'\x1b\x1b[I')
                self.assertNotEqual(checkpoint(lambda value: value['screen'] != 'KeyHelp')['screen'], 'KeyHelp')
                paused = pause_at_top()
                self.assertFalse(paused['follow'])
                self.assertTrue(paused['pill_visible'])
                self.assertIn('⏸ PAUSED', paused['pill'])
                self.assertTrue(paused['footer'].endswith('f follow h older u raw PgUp/PgDn scroll ? keys q quit'))
                # More than both ingestion retention (200 entries) and renderer
                # retention (400 wrapped rows) arrive while the page is paused.
                from tests.test_view_data import event
                with log.open('ab') as stream:
                    stream.write(b''.join(event(i, size=800) for i in range(1200)))
                checkpoint(lambda value: value['entries'] - paused['entries'] > 200)
                os.write(master, b'231')
                drain(0.3)
                retained = checkpoint(lambda value: value['anchor'] is not None
                                      and value['anchor'][0] == paused['anchor'][0]
                                      and abs(value['anchor'][1] - paused['anchor'][1]) <= 0.05)
                self.assertEqual(retained['starts'], paused['starts'])
                self.assertEqual(retained['anchor'][0], paused['anchor'][0])
                # Tab relayout may settle the scrollbar and change wrapping;
                # the same entry and proportional reading position survive.
                self.assertAlmostEqual(retained['anchor'][1], paused['anchor'][1], delta=0.05)
                self.assertGreater(retained['entries'] - paused['entries'], 200)
                self.assertIn('new ↓', retained['pill'])
                self.assertIn('B lag', retained['pill'])
                self.assertNotIn('evicted', retained['notice'])
                os.write(master, b'p')
                drain()
                raw = checkpoint()
                self.assertEqual(raw['screen'], 'RawAccess')
                for diagnostic in (str(log), 'Displayed bytes', 'Page bytes', 'evicted', 'skipped',
                                   'shortened', 'Rendered limit 400', 'entries hidden'):
                    self.assertIn(diagnostic, raw['modal'])
                os.write(master, b'\x1b')
                drain()
                os.write(master, b'r\r')  # Focus the newest outcome, then real Enter.
                self.assertEqual(checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                            and value['anchor'] is not None)['selected'], 'outcome:previous-run')
                outcome_paused = pause_at_top()
                self.assertIsNotNone(outcome_paused['anchor'])
                with outcome_log.open('ab') as stream:
                    stream.write(b''.join(event(i, size=800) for i in range(600)))
                outcome_retained = checkpoint(lambda value: value['entries'] - outcome_paused['entries'] > 200)
                self.assertGreater(outcome_retained['entries'] - outcome_paused['entries'], 200)
                self.assertEqual(outcome_retained['recent_rows'], initial['recent_rows'])
                self.assertEqual(outcome_retained['starts'], outcome_paused['starts'])
                self.assertEqual(outcome_retained['anchor'], outcome_paused['anchor'])
                os.write(master, b'r\x1b[A\r')  # Up crosses to the last live row.
                upper = checkpoint(lambda value: value['selected'].startswith('plan:') and value['focus'] == 'work')
                self.assertGreater(upper['upper_scroll'], 0)
                self.assertEqual(upper['recent_bounds'], initial['recent_bounds'])
                os.write(master, b'\x1b[B\r')  # Down crosses back to the newest outcome.
                revisited = checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                       and value['starts'] == outcome_paused['starts'] and value['anchor'] is not None
                                       and value['anchor'][0] == outcome_paused['anchor'][0]
                                       and abs(value['anchor'][1] - outcome_paused['anchor'][1]) <= 0.05)
                self.assertEqual(revisited['starts'], outcome_paused['starts'])
                self.assertEqual(revisited['anchor'][0], outcome_paused['anchor'][0])
                self.assertAlmostEqual(revisited['anchor'][1], outcome_paused['anchor'][1], delta=0.05)
                # New outcomes clip the selected row without changing its pane or paused page.
                original_outcomes = list(state['outcomes'])
                state['outcomes'] += [dict(state['outcomes'][-1], run=f'new-{n}', item=40 + n)
                                      for n in range(18)]
                path.write_text(json.dumps(state))
                clipped = checkpoint(lambda value: value['recent_rows'][0] == 'outcome:new-17')
                self.assertNotIn('outcome:previous-run', clipped['recent_rows'])
                self.assertEqual(clipped['selected'], 'outcome:previous-run')
                self.assertEqual(clipped['header'], revisited['header'])
                self.assertEqual(clipped['starts'], outcome_paused['starts'])
                self.assertEqual(clipped['anchor'], revisited['anchor'])
                self.assertEqual(clipped['recent_scroll'], 0)
                self.assertEqual(len(clipped['recent'].splitlines()), 1 + 2 * len(clipped['recent_rows']))
                state['outcomes'] = original_outcomes
                path.write_text(json.dumps(state))
                checkpoint(lambda value: value['recent_rows'] == initial['recent_rows'])
                os.write(master, b's\r')
                drain()
                plan = checkpoint()
                state['latest_pass']['state'] = 'complete'
                state['latest_pass']['rows'] = [
                    {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required'}]
                path.write_text(json.dumps(state))
                moved = checkpoint(lambda value: value['group'] == 'Needs attention' and value['cursor'] == plan['cursor'])
                self.assertEqual(moved['group'], 'Needs attention')
                self.assertEqual(moved['selected'], plan['selected'])
                self.assertEqual(moved['cursor'], plan['cursor'])
                self.assertEqual(moved['focus'], plan['focus'])
                self.assertEqual(moved['sections'], ['Running · 1', 'Needs attention · 1'])
                self.assertNotIn('partial', moved['title'])
                state['latest_pass']['rows'][0]['reason'] = 'Waiting for blockers #31'
                path.write_text(json.dumps(state))
                hidden = checkpoint(lambda value: value['sections'] == ['Running · 1']
                                    and value['state'] == 'earlier observation')
                self.assertNotIn(plan['selected'], hidden['nodes'])
                self.assertEqual(hidden['selected'], plan['selected'])
                self.assertEqual(hidden['focus'], plan['focus'])
                state['latest_pass']['rows'] = []
                path.write_text(json.dumps(state))
                self.assertEqual(checkpoint(lambda value: value['state'] == 'earlier observation')['state'],
                                 'earlier observation')
                os.write(master, b'a\r')
                restored = checkpoint(lambda value: value['starts'] == paused['starts'] and value['anchor'] is not None
                                      and value['anchor'][0] == paused['anchor'][0]
                                      and abs(value['anchor'][1] - paused['anchor'][1]) <= 0.05)
                self.assertEqual(restored['starts'], paused['starts'])
                self.assertEqual(restored['anchor'][0], paused['anchor'][0])
                self.assertAlmostEqual(restored['anchor'][1], paused['anchor'][1], delta=0.05)
                os.write(master, b'h')
                older = checkpoint(lambda value: value['starts'] and value['starts'][0] < paused['starts'][0])
                self.assertLess(older['starts'][0], paused['starts'][0])
                os.write(master, b'p')
                drain()
                raw = checkpoint()['raw_details']
                self.assertIn('bytes ', raw)
                self.assertIn('evicted ', raw)
                self.assertIn('skipped ', raw)
                self.assertIn('shortened ', raw)
                self.assertIn('Rendered limit 400:', raw)
                self.assertIn('process.log', raw)
                self.assertIn(b'Rendered limit 400:', transcript)
                os.write(master, b'\x1b')
                drain()
                os.write(master, b'u')
                raw_mode = checkpoint(lambda value: value['raw'])
                self.assertTrue(raw_mode['raw'])
                self.assertIn('RAW', raw_mode['pill'])
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
                os.kill(app.pid, signal.SIGWINCH)
                drain(0.2)
                # Replacement preserves a paused earlier generation until follow.
                replacement = log.with_suffix('.next')
                replacement.write_bytes(capture.read_bytes())
                replacement.replace(log)
                changed = checkpoint(lambda value: 'FILE CHANGED' in value['notice'])
                self.assertEqual(changed['generation'], older['generation'])
                self.assertIn('FILE CHANGED', changed['notice'])
                os.write(master, b'h')
                self.assertIn('File changed', checkpoint(lambda value: 'File changed' in value['notice'])['notice'])
                os.write(master, b'f')
                resumed = checkpoint(lambda value: value['follow'] and value['generation'] > changed['generation'])
                self.assertTrue(resumed['follow'])
                self.assertFalse(resumed['pill_visible'])
                self.assertGreater(resumed['generation'], changed['generation'])
                log.write_bytes(capture.read_bytes().splitlines(keepends=True)[0])
                self.assertGreater(checkpoint(lambda value: value['generation'] > resumed['generation'])['generation'],
                                   resumed['generation'])
                # Exercise the fixed split in both real terminal sizes, including
                # an idle upper viewport and zero cached outcomes.
                os.write(master, b'r\r')
                checkpoint(lambda value: value['selected'] == 'outcome:previous-run')
                state['assignment'] = None
                state['latest_pass'] = {'state': 'complete', 'rows': []}
                path.write_text(json.dumps(state))
                empty = checkpoint(lambda value: value['sections'] == ['Running · 0'])
                self.assertLessEqual(abs(empty['upper_bounds'][1] - empty['recent_bounds'][1]), 1)
                self.assertEqual(sum(empty['upper_bounds']), empty['recent_bounds'][0])
                state['outcomes'] = []
                path.write_text(json.dumps(state))
                zero = checkpoint(lambda value: value['recent'].startswith('Recent activity · 0 today'))
                self.assertEqual(zero['recent_bounds'], empty['recent_bounds'])
                state['latest_pass']['rows'] = [
                    {'item': n, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'}
                    for n in range(1, 50)]
                path.write_text(json.dumps(state))
                overflow = checkpoint(lambda value: value['sections'] == ['Running · 0', 'Eligible · 49'])
                self.assertEqual(overflow['recent_bounds'], empty['recent_bounds'])
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                os.kill(app.pid, signal.SIGWINCH)
                minimum = checkpoint(lambda value: value['terminal_size'] == [110, 32]
                                     and value['recent_bounds'] == initial['recent_bounds'])
                self.assertEqual(minimum['recent_bounds'], initial['recent_bounds'])
                state['latest_pass']['rows'] = []
                path.write_text(json.dumps(state))
                minimum_empty = checkpoint(lambda value: value['sections'] == ['Running · 0'])
                self.assertEqual(minimum_empty['recent_bounds'], initial['recent_bounds'])
                os.write(master, b'q')
                deadline = time.monotonic() + 3
                while app.poll() is None and time.monotonic() < deadline:
                    drain(0.05)
                self.assertEqual(app.wait(timeout=1), 0, bytes(transcript[-1000:]))
                drain()
                self.assertEqual(termios.tcgetattr(slave), modes)
                self.assertIn(b'\x1b[?1049l', transcript)
            finally:
                if app.poll() is None:
                    app.terminate()
                app.wait(timeout=3)
                os.close(master)
                os.close(slave)
