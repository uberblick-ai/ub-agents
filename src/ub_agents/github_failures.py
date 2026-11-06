"""Bound continuous-launch failures, sharing successes and rate waits across clients."""

from contextlib import contextmanager
import threading
from time import monotonic

from .errors import GitHubRetryExhausted

GITHUB_FAILURE_LIMIT_SECONDS = 2 * 60 * 60


class GitHubFailures:
    def __init__(self):
        self._lock = threading.Lock()
        self._since = None
        self._last_error = None
        self._paused_at = None
        self._paused_seconds = 0
        self._pauses = 0

    def _now(self):
        now = monotonic() if self._paused_at is None else self._paused_at
        return now - self._paused_seconds

    def failed(self, error):
        with self._lock:
            if self._since is None:
                self._since = self._now()
            self._last_error = error

    def succeeded(self):
        with self._lock:
            self._since = None
            self._last_error = None

    def remaining(self):
        with self._lock:
            elapsed = 0 if self._since is None else self._now() - self._since
            remaining = max(0, GITHUB_FAILURE_LIMIT_SECONDS - elapsed)
            if not remaining:
                raise GitHubRetryExhausted(self._last_error) from self._last_error
            return remaining

    @contextmanager
    def pause(self):
        # Overlapping waits exclude their union, and a success during a wait
        # still resets the failure clock for every client.
        with self._lock:
            if not self._pauses:
                self._paused_at = monotonic()
            self._pauses += 1
        try:
            yield
        finally:
            with self._lock:
                self._pauses -= 1
                if not self._pauses:
                    self._paused_seconds += monotonic() - self._paused_at
                    self._paused_at = None
