"""Worker protocol and bounded process cleanup for the test runner."""

import atexit
import contextlib
from dataclasses import dataclass
import faulthandler
import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time


def worker_main(event_fd, command_fd):
    # Keep diagnostics outside unittest's redirected/buffered streams. The signal
    # handler can dump every Python thread even when the main thread holds the GIL.
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    events = os.fdopen(event_fd, 'w')
    os.set_inheritable(event_fd, False)
    os.set_inheritable(command_fd, False)
    commands = queue.Queue()
    cleanup_lock = threading.Lock()
    stopping = False

    def parent_gone():
        with cleanup_lock:
            stop_process_trees([os.getpid()])

    def receive():
        # Read independently of tests, discovery and interpreter shutdown. EOF
        # means the runner is gone, even if SIGKILL bypassed its cleanup. Keep
        # this worker alive while finding descendants in separate sessions.
        with os.fdopen(command_fd) as stream:
            for line in stream:
                commands.put(line)
        parent_gone()

    threading.Thread(target=receive, daemon=True).start()
    from tests.parallel import discover, run

    def emit(kind, **data):
        try:
            events.write(json.dumps({'kind': kind, **data}) + '\n')
            events.flush()
        except BrokenPipeError:
            # A result can race the EOF watcher after the runner dies. Do not
            # exit and orphan detached descendants before that thread runs.
            parent_gone()

    def stopped():
        if stopping:
            # Python joins non-daemon threads before atexit. Acknowledging here
            # covers interpreter shutdown too. Keep the worker alive until the
            # parent cleans its tree so detached children retain their ancestry.
            emit('stopped')
            while True:
                commands.get()

    atexit.register(stopped)
    # Separate protocol descriptors leave stdin at devnull for tests and their
    # subprocesses, matching tests/__init__.py's non-interactive behavior.
    emit('ready')
    while True:
        command = json.loads(commands.get())
        if command['kind'] == 'stop':
            stopping = True
            return
        if command['kind'] == 'discover':
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                names, outcome = discover(command['names'])
            emit('discovered', names=names, outcome=outcome)
        else:
            name = command['name']
            emit('started', name=name, since=time.monotonic())
            # Save the waiting stacks at the deadline even if the parent's
            # event loop is delayed and receives a late completed result.
            faulthandler.dump_traceback_later(command['timeout'], file=sys.stderr)
            try:
                outcome = run(name)
            finally:
                faulthandler.cancel_dump_traceback_later()
            emit('finished', name=name, outcome=outcome)


@dataclass
class Worker:
    process: subprocess.Popen
    diagnostics: Path
    reader: threading.Thread
    sender: threading.Thread
    commands: queue.Queue
    command_stream: io.TextIOBase
    since: float
    phase: str = 'worker startup'
    name: str | None = None
    ready: bool = False
    closed: bool = False
    complete: bool = False
    diagnostics_offset: int = 0

    @classmethod
    def start(cls, directory, number, events):
        diagnostics = directory / f'worker-{number}.log'
        read_fd, write_fd = os.pipe()
        command_read, command_write = os.pipe()
        since = time.monotonic()
        try:
            with diagnostics.open('w') as log:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'tests.parallel_worker', str(write_fd), str(command_read)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                    start_new_session=True, pass_fds=(write_fd, command_read))
        except BaseException:
            os.close(read_fd)
            os.close(command_write)
            raise
        finally:
            os.close(write_fd)
            os.close(command_read)

        def receive():
            # A daemon reader lets the supervisor keep its deadline even if a
            # worker stalls halfway through writing a result to its pipe.
            try:
                with os.fdopen(read_fd) as stream:
                    for line in stream:
                        events.put((worker, json.loads(line)))
            except (OSError, ValueError) as error:
                events.put((worker, {'kind': 'protocol error', 'error': str(error)}))
            finally:
                events.put((worker, {'kind': 'closed'}))

        commands = queue.Queue()
        command_stream = os.fdopen(command_write, 'w')

        def transmit():
            # Sending discovery arguments must not block the supervisor if the
            # worker stalls before reading, even when the arguments fill a pipe.
            try:
                while True:
                    command = commands.get()
                    if command is None:
                        return
                    command_stream.write(json.dumps(command) + '\n')
                    command_stream.flush()
                    if command['kind'] == 'stop':
                        return
            except (OSError, ValueError) as error:
                events.put((worker, {'kind': 'protocol error', 'error': str(error)}))

        reader = threading.Thread(target=receive, daemon=True)
        sender = threading.Thread(target=transmit, daemon=True)
        worker = cls(process, diagnostics, reader, sender, commands, command_stream, since)
        reader.start()
        sender.start()
        return worker

    def send(self, kind, **data):
        self.phase = {'discover': 'test discovery', 'run': 'test dispatch',
                      'stop': 'worker shutdown'}[kind]
        self.since = time.monotonic()
        self.name = None
        if kind == 'run':
            self.diagnostics_offset = self.diagnostics.stat().st_size
        self.commands.put({'kind': kind, **data})


def dump_workers(workers):
    for worker in workers:
        if worker.ready and worker.process.poll() is None:
            signal_process(worker.process.pid, signal.SIGUSR1)
    # Give the low-level signal handlers a bounded opportunity to write stacks.
    time.sleep(.2)
    for worker in workers:
        with worker.diagnostics.open('rb') as log:
            log.seek(worker.diagnostics_offset)
            text = log.read().decode(errors='replace')
        sys.stderr.write(f'\nWorker {worker.process.pid} ({worker.name or worker.phase}):\n')
        sys.stderr.write(text or 'No Python stack available; worker has not initialized its diagnostic handler.\n')


def signal_process(pid, sig, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin's killpg returns EPERM for a group containing only zombies.
        # Treat it as gone only after inspection confirms no live members;
        # permission failures against a live process must still fail the run.
        if not group or any(pgid == pid and not state.startswith('Z')
                            for _pid, _parent, pgid, state in process_table()):
            raise


def process_table():
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,stat='],
                            capture_output=True, text=True, timeout=1, check=True)
    return [(int(pid), int(parent), int(group), state)
            for pid, parent, group, state in (line.split() for line in result.stdout.splitlines())]


def stop_process_trees(roots):
    """Freeze the owned tree, including detached children, before killing it.

    Worker sessions exist before Python starts, so even a startup stall has an
    owned process group. Tests also start new sessions; inspect descendants while
    the workers still exist rather than losing those children to reparenting.
    """
    owned = set(roots)
    groups = set(owned)
    current = os.getpid()
    own_group = os.getpgrp() if current in owned else None
    for group in groups:
        if group != own_group:
            signal_process(group, signal.SIGSTOP, group=True)
    try:
        # Freezing descendants prevents them from spawning more children during
        # cleanup. Repeat the snapshot to include any fork racing the first one.
        for _ in range(3):
            rows = process_table()
            previous = set(owned)
            while True:
                found = {pid for pid, parent, group, _state in rows if parent in owned or group in groups}
                if found <= owned:
                    break
                owned.update(found)
            for pid, _parent, group, state in rows:
                # A worker cleaning itself cannot freeze its own watchdog.
                if pid in owned and pid != current and not state.startswith('Z'):
                    signal_process(pid, signal.SIGSTOP)
                    if group == pid:
                        groups.add(group)
            if owned == previous:
                break
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.stderr.write(f'\nCould not inspect worker descendants: {error}\n')
    finally:
        for group in groups:
            if group != own_group:
                signal_process(group, signal.SIGKILL, group=True)
        for pid in owned:
            if pid != current:
                signal_process(pid, signal.SIGKILL)
        if own_group is not None:
            # Kill ourselves last, after detached descendants are stopped.
            signal_process(own_group, signal.SIGKILL, group=True)


def stop_workers(workers):
    stop_process_trees([worker.process.pid for worker in workers])
    deadline = time.monotonic() + 3
    for worker in workers:
        try:
            worker.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            sys.stderr.write(f'\nWorker {worker.process.pid} did not exit after SIGKILL.\n')
        worker.commands.put(None)
    deadline = time.monotonic() + 1
    for worker in workers:
        worker.reader.join(timeout=max(0, deadline - time.monotonic()))
        worker.sender.join(timeout=max(0, deadline - time.monotonic()))
        if not worker.sender.is_alive():
            try:
                worker.command_stream.close()
            except BrokenPipeError:
                pass


if __name__ == '__main__':
    worker_main(int(sys.argv[1]), int(sys.argv[2]))
