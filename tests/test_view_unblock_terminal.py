"""Unblock acceptance in an owned real PTY with scripted read-only sources."""

import json
from pathlib import Path
import tempfile
import unittest

from tests.terminal import Terminal
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
                     'state': 'parked', 'reason': 'Approval needed', 'waiting_since': '2026-10-05T12:12:00Z',
                     'stop_labels': ['needs-human'], 'attention_reason': 'Maintainer merge ' * 8},
                    {'item': 179, 'agent': 'worker', 'state': 'blocked', 'reason': 'Attempt limit exhausted',
                     'failures': 3, 'max_attempts': 3},
                    {'item': 180, 'agent': 'worker', 'state': 'ready', 'reason': 'Ready'}]}
                state['coordination_authors'] = AUTHORS
                state['action_needed'] = {'178': {'text': NOTICE, 'author': 'operator',
                                                 'created_at': '2026-10-05T12:12:00Z'}}
                path.write_text(json.dumps(state))
                proof = root / 'proof.json'
                script = '''
import pathlib, sys
from textual.binding import Binding
from textual.widgets import Markdown
from tests.support import RecordingDescriptionTransport
from tests.test_view_unblock import AUTHORS, comment, comments_reply
from ub_agents.view_github import DescriptionLoads, parse_response
from ub_agents.view_ui import ItemTabs, View
from ub_agents.view_unblock import stamp
from ub_agents.view_work import WorkTree
transport = RecordingDescriptionTransport()
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('a', 'select_attention', priority=True),
                Binding('b', 'select_foreign', priority=True),
                Binding('e', 'select_eligible', priority=True),
                Binding('t', 'advance', priority=True), Binding('l', 'complete', priority=True)]
    def action_select_attention(self): self.select('plan:178:worker')
    def action_select_foreign(self): self.select('plan:179:worker')
    def action_select_eligible(self): self.select('plan:180')
    def action_advance(self): self.now += 60
    def action_complete(self):
        transport.response = parse_response(comments_reply(comment()), b'', 0, self.now, 'unblock', AUTHORS)
    def proof_values(self):
        tree = self.query_one(WorkTree)
        tree.get_node_at_line(0)
        node = self.nodes.get('plan:178:worker')
        first = tree.render_line(node._line - tree.scroll_offset.y) if node else None
        second = tree.render_line(node._line + 1 - tree.scroll_offset.y) if node and tree.row_height == 2 else None
        value = {'tab': self.query_one(ItemTabs).active, 'attention': self.unblock_visible,
                 'work': first.text if first else '', 'detail': second.text if second else '',
                 'red_wait': any(segment.style and segment.style.color and
                                segment.style.color.get_truecolor().hex == '#ff8b7f' and
                                ('24m' in segment.text or '25m' in segment.text)
                                for segment in first) if first else False,
                 'selected': self.selected, 'header': self.query_one('#item_header').render().plain,
                 'note': self.query_one('#unblock_note').render().plain,
                 'body': self.query_one('#unblock_body', Markdown).source,
                 'footer': self.query_one('#status').render().plain, 'calls': len(transport.calls),
                 'pending': self.descriptions.pending, 'screen': type(self.screen).__name__,
                 'size': list(self.size), 'narrow': self.narrow, 'item': self.item_view,
                 'modal': self.screen.query_one('#raw_details').render().plain
                          if self.screen.query('#raw_details') else '',
                 'visible': [strip.text for strip in self.screen._compositor.render_strips()]}
        return value
app = ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),
                descriptions=DescriptionLoads(transport, clock=lambda: app.now))
app.now = stamp('2026-10-05T12:36:00Z')
app.run()
pathlib.Path(sys.argv[3] + '.closed').write_text(str(transport.closed))
'''
                with Terminal(script, root, path, proof, proof=proof) as terminal:
                    terminal.checkpoint(lambda value: value['attention'])
                    terminal.send(b'4g')
                    cached = terminal.checkpoint(lambda value: value['tab'] == 'unblock' and 'snapshot' in value['note']
                                        and 'waiting 24m' in value['header'])
                    self.assertIn('⌥178 Blocked candidate', cached['header'])
                    self.assertIn('worker · needs-human · waiting 24m · since ', cached['header'])
                    self.assertTrue(cached['work'].startswith('? ⌥178 '), cached['work'])
                    self.assertTrue(cached['work'].endswith('24m'), cached['work'])
                    self.assertTrue(cached['red_wait'])
                    self.assertTrue(cached['detail'].startswith('  worker · needs-human · Maintainer'))
                    self.assertTrue(cached['detail'].endswith('…'), cached['detail'])
                    self.assertIn('Resolve the blocker', '\n'.join(cached['visible']))
                    self.assertNotIn('**Action needed**', cached['body'])
                    self.assertIn('1-4 tabs g load', cached['footer'])
                    self.assertEqual(cached['calls'], 0)
                    terminal.send(b't?')
                    help_view = terminal.checkpoint(lambda value: value['screen'] == 'KeyHelp')
                    self.assertIn('g on Unblock', help_view['modal'])
                    terminal.send(b'?')
                    advanced = terminal.checkpoint(lambda value: value['screen'] != 'KeyHelp' and 'waiting 25m' in value['header'])
                    self.assertTrue(advanced['work'].endswith('25m'), advanced['work'])
                    # Real resize and Enter/Esc navigation at the minimum width.
                    terminal.resize(60, 16)
                    narrow_work = terminal.checkpoint(lambda value: value['size'] == [60, 16])
                    self.assertTrue(narrow_work['work'].startswith('? ⌥178 '))
                    self.assertTrue(narrow_work['work'].endswith('25m'))
                    self.assertEqual(narrow_work['detail'], '')
                    terminal.send(b'\r')
                    narrow = terminal.checkpoint(lambda value: value['item'] and 'g load' in value['footer'])
                    self.assertIn('1-4 tabs', narrow['footer'])
                    self.assertIn('v', narrow['footer'])
                    terminal.send(b'\x1b')
                    terminal.checkpoint(lambda value: not value['item'])
                    terminal.send(b'\r')
                    terminal.checkpoint(lambda value: value['item'])
                    terminal.send(b'f')
                    paused = terminal.checkpoint(lambda value: 'f follow' in value['footer'])
                    self.assertIn('g load', paused['footer'])
                    self.assertIn('1-4 tabs', paused['footer'])
                    self.assertIn('v', paused['footer'])
                    terminal.resize(110, 32)
                    terminal.checkpoint(lambda value: value['size'] == [110, 32])
                    terminal.send(b'b4g2g')
                    pending = terminal.checkpoint(lambda value: value['pending'] is not None)
                    self.assertEqual(pending['calls'], 1)
                    terminal.send(b'l4')
                    loaded = terminal.checkpoint(lambda value: value['tab'] == 'unblock' and 'GitHub · loaded' in value['note']
                                        and 'failed 3/3' in value['header'])
                    self.assertIn('failed 3/3', loaded['header'])
                    terminal.send(b'g')
                    self.assertEqual(terminal.checkpoint()['calls'], 1)
                    terminal.send(b'e4')
                    eligible = terminal.checkpoint(lambda value: value['selected'] == 'plan:180')
                    self.assertFalse(eligible['attention'])
                    self.assertEqual(eligible['tab'], 'log')
                    terminal.send(b'a4')
                    terminal.checkpoint(lambda value: value['tab'] == 'unblock')
                    state['latest_pass']['rows'][0]['state'] = 'ready'
                    path.write_text(json.dumps(state))
                    resumed = terminal.checkpoint(lambda value: not value['attention'])
                    self.assertEqual(resumed['tab'], 'log')
                    terminal.send(quit_key)
                    terminal.wait_exit()
                    self.assertEqual(Path(str(proof) + '.closed').read_text(), 'True')
