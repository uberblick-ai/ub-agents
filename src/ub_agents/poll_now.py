"""Optional launcher poll requests, accepted only by an idle poll waiter."""

from contextlib import contextmanager
import threading
from time import monotonic

COOLDOWN_SECONDS = 10


class PollNow:
    def __init__(self, status, wall_clock, clock=monotonic):
        self.status, self.wall_clock, self.clock = status, wall_clock, clock
        self.lock = threading.Lock()
        self.waiter = None
        self.next_allowed = 0
        self.cooldown_until = None
        self.limits = {}

    def _status(self):
        self.status(self.cooldown_until, max(self.limits.values(), default=None))

    def request(self):
        with self.lock:
            if self.waiter is None or self.limits:
                return
            stop, wake = self.waiter
            if stop.is_set() or wake.is_set():
                return
            remaining = self.next_allowed - self.clock()
            if remaining > 0:
                self.cooldown_until = self.wall_clock() + remaining
                self._status()
                return
            self.next_allowed = self.clock() + COOLDOWN_SECONDS
            self.cooldown_until = None
            self._status()
            wake.set()

    def wait(self, stop, delay, update=None, on_request=None):
        """Wake for one request or shutdown; retain no requests outside this wait."""
        if delay <= 0 or stop.is_set():
            return stop.is_set()
        wake = threading.Event()
        waiter = (stop, wake)
        with self.lock:
            if stop.is_set():
                return True
            self.waiter = waiter
        try:
            deadline = self.clock() + delay
            next_update = self.clock() + 1
            while not stop.is_set():
                remaining = deadline - self.clock()
                if remaining <= 0 or wake.wait(min(0.1, remaining)):
                    break
                if update is not None and self.clock() >= next_update:
                    update()
                    next_update = self.clock() + 1
            if wake.is_set() and not stop.is_set() and on_request is not None:
                on_request()
            return stop.is_set()
        finally:
            with self.lock:
                if self.waiter is waiter:
                    self.waiter = None

    @contextmanager
    def rate_limit(self, until):
        token = object()
        with self.lock:
            self.limits[token] = until
            self._status()
        try:
            yield
        finally:
            with self.lock:
                del self.limits[token]
                self._status()
