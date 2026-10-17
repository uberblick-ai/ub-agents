"""Record signal requests immediately; wake lock-taking waiters on another thread."""

import os
import threading


class SignalEvent(threading.Event):
    def __init__(self):
        super().__init__()
        self.requested = False

    def request(self):
        # A Python signal handler can interrupt Event.wait while it owns the
        # condition lock. Only record intent here; SignalRelay calls set().
        self.requested = True

    def is_set(self):
        return self.requested or super().is_set()

    def wait(self, timeout=None):
        if not self.is_set():
            super().wait(timeout)
        return self.is_set()

    def clear(self):
        self.requested = False
        super().clear()


class SignalRelay:
    def __init__(self, callbacks):
        self.callbacks = callbacks
        self.requests = dict.fromkeys(callbacks, 0)
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.writer, False)
        # close() joins the worker. If startup itself is interrupted before the
        # caller owns this relay, the worker must not keep Python alive.
        self.thread = threading.Thread(target=self.run, name="launcher-signals", daemon=True)
        self.thread.start()

    def request(self, key):
        self.requests[key] += 1
        try:
            os.write(self.writer, b"\0")
        except BlockingIOError:
            # A full pipe already wakes the reader. Counters preserve requests
            # for every signal, including a different signal arriving last.
            pass

    def run(self):
        seen = dict.fromkeys(self.callbacks, 0)
        while True:
            data = os.read(self.reader, 4096)
            for key, callback in self.callbacks.items():
                count = self.requests[key]
                if count != seen[key]:
                    seen[key] = count
                    callback()
            if not data:
                return

    def close(self):
        # Restore handlers first. EOF drains all recorded requests before the
        # observer and its publisher are closed by the launcher.
        os.close(self.writer)
        self.thread.join()
        os.close(self.reader)
