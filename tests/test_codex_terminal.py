"""Synthetic oversized Codex acceptance in an owned real PTY."""

from pathlib import Path
import tempfile
import unittest

from tests.terminal import Terminal
from tests.test_codex_logs import encoded, recorded_item
from tests.test_view_data import fixture
from ub_agents.log_format import MAX_RECORD


class CodexTerminalTests(unittest.TestCase):
    def test_oversized_codex_formatted_raw_pause_history_and_reset_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, _ = fixture(root, runtime='codex:synthetic-model:high')
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from ub_agents.view_ui import LogPane, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    def proof_values(self):
        output = self.query_one(LogPane)
        page, snapshot = self.reading.page, self.reading.log
        return {'follow': self.reading.follow, 'raw': self.reading.raw,
                'selected': self.selected, 'anchor': output.anchor(),
                'start': page.start if page else None,
                'generation': page.generation if page else None,
                'notice': self.reading.notice,
                'unread': snapshot.unread_bytes if snapshot else None,
                'pending': snapshot.pending_bytes if snapshot else None,
                'formatted': [r.value.text for r in page.refs if r.value.text] if page else [],
                'raw_text': [r.value.display(raw=True) for r in page.refs] if page else [],
                'styles': [r.value.styles for r in page.refs if r.value.text] if page else [],
                'lines': [strip.text for strip in output.lines],
                'screen': [strip.text for strip in self.screen._compositor.render_strips()]}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof, size=(150, 40)) as terminal:
                terminal.checkpoint(lambda value: value['selected'] == 'assignment:owned-run'
                                    and value['unread'] == 0)
                failed = recorded_item('command_execution', status='failed')
                failed['item']['aggregated_output'] = 'synthetic line\n✓\n' * 24000
                raw = encoded(failed)
                self.assertGreater(len(raw), MAX_RECORD * 3)
                log.write_bytes(raw[:-1])
                incomplete = terminal.checkpoint(lambda value: value['unread'] == 0
                                  and any('incomplete Codex record omitted' in text
                                          for text in value['formatted']), timeout=20)
                self.assertEqual(len(incomplete['formatted']), 1)
                with log.open('ab') as stream:
                    stream.write(b'\n' + encoded(recorded_item('agent_message')))
                completed = terminal.checkpoint(lambda value: value['unread'] == 0 and value['pending'] == 0
                                  and any('✗ Exit code 7: synthetic line' in text
                                          for text in value['formatted']), timeout=20)
                self.assertEqual(len(completed['formatted']), 2)
                self.assertEqual(completed['styles'][0][-1][2], 'error')
                self.assertTrue(any('✗ Exit code 7: synthetic line' in line for line in completed['screen']))
                self.assertFalse(any('aggregated_output' in line for line in completed['lines']))
                terminal.send(b'f')
                paused = terminal.checkpoint(lambda value: not value['follow'])
                terminal.send(b'u')
                raw_view = terminal.checkpoint(lambda value: value['raw'])
                self.assertTrue(any('oversized raw fragment' in line for line in raw_view['lines']))
                self.assertTrue(any('aggregated_output' in text for text in raw_view['raw_text']))
                terminal.send(b'u')
                restored = terminal.checkpoint(lambda value: not value['raw'])
                self.assertEqual(restored['selected'], paused['selected'])
                self.assertEqual(restored['anchor'], paused['anchor'])
                terminal.send(b'f')
                terminal.checkpoint(lambda value: value['follow'])
                # Replacement attaches at the tail of an existing large record,
                # leaving bounded older pages entirely inside its output.
                replacement = log.with_suffix('.next')
                replacement.write_bytes(raw + encoded(recorded_item('agent_message')))
                replacement.replace(log)
                tail = terminal.checkpoint(lambda value: value['generation'] > completed['generation']
                                and value['unread'] == 0
                                and any('earlier Codex output omitted' in text for text in value['formatted']))
                self.assertGreater(tail['start'], 0)
                self.assertEqual(len(tail['formatted']), 3)  # Reset marker, omission, assistant.
                terminal.send(b'h')
                older = terminal.checkpoint(lambda value: value['start'] < tail['start']
                                  and any('Codex record omitted' in text for text in value['formatted']))
                self.assertEqual(len(older['formatted']), 1)
                self.assertFalse(any('aggregated_output' in line for line in older['lines']))
                terminal.send(b'u')
                terminal.checkpoint(lambda value: value['raw'] and value['start'] == older['start'])
                terminal.send(b'uf')
                latest = terminal.checkpoint(lambda value: value['follow'] and not value['raw']
                                  and value['formatted'] == tail['formatted'])
                self.assertEqual(latest['selected'], completed['selected'])
                log.write_bytes(encoded(recorded_item('agent_message')))
                reset = terminal.checkpoint(lambda value: value['generation'] > tail['generation']
                                  and value['unread'] == 0)
                self.assertEqual(reset['selected'], completed['selected'])
                self.assertFalse(any('synthetic line' in text for text in reset['formatted']))
                terminal.send(b'q')
                terminal.wait_exit()
            self.assertEqual(log.read_bytes(), encoded(recorded_item('agent_message')))
