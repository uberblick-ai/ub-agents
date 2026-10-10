"""One optional idle-sleep assertion owned by an attached launcher's lifetime."""

import os
import subprocess
import sys
import threading


class KeepAwake:
    def __init__(self, publish):
        self.publish = publish
        self.supported = sys.platform == 'darwin'
        self.process = None
        self.closed = False
        self.lock = threading.Lock()

    def state(self, notice=''):
        self.publish({'supported': self.supported, 'enabled': self.process is not None,
                      'notice': notice})

    def toggle(self):
        with self.lock:
            if self.closed:
                return
            if not self.supported:
                self.state('keep awake unavailable on this platform')
            elif self.process is not None:
                self._stop()
                self.state()
            else:
                try:
                    self.process = subprocess.Popen(
                        ['caffeinate', '-i', '-w', str(os.getpid())],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                except FileNotFoundError:
                    self.state('keep awake unavailable: caffeinate not found')
                    return
                except OSError as exc:
                    reason = ' '.join(str(exc).split())[:100]
                    self.state('keep awake unavailable: ' + reason)
                    return
                # A helper that immediately rejects the request never appears on.
                try:
                    code = self.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    code = self.process.poll()
                if code is not None:
                    self.process = None
                    self.state(f'keep awake unavailable: caffeinate exited {code}')
                else:
                    self.state()

    def check(self):
        with self.lock:
            if self.process is not None:
                code = self.process.poll()
                if code is not None:
                    self.process.wait()
                    self.process = None
                    self.state(f'keep awake stopped: caffeinate exited {code}')

    def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None:
                self._stop()
                self.state()
