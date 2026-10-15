"""Opt-in subprocess fixtures; this module is outside unittest discovery."""

import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest


class Results(unittest.TestCase):
    def test_pass(self):
        pass

    def test_late_result(self):
        time.sleep(2.1)

    def test_stderr_noise(self):
        os.write(2, b'stderr from an earlier test\n')

    def test_failure(self):
        print('buffered failure output')
        self.fail('fixture failure')

    def test_error(self):
        raise ValueError('fixture error')

    @unittest.skip('fixture skip')
    def test_skip(self):
        pass

    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass


class Hanging(unittest.TestCase):
    def test_hang(self):
        self.start_children()
        threading.Thread(target=self.waiting_thread, daemon=True).start()
        self.waiting_thread()

    def test_children_finish(self):
        self.start_children()
        self.wait_for_children(Path(os.environ['TEST_RUNNER_RECORD']))

    def wait_for_children(self, directory):
        deadline = time.monotonic() + 10
        while not all((directory / f'children-{detached}').exists() for detached in (False, True)):
            if time.monotonic() >= deadline:
                self.fail('child did not start')
            time.sleep(.01)

    def start_children(self, directory=None):
        directory = directory or Path(os.environ['TEST_RUNNER_RECORD'])
        directory.mkdir(exist_ok=True)
        (directory / 'worker').write_text(str(os.getpid()))
        # Cover an ordinary child plus a child and grandchild that escape the
        # worker session. All ignore SIGTERM, requiring forced cleanup.
        script = '''import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'],
    start_new_session=True)
Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
'''
        for detached in (False, True):
            subprocess.Popen([sys.executable, '-c', script, str(directory / f'children-{detached}')],
                             start_new_session=detached)

    def waiting_thread(self):
        threading.Event().wait(60)

    def test_shutdown(self):
        threading.Thread(target=self.waiting_thread).start()

    def test_crash(self):
        os._exit(7)

    def test_parent_finished(self):
        directory = Path(os.environ['TEST_RUNNER_RECORD']) / 'finished'
        self.start_children(directory)
        self.wait_for_children(directory)
        # Ensure the second worker owns the hung test before this test returns;
        # otherwise the first worker could receive both tests during startup.
        self.wait_for_children(directory.parent / 'running')
        # Registered after the worker's shutdown callback, so this proves the
        # worker reached atexit before the runner is killed.
        atexit.register((directory / 'stopped').touch)

    def test_parent_running(self):
        self.start_children(Path(os.environ['TEST_RUNNER_RECORD']) / 'running')
        self.waiting_thread()

    def test_parent_shutdown(self):
        directory = Path(os.environ['TEST_RUNNER_RECORD']) / 'shutdown'
        self.start_children(directory)
        self.wait_for_children(directory)
        threading.Thread(target=self.waiting_thread).start()
        # Runs before Python joins non-daemon threads, which deliberately hang.
        threading._register_atexit((directory / 'shutting-down').touch)


def __getattr__(name):
    if name == 'discovery_hang':
        threading.Event().wait(60)
    raise AttributeError(name)


def stalled_worker_runner():
    """Replace only the worker command with a stalled protocol fixture."""
    from unittest.mock import patch
    from tests.parallel import main

    popen = subprocess.Popen

    def start(command, **kwargs):
        if command[1:3] == ['-m', 'tests.parallel_worker']:
            script = 'import time; time.sleep(60)'
            if sys.argv[1] != 'startup':
                message = '{"kind":"ready"}\n' if sys.argv[1] == 'blocked_write' else '{"kind":'
                script = f'import os,sys,time; os.write(int(sys.argv[1]), {message.encode()!r}); time.sleep(60)'
            command = [sys.executable, '-c', script, command[3]]
        return popen(command, **kwargs)

    with patch('tests.parallel_worker.subprocess.Popen', side_effect=start):
        return main(sys.argv[2:])


if __name__ == '__main__':
    sys.exit(stalled_worker_runner())
