"""Flush launch output to the terminal and an append-only, timestamped log."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import sys

from .records import iso, timestamp


class LaunchStream:
    def __init__(self, terminal, log):
        self.terminal = terminal
        self.log = log
        self.pending = ""

    def __getattr__(self, name):
        return getattr(self.terminal, name)

    def write(self, text):
        self.terminal.write(text)
        self.terminal.flush()
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.log.write(f"{iso(timestamp())} {line}\n")
            self.log.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def finish(self):
        if self.pending:
            self.log.write(f"{iso(timestamp())} {self.pending}\n")
            self.pending = ""
        self.flush()


@contextmanager
def launch_output(root):
    local = root / ".ub-agents"
    local.mkdir(mode=0o700, exist_ok=True)
    with (local / "launch.log").open("a", encoding="utf-8") as log:
        stdout = LaunchStream(sys.stdout, log)
        stderr = LaunchStream(sys.stderr, log)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                yield
            finally:
                stdout.finish()
                stderr.finish()
