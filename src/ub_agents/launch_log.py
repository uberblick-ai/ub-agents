"""Flush launch output to the terminal and a timestamped, size-rotated log."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import os
import stat
import sys
import threading

from .records import iso, timestamp

LOG_CAP_BYTES = 5 * 1024 * 1024


def open_log(local):
    """Serialize rotation and opening across launchers; existing writers retain their fd."""
    if local.resolve() != local.parent.resolve() / local.name:
        raise OSError("Launch log directory redirects")
    lock = os.open(local / "launch.log.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rotated, failure = False, None
        path = local / "launch.log"
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size > LOG_CAP_BYTES:
                path.replace(local / "launch.log.1")
                rotated = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            failure = str(exc)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8"), rotated, failure
    finally:
        os.close(lock)


class LaunchStream:
    def __init__(self, terminal, log, output):
        self.terminal = terminal
        self.log = log
        self.output = output
        self.pending = ""

    def __getattr__(self, name):
        return getattr(self.terminal, name)

    def write(self, text):
        with self.output.lock:
            if not self.output.hidden:
                self.terminal.write(text)
                self.terminal.flush()
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self.log.write(f"{iso(timestamp())} {line}\n")
                self.log.flush()
                if self.output.hidden:
                    self.output.last = (self.terminal, line)
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def finish(self):
        if self.pending:
            self.log.write(f"{iso(timestamp())} {self.pending}\n")
            self.pending = ""
        self.flush()


class LaunchOutput:
    def __init__(self, log):
        self.lock = threading.RLock()
        self.hidden = False
        self.last = None
        self.result = ()
        self.stdout = LaunchStream(sys.stdout, log, self)
        self.stderr = LaunchStream(sys.stderr, log, self)

    def hide(self):
        with self.lock:
            self.hidden = True
            self.last = None

    def refusals(self, lines):
        """Keep a numbered no-op's complete result across terminal restoration."""
        with self.lock:
            self.result = tuple(lines) if self.hidden else ()
            for line in lines:
                self.stdout.write(line + "\n")

    def resume(self, restore=None, final=False):
        with self.lock:
            if restore is not None:
                restore()
            self.hidden = False
            if self.result:
                for line in self.result:
                    self.stdout.terminal.write(line + "\n")
                self.stdout.terminal.flush()
            elif final and self.last is not None:
                terminal, line = self.last
                terminal.write(line + "\n")
                terminal.flush()
            self.last = None
            self.result = ()


@contextmanager
def launch_output(root):
    local = root / ".ub-agents"
    local.mkdir(mode=0o700, exist_ok=True)
    log, rotated, failure = open_log(local)
    with log:
        output = LaunchOutput(log)
        with redirect_stdout(output.stdout), redirect_stderr(output.stderr):
            try:
                if rotated:
                    print("rotated launch.log to launch.log.1 (larger than 5 MB)")
                if failure:
                    print(f"Cannot rotate launch.log: {' '.join(failure.split())}; continuing to launch")
                yield output
            finally:
                output.stdout.finish()
                output.stderr.finish()
