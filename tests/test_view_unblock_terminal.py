"""Unblock acceptance in an owned real PTY with scripted read-only sources."""

from datetime import datetime, timezone
import fcntl
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
from tests.test_view_unblock import AUTHORS, NOTICE


class UnblockTerminalTests(unittest.TestCase):
    def test_unblock_in_real_terminal_q_and_ctrl_c_restore_terminal(self):
        for quit_key in (b'q', b'\x03'):
            with self.subTest(quit_key=quit_key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, _, state = fixture(root)
                state['assignment'] = None
                state['base_version'] = '0.1.11'
                state['activity'] = {'state': 'polling'}
                state['latest_pass'] = {'state': 'complete', 'rows': [
                    {'item': 178, 'agent': 'worker', 'kind': 'pr', 'title': 'Blocked candidate',
                     'state': 'parked', 'reason': 'Approval needed'},
                    {'item': 179, 'agent': 'worker', 'state': 'blocked', 'reason': 'Attempt limit exhausted',
                     'failures': 3, 'max_attempts': 3},
                    {'item': 180, 'agent': 'worker', 'state': 'ready', 'reason': 'Ready'}]}
                state['coordination_authors'] = AUTHORS
                state['action_needed'] = {'178': {'text': NOTICE, 'author': 'operator',
                                                 'created_at': '2026-10-05T12:12:00Z'}}
                path.write_text(json.dumps(state))
                proof = root / 'proof.json'
                script = '''
import json, pathlib, sys
sys.path.insert(0, sys.argv[4])
from textual.binding import Binding
from textual.widgets import Markdown
from tests.support import RecordingDescriptionTransport
from tests.test_view_unblock import AUTHORS, comment, comments_reply
from ub_agents.view_github import DescriptionLoads, parse_response
from ub_agents.view_ui import ItemTabs, View
from ub_agents.view_unblock import stamp
transport = RecordingDescriptionTransport()
class ProofView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'checkpoint', priority=True),
                Binding('a', 'select_attention', priority=True),
                Binding('b', 'select_foreign', priority=True),
                Binding('e', 'select_eligible', priority=True),
                Binding('t', 'advance', priority=True), Binding('l', 'complete', priority=True)]
    def action_select_attention(self): self.select('plan:178:worker')
    def action_select_foreign(self): self.select('plan:179:worker')
    def action_select_eligible(self): self.select('plan:180:worker')
    def action_advance(self): self.now += 60
    def action_complete(self):
        transport.response = parse_response(comments_reply(comment()), b'', 0, self.now, 'unblock', AUTHORS)
    def action_checkpoint(self): self.call_after_refresh(self.checkpoint)
    def checkpoint(self):
        value = {'tab': self.query_one(ItemTabs).active, 'attention': self.unblock_visible,
                 'selected': self.selected, 'header': self.query_one('#item_header').render().plain,
                 'note': self.query_one('#unblock_note').render().plain,
                 'body': self.query_one('#unblock_body', Markdown).source,
                 'footer': self.query_one('#status').render().plain, 'calls': len(transport.calls),
                 'pending': self.descriptions.pending, 'screen': type(self.screen).__name__,
                 'size': list(self.size), 'narrow': self.narrow, 'item': self.item_view,
                 'modal': self.screen.query_one('#raw_details').render().plain
                          if self.screen.query('#raw_details') else '',
                 'visible': [strip.text for strip in self.screen._compositor.render_strips()]}
        staging = pathlib.Path(sys.argv[3] + '.new')
        staging.write_text(json.dumps(value))
        staging.replace(sys.argv[3])
app = ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),
                descriptions=DescriptionLoads(transport, clock=lambda: app.now))
app.now = stamp('2026-10-05T12:36:00Z')
app.run()
pathlib.Path(sys.argv[3] + '.closed').write_text(str(transport.closed))
'''
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                modes = termios.tcgetattr(slave)
                process = subprocess.Popen([sys.executable, '-P', '-c', script, str(root), str(path), str(proof),
                    str(Path(__file__).resolve().parents[1])],
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                    env=dict(os.environ, TERM='xterm-256color'))
                transcript = bytearray()
                def drain(seconds=0.05):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.02)[0]:
                            transcript.extend(os.read(master, 65536))
                def checkpoint(condition=lambda _: True):
                    deadline = time.monotonic() + 5
                    value = None
                    while time.monotonic() < deadline:
                        proof.unlink(missing_ok=True)
                        os.write(master, b'x')
                        while not proof.exists() and time.monotonic() < deadline:
                            drain()
                        self.assertTrue(proof.exists(), bytes(transcript[-1000:]))
                        value = json.loads(proof.read_text())
                        if condition(value):
                            return value
                        drain()
                    self.fail((value, bytes(transcript[-1000:])))
                try:
                    checkpoint(lambda value: value['attention'])
                    os.write(master, b'4g')
                    cached = checkpoint(lambda value: value['tab'] == 'unblock' and 'snapshot' in value['note']
                                        and 'waiting 24m' in value['header'])
                    self.assertIn('⌥178 Blocked candidate', cached['header'])
                    self.assertIn('worker · parked · waiting 24m · since ', cached['header'])
                    self.assertIn('Resolve the blocker', '\n'.join(cached['visible']))
                    self.assertNotIn('**Action needed**', cached['body'])
                    self.assertIn('1-4 tabs g load', cached['footer'])
                    self.assertEqual(cached['calls'], 0)
                    os.write(master, b't?')
                    help_view = checkpoint(lambda value: value['screen'] == 'KeyHelp')
                    self.assertIn('g on Unblock', help_view['modal'])
                    os.write(master, b'?')
                    checkpoint(lambda value: value['screen'] != 'KeyHelp' and 'waiting 25m' in value['header'])
                    # Real resize and Enter/Esc navigation at the minimum width.
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 16, 60, 0, 0))
                    os.kill(process.pid, signal.SIGWINCH)
                    checkpoint(lambda value: value['size'] == [60, 16])
                    os.write(master, b'\r')
                    narrow = checkpoint(lambda value: value['item'] and 'g load' in value['footer'])
                    self.assertIn('1-4 tabs', narrow['footer'])
                    self.assertIn('v', narrow['footer'])
                    os.write(master, b'\x1b')
                    drain(0.3)
                    checkpoint(lambda value: not value['item'])
                    os.write(master, b'\r')
                    checkpoint(lambda value: value['item'])
                    os.write(master, b'f')
                    paused = checkpoint(lambda value: 'f follow' in value['footer'])
                    self.assertIn('g load', paused['footer'])
                    self.assertIn('1-4 tabs', paused['footer'])
                    self.assertIn('v', paused['footer'])
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    os.kill(process.pid, signal.SIGWINCH)
                    checkpoint(lambda value: value['size'] == [110, 32])
                    os.write(master, b'b4g2g')
                    pending = checkpoint(lambda value: value['pending'] is not None)
                    self.assertEqual(pending['calls'], 1)
                    os.write(master, b'l4')
                    loaded = checkpoint(lambda value: value['tab'] == 'unblock' and 'GitHub · loaded' in value['note']
                                        and 'failed 3/3' in value['header'])
                    self.assertIn('failed 3/3', loaded['header'])
                    os.write(master, b'g')
                    self.assertEqual(checkpoint()['calls'], 1)
                    os.write(master, b'e4')
                    eligible = checkpoint(lambda value: value['selected'] == 'plan:180:worker')
                    self.assertFalse(eligible['attention'])
                    self.assertEqual(eligible['tab'], 'log')
                    os.write(master, b'a4')
                    checkpoint(lambda value: value['tab'] == 'unblock')
                    state['latest_pass']['rows'][0]['state'] = 'ready'
                    path.write_text(json.dumps(state))
                    resumed = checkpoint(lambda value: not value['attention'])
                    self.assertEqual(resumed['tab'], 'log')
                    os.write(master, quit_key)
                    deadline = time.monotonic() + 3
                    while process.poll() is None and time.monotonic() < deadline:
                        drain()
                    self.assertEqual(process.wait(timeout=1), 0, bytes(transcript[-1000:]))
                    drain()
                    self.assertEqual(termios.tcgetattr(slave), modes)
                    self.assertIn(b'\x1b[?1049l', transcript)
                    self.assertEqual(Path(str(proof) + '.closed').read_text(), 'True')
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)
                    os.close(master)
                    os.close(slave)
