"""Capture runtime streams separately while retaining the combined process log."""

from contextlib import contextmanager, ExitStack
import os
import subprocess


class ProcessOutput:
    def __init__(self, log, stdout, stderr, readers=()):
        self.log, self.stdout, self.stderr, self.readers = log, stdout, stderr, readers

    def poll(self):
        for reader in self.readers:
            # Bound this copy to bytes already written so a noisy child cannot
            # keep supervision from checking shutdown and ownership deadlines.
            remaining = max(0, os.fstat(reader.fileno()).st_size - reader.tell())
            while remaining:
                chunk = reader.read(min(remaining, 65536))
                if not chunk:
                    break
                self.log.write(chunk)
                remaining -= len(chunk)
        self.log.flush()


@contextmanager
def capture_streams(run_dir, separate=False):
    with ExitStack() as stack:
        log = stack.enter_context((run_dir / "process.log").open("wb"))
        if separate:
            paths = [run_dir / name for name in ("stdout.log", "stderr.log")]
            writers = [stack.enter_context(path.open("wb", buffering=0)) for path in paths]
            readers = [stack.enter_context(path.open("rb")) for path in paths]
            output = ProcessOutput(log, *writers, readers)
        else:
            output = ProcessOutput(log, log, subprocess.STDOUT)
        try:
            yield output
        finally:
            output.poll()
