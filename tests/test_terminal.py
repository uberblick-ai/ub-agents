"""Regression checks for the shared PTY protocol, timing and ownership."""

import os
from pathlib import Path
import tempfile
import unittest

from tests.terminal import Terminal


PROOF_APP = '''
import os, pathlib, sys
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static
class Pane(Static):
    def on_resize(self):
        self.call_after_refresh(self.adjust)
    def adjust(self):
        self.styles.padding = 1 if self.region.width < 90 else 2
class ProofApp(checkpoint_view(App, sys.argv[1])):
    CSS = 'Pane { width: 100%; height: 100%; }'
    BINDINGS = [Binding('q', 'quit', priority=True)]
    def compose(self) -> ComposeResult:
        yield Pane('ready')
    def on_mount(self):
        self.keys = []
    def on_key(self, event):
        if event.key in ('escape', 'a') or event.key.startswith('alt+'):
            self.keys.append(event.key)
    def proof_values(self):
        pane = self.query_one(Pane)
        return {'keys': self.keys, 'region': list(pane.region.size),
                'content_width': pane.content_region.width,
                'argument': sys.argv[2], 'environment': os.environ['PROOF_ENV'],
                'term': os.environ['TERM']}
ProofApp().run()
'''


class TerminalHarnessTests(unittest.TestCase):
    def test_escape_delay_from_spawned_environment_prevents_alt_keys(self):
        for delay in ('1', '200', 'invalid', None):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                proof = Path(directory) / 'proof.json'
                with Terminal(PROOF_APP, proof, 'argument', proof=proof,
                              env={'ESCDELAY': delay, 'PROOF_ENV': 'extra'}) as terminal:
                    initial = terminal.checkpoint()
                    self.assertEqual(initial['argument'], 'argument')
                    self.assertEqual(initial['environment'], 'extra')
                    self.assertEqual(initial['term'], 'xterm-256color')
                    terminal.send(b'\x1b')
                    terminal.send(b'a')
                    self.assertEqual(terminal.checkpoint()['keys'], ['escape', 'a'])
                    terminal.send(b'\x1b')
                    self.assertEqual(terminal.checkpoint()['keys'], ['escape', 'a', 'escape'])
                    terminal.send(b'q')
                    terminal.wait_exit()

    def test_resize_checkpoint_includes_deferred_widget_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / 'proof.json'
            with Terminal(PROOF_APP, proof, 'argument', proof=proof,
                          size=(110, 32), env={'PROOF_ENV': 'extra'}) as terminal:
                terminal.checkpoint()
                for width, height in ((80, 24), (120, 36), (60, 16), (110, 32)):
                    terminal.resize(width, height)
                    # No readiness predicate hides an unsettled rendered region.
                    value = terminal.checkpoint()
                    self.assertEqual(value['region'], [width, height])
                    self.assertEqual(value['content_width'], width - (2 if width < 90 else 4))
                terminal.send(b'q')
                terminal.wait_exit()

    def test_failure_drains_transcript_and_closes_owned_process_and_descriptors(self):
        with self.assertRaisesRegex(AssertionError, 'owned marker') as caught:
            with Terminal("import time; print('owned marker', flush=True); time.sleep(60)") as terminal:
                terminal.expect(b'owned marker')
                terminal.wait_for(lambda: False, timeout=0.01)
        self.assertIn('owned marker', '\n'.join(caught.exception.__notes__))
        self.assertIsNotNone(terminal.process.poll())
        for descriptor in (terminal.master, terminal.slave):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_exit_drains_large_output_and_checks_expected_status(self):
        script = "print('x' * 200000); print('final marker\\x1b[?1049l\\x1b[?25h'); raise SystemExit(7)"
        with Terminal(script) as terminal:
            terminal.wait_exit(7)
            self.assertIn(b'final marker', terminal.transcript)
