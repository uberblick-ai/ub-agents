"""Actual owned 110x32 PTY acceptance, separate from Textual headless pilots."""

import fcntl
from datetime import datetime, timedelta, timezone
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
        value = {'display': banner.display, 'height': banner.size.height,
                 'width': banner.content_size.width, 'line': banner.render().plain,
                 'text': banner.banner.get('text'), 'terminal_width': self.size.width,
                 'cells': banner.render().cell_len, 'yellow': str(banner.styles.background),
                 'banner_y': banner.region.y, 'body_y': self.query_one('#body').region.y,
                 'header_y': header.region.y, 'header_height': header.size.height,
                 'header': header.render().plain, 'output_y': output.region.y,
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
                until(lambda value: value['starts'] and value['focus'] is not None)
                self.assertIn(b'FOLLOW', transcript)
                self.assertFalse(checkpoint()['display'])
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
                        current = until(lambda value: value['text'] == banner['text']
                                        and value['display'] and value['height'] == 1
                                        and value['terminal_width'] == width
                                        and value['width'] == width - 2 and value['body_y'] == 1
                                        and value['anchor'] == paused['anchor'])
                        self.assertTrue(current['display'])
                        self.assertEqual(current['height'], 1)
                        self.assertEqual(current['banner_y'], 0)
                        self.assertEqual(current['body_y'], 1)
                        self.assertGreater(current['header_y'], current['banner_y'])
                        self.assertEqual(current['header_height'], 3)
                        self.assertLessEqual(current['header_y'] + current['header_height'], current['output_y'])
                        self.assertIn('#114', current['header'])
                        self.assertEqual(current['yellow'], 'Color(215, 175, 0)')
                        self.assertLessEqual(current['cells'], width - 2)
                        self.assertEqual(current['selected'], paused['selected'])
                        self.assertEqual(current['focus'], paused['focus'])
                        self.assertEqual(current['starts'], paused['starts'])
                        self.assertEqual(current['anchor'], paused['anchor'])
                        self.assertEqual(current['saved_anchor'], paused['saved_anchor'])
                        self.assertFalse(current['follow'])
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
                until(b'FORMATTED')
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
                    until(b'FOLLOW', b'FORMATTED', b'Running', b'partial', b'\x1b[?1049h', b'replay output')
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
                    os.write(master, b'3')
                    until(b'needs-human')
                    os.write(master, b'1p')
                    until(b'process.log')
                    # A focus report right after Escape ends the escape sequence,
                    # so a busy machine cannot merge Escape and f into Alt+f.
                    os.write(master, b'\x1b\x1b[I')
                    drain()
                    os.write(master, b'f')
                    until(b'FOLLOW')
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

class TerminalRetentionTests(unittest.TestCase):
    def test_compact_claude_replay_hidden_and_failed_result_toggles_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root)
            state['assignment'].update(kind='issue', attempt=2)
            state['outcomes'].append({'item': 114, 'run': 'earlier-run', 'handoff': 185})
            path.write_text(json.dumps(state))
            capture = Path(__file__).parent / 'fixtures/runtime_logs/claude.log'
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
        pane = self.query_one(LogPane)
        r = self.reading
        value = {'raw': r.raw, 'anchor': pane.anchor(), 'entries': len(r.page.refs),
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'notice': self.query_one('#log_note').render().plain,
                 'raw_details': self.raw_details(),
                 'lines': [line.text for line in pane.lines],
                 'refs': [(ref.start, ref.value.kind) for ref in r.page.refs],
                 'positions': pane.positions}
        pathlib.Path(sys.argv[3]).write_text(json.dumps(value))
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
            def checkpoint():
                proof.unlink(missing_ok=True)
                os.write(master, b'x')
                deadline = time.monotonic() + 3
                while not proof.exists() and time.monotonic() < deadline:
                    drain(0.05)
                self.assertTrue(proof.exists(), bytes(transcript[-1000:]))
                return json.loads(proof.read_text())
            try:
                drain(0.8)
                # Appending after attachment replays every recorded input from
                # byte zero, preserving capture times as well as producer times.
                from tests.test_view_data import event
                with log.open('ab') as stream:
                    stream.write(capture.read_bytes())
                    stream.write(b''.join(event(i, 20) for i in range(10)))
                drain(0.8)
                formatted = checkpoint()
                self.assertEqual(formatted['entries'], len(capture.read_bytes().splitlines()) + 10)
                text = '\n'.join(formatted['lines'])
                self.assertIn('· thinking', text)
                self.assertIn('▸ Read <fixture>/fixture_output.py', text)
                self.assertIn('▸ Bash cat missing-owned.txt', text)
                self.assertIn('✗ Exit code 1', text)
                self.assertIn('✓ run finished', text)
                self.assertNotIn('thinking_tokens', text)
                self.assertNotIn('producer=', text)
                self.assertEqual(formatted['header'].splitlines()[:2],
                                 ['#114 Cached title',
                                  'implementer · claude synthetic-model high · attempt 2 · ⌥185'])
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
                self.assertIn('task_started', '\n'.join(hidden['lines']))
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
                self.assertIn('No such file', '\n'.join(raw_failed['lines']))
                os.write(master, b'u')
                drain()
                self.assertEqual(checkpoint()['anchor'][0], failed_start)
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
from textual.widgets import Tree
from ub_agents.view_ui import LogPane, View
class ProofView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'checkpoint', priority=True),
                Binding('r', 'recent_cursor', priority=True), Binding('s', 'plan_cursor', priority=True),
                Binding('a', 'assignment_cursor', priority=True)]
    def cursor(self, node):
        tree = self.query_one(Tree)
        tree.focus()
        tree.move_cursor(node)
    def action_recent_cursor(self):
        self.cursor(self.groups['Recent activity'])
    def action_plan_cursor(self):
        self.cursor(self.nodes['plan:21:worker'])
    def action_assignment_cursor(self):
        self.cursor(self.nodes['assignment:owned-run'])
    def action_checkpoint(self):
        pane = self.query_one(LogPane)
        r = self.reading
        tree = self.query_one(Tree)
        row = self.rows.get(self.selected)
        value = {'follow': r.follow, 'raw': r.raw, 'generation': r.page.generation if r.page else None,
                 'starts': [ref.start for ref in r.page.refs] if r.page else [], 'anchor': pane.anchor(),
                 'entries': r.log.total_entries if r.log else 0, 'lag': r.log.unread_bytes if r.log else 0,
                 'notice': self.query_one('#log_note').render().plain,
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'raw_details': self.raw_details(),
                 'selected': self.selected, 'group': row.group if row else None,
                 'state': row.state if row else None,
                 'cursor': tree.cursor_node.data if tree.cursor_node else None,
                 'focus': self.focused.id if self.focused else None, 'title': tree.root.label.plain,
                 'sections': [node.label.plain for node in tree.root.children],
                 'eligible': [node.data for node in self.groups.get('Eligible', tree.root).children],
                 'recent_expanded': 'Recent activity' in self.groups and self.groups['Recent activity'].is_expanded}
        # Replace atomically: the test polls for this file.
        partial = pathlib.Path(sys.argv[3] + '.partial')
        partial.write_text(json.dumps(value))
        partial.replace(sys.argv[3])
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            proof = root / 'proof.json'
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
                    if ready(value) or time.monotonic() >= deadline:
                        return value
                    drain(0.05)
            try:
                while b'FORMATTED' not in transcript and app.poll() is None:
                    drain(0.05)
                initial = checkpoint(lambda value: len(value['sections']) == 5 and value['anchor'] is not None)
                self.assertEqual(initial['sections'], ['Running · 2', 'Needs attention · 3',
                                                       'Eligible · 2', 'Waiting · 4',
                                                       'Recent activity · 1 today'])
                self.assertEqual(initial['eligible'], ['plan:20:worker', 'plan:21:worker'])
                self.assertIn('partial', initial['title'])
                self.assertFalse(initial['recent_expanded'])
                self.assertEqual(initial['header'].splitlines()[:2],
                                 ['#114 Cached title', 'implementer · claude synthetic-model high · attempt 1'])
                self.assertIn('implementer running · no outcome reported', initial['run_status'])
                self.assertNotIn('bytes ', initial['notice'])
                for tab in (b'2', b'3', b'1'):
                    os.write(master, tab)
                    drain()
                    self.assertEqual(checkpoint()['header'], initial['header'])
                os.write(master, b'f\x1b[5~')
                drain(0.2)
                paused = checkpoint(lambda value: not value['follow'])
                self.assertFalse(paused['follow'])
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
                os.write(master, b'r\r')  # Focus Recent activity, then real Enter.
                self.assertTrue(checkpoint(lambda value: value['recent_expanded'])['recent_expanded'])
                os.write(master, b'\x1b[B\r')  # Down to the latest outcome and Enter.
                self.assertEqual(checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                            and value['anchor'] is not None)['selected'], 'outcome:previous-run')
                os.write(master, b'f\x1b[5~')
                drain(0.2)
                outcome_paused = checkpoint(lambda value: not value['follow'] and value['anchor'] is not None)
                self.assertIsNotNone(outcome_paused['anchor'])
                with outcome_log.open('ab') as stream:
                    stream.write(b''.join(event(i, size=800) for i in range(600)))
                outcome_retained = checkpoint(lambda value: value['entries'] - outcome_paused['entries'] > 200)
                self.assertGreater(outcome_retained['entries'] - outcome_paused['entries'], 200)
                self.assertTrue(outcome_retained['recent_expanded'])
                self.assertEqual(outcome_retained['starts'], outcome_paused['starts'])
                self.assertEqual(outcome_retained['anchor'], outcome_paused['anchor'])
                os.write(master, b'r\r')
                collapsed = checkpoint(lambda value: not value['recent_expanded'])
                self.assertEqual(collapsed['selected'], 'recent-activity')
                self.assertEqual(collapsed['cursor'], 'recent-activity')
                self.assertFalse(collapsed['recent_expanded'])
                os.write(master, b'\r')
                checkpoint(lambda value: value['recent_expanded'])
                os.write(master, b'\x1b[B\r')
                revisited = checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                       and value['starts'] == outcome_paused['starts'] and value['anchor'] is not None
                                       and value['anchor'][0] == outcome_paused['anchor'][0]
                                       and abs(value['anchor'][1] - outcome_paused['anchor'][1]) <= 0.05)
                self.assertEqual(revisited['starts'], outcome_paused['starts'])
                self.assertEqual(revisited['anchor'][0], outcome_paused['anchor'][0])
                self.assertAlmostEqual(revisited['anchor'][1], outcome_paused['anchor'][1], delta=0.05)
                os.write(master, b's\r')
                drain()
                plan = checkpoint()
                state['latest_pass']['state'] = 'complete'
                state['latest_pass']['rows'] = [
                    {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for blockers #31'}]
                path.write_text(json.dumps(state))
                moved = checkpoint(lambda value: value['group'] == 'Waiting' and value['cursor'] == plan['cursor'])
                self.assertEqual(moved['group'], 'Waiting')
                self.assertEqual(moved['selected'], plan['selected'])
                self.assertEqual(moved['cursor'], plan['cursor'])
                self.assertEqual(moved['focus'], plan['focus'])
                self.assertEqual(moved['sections'][:2], ['Running · 1', 'Waiting · 1'])
                self.assertNotIn('partial', moved['title'])
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
                self.assertTrue(checkpoint(lambda value: value['raw'])['raw'])
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
                self.assertGreater(resumed['generation'], changed['generation'])
                log.write_bytes(capture.read_bytes().splitlines(keepends=True)[0])
                self.assertGreater(checkpoint(lambda value: value['generation'] > resumed['generation'])['generation'],
                                   resumed['generation'])
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
