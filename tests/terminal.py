"""Owned real-terminal acceptance helpers, for both the test and its app.

Use Terminal as a context manager. Script arguments retain their usual sys.argv
positions under python -P; the prelude makes test helpers importable and restores
stdin after tests/__init__.py detaches it. Subclass checkpoint_view(View, path)
and implement proof_values() to record app state after pending layout work.
"""

import errno
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


class Terminal:
    def __init__(self, script, *arguments, size=(110, 32), python=sys.executable,
                 env=None, proof=None, tmux=False):
        self.size = size
        self.proof = proof
        self.transcript = bytearray()
        self._offset = 0
        self._files = tempfile.TemporaryDirectory()
        self._delay_path = Path(self._files.name) / 'escape-delay'
        self.master, self.slave = pty.openpty()
        self.modes = termios.tcgetattr(self.slave)
        self.process = None
        self._tmux = tmux
        environment = dict(os.environ, TERM='xterm-256color', ESCDELAY='25')
        if env:
            for key, value in env.items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
        # Read the actual constant in the selected interpreter and environment,
        # including Textual's default and validation of ESCDELAY.
        prelude = f'''
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from tests.terminal import checkpoint_view
sys.stdin = sys.__stdin__
from pathlib import Path as _ProofPath
from textual.constants import ESCAPE_DELAY as _escape_delay
_ProofPath({str(self._delay_path)!r}).write_text(str(_escape_delay))
'''
        try:
            self._set_size(*size)
            command = [str(python), '-P', '-c', prelude + script, *map(str, arguments)]
            if tmux:
                # A private socket and config isolate this test from operator sessions.
                # A relative socket name also works with deeply nested scratch paths.
                command = ['tmux', '-S', 'tmux', '-f', '/dev/null',
                           'new-session', *command, ';', 'set-option', '-g', 'mouse', 'on',
                           ';', 'set-option', '-g', 'status', 'off']
            self.process = subprocess.Popen(
                command,
                stdin=self.slave, stdout=self.slave, stderr=self.slave,
                start_new_session=True, env=environment,
                cwd=self._files.name if tmux else None)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        try:
            self.close()
        finally:
            if error is not None:
                error.add_note(self._diagnostic())

    def _diagnostic(self, message='Terminal transcript tail'):
        return f'{message}\n{bytes(self.transcript[-3000:])!r}'

    def _read(self, timeout=0):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.transcript.extend(os.read(self.master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:  # Linux PTY EOF.
                    raise

    def wait_for(self, condition, timeout=5, message='Terminal condition timed out'):
        """Wait for a transcript or external-state predicate while draining output."""
        deadline = time.monotonic() + timeout
        while not condition():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(self._diagnostic(message))
            self._read(min(0.01, remaining))

    def expect(self, *expected, timeout=5):
        """Wait for bytes/predicates in output since the previous expect call."""
        def met():
            output = bytes(self.transcript[self._offset:])
            return all(value(output) if callable(value) else value in output for value in expected)
        self.wait_for(met, timeout, f'Expected terminal output: {expected!r}')
        output = bytes(self.transcript[self._offset:])
        self._offset = len(self.transcript)
        return output

    def send(self, keys):
        """Send keys; a standalone Escape is resolved before any subsequent input.

        Escape sequences (arrows, Page Up, etc.) stay intact. Send a lone Escape
        separately; the delay is measured by the spawned app. Textual's POSIX
        driver ticks the parser on a 100 ms selector poll. Allow two complete
        delay-and-poll intervals so the input thread also gets scheduling time.
        """
        if keys == b'\x1b':
            self.wait_for(self._delay_path.exists)
            delay = float(self._delay_path.read_text())
            os.write(self.master, keys)
            deadline = time.monotonic() + 2 * (delay + 0.1)
            while time.monotonic() < deadline:
                self._read(max(0, deadline - time.monotonic()))
        else:
            os.write(self.master, keys)

    def _set_size(self, width, height):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))

    def resize(self, width, height):
        self.size = (width, height)
        self._set_size(width, height)
        self.process.send_signal(signal.SIGWINCH)

    def checkpoint(self, condition=lambda value: True, timeout=5):
        """Request atomic proofs until app size and the supplied predicate match."""
        if self.proof is None:
            raise ValueError('A proof path is required for checkpoints')
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            self.proof.unlink(missing_ok=True)
            self.send(b'x')
            try:
                self.wait_for(self.proof.exists, max(0, deadline - time.monotonic()),
                              'Checkpoint was not written')
            except AssertionError as error:
                if value is not None:
                    error.add_note(f'Last checkpoint value: {value!r}')
                raise
            proof = json.loads(self.proof.read_text())
            value = proof['value']
            if proof['size'] == list(self.size) and condition(value):
                return value
        raise AssertionError(self._diagnostic(f'Checkpoint condition timed out: {value!r}'))

    def assert_restored(self, sequences=True):
        if termios.tcgetattr(self.slave) != self.modes:
            raise AssertionError(self._diagnostic('Terminal modes were not restored'))
        if sequences:
            for sequence in (b'\x1b[?1049l', b'\x1b[?25h'):
                if sequence not in self.transcript:
                    raise AssertionError(self._diagnostic(f'Missing restore sequence {sequence!r}'))

    def wait_exit(self, expected=0, timeout=5, sequences=True):
        """Drain even crash tracebacks, then check exit status and restoration."""
        self.wait_for(lambda: self.process.poll() is not None, timeout, 'Process did not exit')
        while select.select([self.master], [], [], 0)[0]:
            self._read()
        if self.process.wait() != expected:
            raise AssertionError(self._diagnostic(
                f'Expected exit {expected}, got {self.process.returncode}'))
        self.assert_restored(sequences)

    def close(self):
        try:
            if self._tmux:
                subprocess.run(['tmux', '-S', 'tmux', 'kill-server'], cwd=self._files.name,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if self.process is not None and self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
                try:
                    self.wait_for(lambda: self.process.poll() is not None, timeout=5)
                except AssertionError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        finally:
            os.close(self.master)
            os.close(self.slave)
            self._files.cleanup()


def checkpoint_view(base, path):
    """Create an app base with one proof binding and a shared layout barrier."""
    from textual.binding import Binding

    class CheckpointView(base):
        BINDINGS = [Binding('x', 'checkpoint', priority=True)]

        def action_checkpoint(self):
            self.call_after_refresh(self._checkpoint_widgets)

        def _checkpoint_widgets(self):
            # Resize can enqueue widget callbacks (notably LogPane reflow) after
            # the app's refresh. Cross every widget's queue and refresh before
            # recording; all callbacks share a frame rather than waiting serially.
            widgets = list(self.query('*'))
            geometry = self._checkpoint_geometry()
            pending = len(widgets)
            def refreshed():
                nonlocal pending
                pending -= 1
                if pending == 0:
                    self._write_checkpoint(geometry)
            if not pending:
                self._write_checkpoint(geometry)
            for widget in widgets:
                if not widget.call_after_refresh(refreshed):
                    refreshed()

        def _checkpoint_geometry(self):
            # Hidden tabs can ingest live output throughout the barrier. Their
            # virtual size need not settle to prove the displayed layout.
            return (self.size, tuple((widget, region, clip, widget.virtual_size)
                                     for screen in self.screen_stack
                                     for widget, (region, clip) in screen._compositor.visible_widgets.items()))

        def _write_checkpoint(self, geometry):
            # Reflow may request another layout (e.g. a scrollbar). Include
            # background screens too, since a modal can cover resized panes.
            if (geometry != self._checkpoint_geometry()
                    or any(screen._layout_required or screen._scroll_required
                           or screen._compositor.size != self.size for screen in self.screen_stack)):
                self.call_after_refresh(self._checkpoint_widgets)
                return
            proof = Path(path)
            staging = proof.with_suffix('.new')
            staging.write_text(json.dumps({'size': list(self.size), 'value': self.proof_values()}))
            staging.replace(proof)

    return CheckpointView
