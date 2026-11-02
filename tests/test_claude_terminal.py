"""Synthetic oversized Claude acceptance in an owned real PTY."""

from pathlib import Path
import tempfile
import unittest

from tests.terminal import Terminal
from tests.test_claude_logs import encoded, image_result
from tests.test_log_reader import record, result, tool
from tests.test_view_data import fixture
from ub_agents.log_format import MAX_RECORD


class ClaudeTerminalTests(unittest.TestCase):
    def test_oversized_image_error_and_raw_access_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, _ = fixture(root)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from ub_agents.view.ui import LogPane, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    def proof_values(self):
        output = self.query_one(LogPane)
        page, snapshot = self.reading.page, self.reading.log
        return {'selected': self.selected, 'raw': self.reading.raw,
                'unread': snapshot.unread_bytes if snapshot else None,
                'pending': snapshot.pending_bytes if snapshot else None,
                'formatted': [r.value.text for r in page.refs if r.value.text] if page else [],
                'styles': [r.value.styles for r in page.refs if r.value.text] if page else [],
                'lines': [strip.text for strip in output.lines],
                'screen': [strip.text for strip in self.screen._compositor.render_strips()],
                'screen_name': type(self.screen).__name__,
                'modal': self.screen.query_one('#raw_details').render().plain
                         if self.screen.query('#raw_details') else ''}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof) as terminal:
                terminal.checkpoint(lambda value: value['selected'] == 'assignment:owned-run'
                                    and value['unread'] == 0)
                call = record(content=[{**tool(name='Read'), 'input': {'file_path': 'synthetic.png'}}])
                log.write_bytes(call)
                terminal.checkpoint(lambda value: any('▸ Read synthetic.png' in text
                                                     for text in value['formatted']))
                raw = encoded(image_result())
                splits = (raw.index(b'QUJD') + MAX_RECORD + 3,
                          raw.index('café'.encode()) + len(b'caf') + 1, len(raw) - 1)
                start = 0
                for end in splits:
                    with log.open('ab') as stream:
                        stream.write(raw[start:end])
                    incomplete = terminal.checkpoint(lambda value: value['unread'] == 0
                        and any('incomplete Claude record omitted' in text for text in value['formatted']),
                        timeout=20)
                    self.assertEqual(len(incomplete['formatted']), 2)
                    self.assertFalse(any('QUJD' in line for line in incomplete['lines']))
                    start = end
                follower = record(content=[{'type': 'text', 'text': 'after image'}])
                with log.open('ab') as stream:
                    stream.write(b'\n' + follower)
                completed = terminal.checkpoint(lambda value: value['unread'] == 0 and value['pending'] == 0
                    and any('after image' in text for text in value['formatted']), timeout=20)
                self.assertEqual(len(completed['formatted']), 2)
                self.assertFalse(any('omitted' in text for text in completed['formatted']))
                self.assertFalse(any('QUJD' in line or '"type"' in line for line in completed['lines']))
                error = {'type': 'user', 'message': {'content': [
                    result(is_error=True, content='first error\n' + 'x' * (MAX_RECORD * 3))]}}
                with log.open('ab') as stream:
                    stream.write(encoded(error) + follower)
                failed = terminal.checkpoint(lambda value: value['unread'] == 0 and value['pending'] == 0
                    and any('✗ Read: first error' in text for text in value['formatted']), timeout=20)
                self.assertEqual(len(failed['formatted']), 4)
                self.assertEqual(failed['styles'][2][-1][2], 'error')
                self.assertTrue(any('✗ Read: first error' in line for line in failed['screen']))
                terminal.send(b'u')
                raw_view = terminal.checkpoint(lambda value: value['raw'])
                self.assertTrue(any('oversized raw' in line for line in raw_view['lines']))
                self.assertTrue(any('QUJD' in line for line in raw_view['lines']))
                terminal.send(b'u')
                terminal.checkpoint(lambda value: not value['raw'] and value['formatted'] == failed['formatted'])
                terminal.send(b'p')
                access = terminal.checkpoint(lambda value: value['screen_name'] == 'RawAccess')
                self.assertIn(str(log), access['modal'])
                self.assertIn('bytes ', access['modal'])
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: value['screen_name'] == 'Screen')
                terminal.send(b'q')
                terminal.wait_exit()
            self.assertEqual(log.read_bytes(), call + raw + follower + encoded(error) + follower)
