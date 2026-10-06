"""Supported optional view entrypoint. UI imports occur only after attachment."""

import argparse
from pathlib import Path
import select
import signal
import socket
import sys
import threading
import time

from . import __version__
from .view_data import choose_session, load_session


def attach(root, session_id, base_version, wait=2):
    if base_version != __version__:
        raise ValueError(f'UI/base version mismatch: UI {__version__}, launcher {base_version}')
    path, _ = choose_session(root, session_id)
    deadline = time.monotonic() + wait
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    session = load_session(path)
    if session.error:
        raise ValueError(f'session {session_id} unavailable: {session.error}')
    if session.data.get('session') != session_id:
        raise ValueError('session identity mismatch')
    if session.data.get('base_version') != base_version:
        raise ValueError('session UI/base version mismatch')
    if session.state() not in {'live', 'idle'}:
        raise ValueError(f'session {session_id} is {session.state()}')
    return path


class LauncherConnection:
    def __init__(self, descriptor):
        self.channel = socket.socket(fileno=descriptor)
        self.stopping = threading.Event()
        self.thread = None

    def send(self, value):
        try:
            self.channel.sendall(value.encode('utf-8')[:500] + b'\n')
        except OSError:
            pass

    def mounted(self, app):
        def watch():
            while not self.stopping.is_set():
                if select.select([self.channel], [], [], 0.1)[0]:
                    self.channel.recv(1024)  # stop or EOF; never another action
                    try:
                        app.call_from_thread(app.exit)
                    except RuntimeError:
                        pass
                    return
        self.thread = threading.Thread(target=watch, name='launcher-lifetime', daemon=True)
        self.thread.start()
        self.send('ready')

    def interrupt(self):
        self.send('interrupt')

    def poll(self):
        self.send('poll')

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            # call_from_thread may wait for the UI loop during unmount.
            self.thread.join(timeout=0.15)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only terminal view of one local launcher.')
    parser.add_argument('control_checkout', type=Path, nargs='?')
    parser.add_argument('--session', help='Exact session ID (required when launched by ub-agents)')
    parser.add_argument('--version', action='version', version=f'ub-agents-ui {__version__}')
    parser.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--base-version', help=argparse.SUPPRESS)
    parser.add_argument('--launcher-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe:
        if args.base_version != __version__:
            print(f'UI/base version mismatch: UI {__version__}, launcher {args.base_version}')
            return 2
        return 0
    if args.control_checkout is None:
        parser.error('control_checkout is required')
    connection = LauncherConnection(args.launcher_fd) if args.launcher_fd is not None else None
    handlers = {}
    driver_output = sys.__stderr__
    try:
        root = args.control_checkout.absolute()
        if connection is not None:
            if not args.session:
                raise ValueError('launcher session is missing')
            path = attach(root, args.session, args.base_version)
        else:
            path, listing = choose_session(root, args.session)
            if path is None:
                print('Specify --session ID. Available local sessions:')
                print('\n'.join(listing) if listing else '(none)')
                return 0
        from .view_ui import View
        app = View(root, path, launcher=connection)
        if connection is not None:
            # Textual's POSIX driver draws to __stderr__. The launcher's UI
            # surface is stdout; ordinary error output stays off that surface.
            sys.__stderr__ = sys.__stdout__
            handlers = {sig: signal.signal(sig, lambda *_: app.exit())
                        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        app.run()
        return app.return_code or 0
    except (OSError, ValueError, ModuleNotFoundError) as exc:
        detail = ' '.join(str(exc).split())[:300]
        if connection is not None:
            connection.send('error ' + detail)
        else:
            print(f'Terminal view unavailable: {detail}')
        return 1
    finally:
        sys.__stderr__ = driver_output
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if connection is not None:
            connection.close()
            connection.channel.close()


if __name__ == '__main__':
    raise SystemExit(main())
