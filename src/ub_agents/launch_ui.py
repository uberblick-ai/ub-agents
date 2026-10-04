"""Optional terminal child supervision; no UI dependency imports in the launcher."""

import os
import select
import signal
import socket
import subprocess
import sys
import termios
import threading

from . import __version__

RESTORE = ('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l'
           '\x1b[?2004l\x1b[?1049l\x1b[?25h\x1b[0m')


def ui_command():
    return [sys.executable, '-P', '-m', 'ub_agents.view']


def open_view(root, session, output, stop, *, no_ui=False):
    if no_ui or not sys.stdin.isatty() or not output.stdout.isatty():
        return None
    command = ui_command()
    try:
        probe = subprocess.run([*command, '--probe', '--base-version', __version__],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
        if probe.returncode:
            raise ValueError(probe.stdout.strip() or 'incompatible UI/base version')
        if session is None:
            raise ValueError('launcher session is missing')
        view = ViewProcess(command, root, session, output)
        view.start(stop)
        return view
    except (OSError, ValueError, termios.error, subprocess.TimeoutExpired) as exc:
        print(f'Terminal view unavailable: {" ".join(str(exc).split())[:300]}; continuing with plain output')
        return None


class ViewProcess:
    def __init__(self, command, root, session, output):
        self.command, self.root, self.session, self.output = command, root, session, output
        self.process = self.channel = None
        self.closing = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.error = None
        self.modes = None
        self.winch_handler = None

    def start(self, stop):
        child = None
        try:
            self.modes = termios.tcgetattr(sys.stdin.fileno())
            self.channel, child = socket.socketpair()
            self.output.hide()
            self.process = subprocess.Popen(
                [*self.command, str(self.root), '--session', self.session,
                 '--base-version', __version__, '--launcher-fd', str(child.fileno())],
                pass_fds=(child.fileno(),), stdin=sys.stdin, stdout=self.output.stdout.terminal,
                stderr=subprocess.DEVNULL, start_new_session=True)
            def resize(*_):
                if self.process.poll() is None:
                    try:
                        os.kill(self.process.pid, signal.SIGWINCH)
                    except ProcessLookupError:
                        pass
            self.winch_handler = signal.signal(signal.SIGWINCH, resize)
            self.thread = threading.Thread(target=self.monitor, name='terminal-view-supervisor')
            self.thread.start()
            # Initial snapshot publication and UI startup are bounded. Signals
            # remain handled by the launcher during this wait.
            for _ in range(80):
                if self.ready.wait(0.05) or stop.is_set():
                    break
            else:
                self.error = 'startup timed out'
                self.close()
                print('Terminal view unavailable: startup timed out; continuing with plain output')
        except BaseException:
            self.close()
            raise
        finally:
            if child is not None:
                child.close()

    def restore(self):
        try:
            if self.modes is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, self.modes)
            self.output.stdout.terminal.write(RESTORE)
            self.output.stdout.terminal.flush()
        except (OSError, ValueError, termios.error):
            pass

    def monitor(self):
        pending = b''
        code = None
        try:
            while True:
                if select.select([self.channel], [], [], 0.05)[0]:
                    data = self.channel.recv(1024)
                    if not data:
                        break
                    pending += data
                    while b'\n' in pending:
                        message, pending = pending.split(b'\n', 1)
                        if message == b'ready':
                            self.ready.set()
                        elif message == b'interrupt':
                            os.kill(os.getpid(), signal.SIGINT)
                        elif message.startswith(b'error '):
                            self.error = message[6:].decode('utf-8', errors='replace')[:300]
                elif self.process.poll() is not None:
                    break
            code = self.process.wait()
        finally:
            closing = self.closing.is_set()
            self.output.resume(self.restore, final=closing)
            if not closing and (self.error or code):
                detail = self.error or f'view exited {code}'
                print(f'Terminal view closed: {" ".join(detail.split())}; continuing with plain output')
            self.ready.set()

    def close(self):
        self.closing.set()
        if self.winch_handler is not None:
            signal.signal(signal.SIGWINCH, self.winch_handler)
            self.winch_handler = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.channel.sendall(b'stop\n')
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.thread is not None:
            self.thread.join(timeout=5)
        elif self.modes is not None:
            self.output.resume(self.restore)
        if self.channel is not None:
            self.channel.close()
