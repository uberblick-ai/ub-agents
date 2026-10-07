"""Expendable, read-only queue planning alongside a supervised assignment."""

import threading
import subprocess
from copy import deepcopy
from time import monotonic

from .errors import GitHubError
from .github import GitHub, RATE_LIMIT_FALLBACK_SECONDS, RATE_LIMIT_MAX_SECONDS
from .polling import idle_interval
from .rate_limits import READS


class _Cancelled(Exception):
    pass


class ObservationReads:
    """Forbid mutations and stop between reads, without touching the run's client."""

    def __init__(self, github, stop):
        self.github, self.stop = github, stop

    def __getattr__(self, name):
        if name in {"repository", "quota_requests", "resource_quotas"}:
            return getattr(self.github, name)
        if name not in READS:
            raise AttributeError(name)

        def read(*args, **kwargs):
            if self.stop.is_set():
                raise _Cancelled
            return getattr(self.github, name)(*args, **kwargs)
        return read


class PassEvents:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *args: self.events.append((name, args))


class RunPlanning:
    def __init__(self, loop, started, clock=None):
        self.loop = loop
        self.clock = clock or (lambda: monotonic())
        # A production adapter has private caches, counters and rate-limit state.
        # Recording adapters retain their shared durable store in tests.
        github = loop.github.github
        self.stop = threading.Event()
        if isinstance(github, GitHub):
            source = github
            runner = github.runner or subprocess.run
            def request(*args, **kwargs):
                if self.stop.is_set():
                    raise _Cancelled
                return runner(*args, **kwargs)
            github = GitHub(github.repository, request)
            github.resource_quotas = deepcopy(source.resource_quotas)
            github._etag_cache = deepcopy(source._etag_cache)
            github._comment_cache = deepcopy(source._comment_cache)
            github._comment_since = source._comment_since
        self.lock = threading.Lock()
        self.refreshing = False
        self.started = started
        from .loop import Loop
        self.planner = Loop(loop.config, ObservationReads(github, self.stop),
                            loop.coordinator.actor, output=lambda *_: None)
        # Retain discovery inputs, not the launcher's client or pass-local state.
        for name in ("items", "closed_items", "comments_index", "cache"):
            setattr(self.planner.discovery, name, deepcopy(getattr(loop.discovery, name)))
        self.planner.coordinator.clock = loop.coordinator.clock
        self.planner.usage = loop.usage
        self.planner.maintenance = loop.maintenance
        self.thread = threading.Thread(target=self._run, name="run-planning")

    def start(self):
        self.thread.start()

    def cancel(self):
        with self.lock:
            self.stop.set()
            self._finish_refresh()
            return self.started

    def close(self):
        self.cancel()
        self.thread.join()

    def _requested(self):
        with self.lock:
            if not self.stop.is_set():
                self.refreshing = True
                self.loop._observe("poll_refresh", True)

    def _finish_refresh(self):
        # Called under the worker lock, including cancellation while a read is
        # still blocked. Its eventual return must not clear a newer poll's state.
        if self.refreshing:
            self.refreshing = False
            self.loop._observe("poll_refresh", False)

    def _wait(self, delay, rate_until):
        control = self.loop.poll_now
        if control is None:
            return self.stop.wait(delay)
        deadline = self.clock() + delay
        if rate_until is not None:
            limited_delay = min(delay, max(0, rate_until - self.planner.coordinator.clock()))
            if limited_delay:
                with control.rate_limit(rate_until):
                    if self.stop.wait(limited_delay):
                        return True
        return control.wait(self.stop, max(0, deadline - self.clock()), on_request=self._requested)

    def _run(self):
        elapsed = self.clock() - self.started
        interval, _ = idle_interval(self.loop.github.quota_requests - self.loop._requests_before,
                                    self.planner.config.poll_seconds,
                                    self.loop.github.resource_quotas,
                                    self.planner.coordinator.clock(), elapsed)
        rate_until = None
        while not self._wait(max(0, self.started + interval - self.clock()), rate_until):
            with self.lock:
                if self.stop.is_set():
                    return
                self.started = self.clock()
            observed_at = self.planner.coordinator.clock()
            events = PassEvents()
            self.planner.observer = events
            before = self.planner.github.quota_requests
            rate_wait = 0
            rate_until = None
            try:
                # Exhaust the ranked queue. No tick, recovery, parking or runtime
                # maintenance is reachable through this path.
                for _ in self.planner.iter_plans():
                    if self.stop.is_set():
                        return
                with self.lock:
                    if not self.stop.is_set():
                        self.loop._observe("observation_pass", observed_at, events.events)
            except _Cancelled:
                return
            except KeyboardInterrupt:
                return
            except Exception as exc:
                if isinstance(exc, GitHubError) and exc.rate_limited:
                    now = self.planner.coordinator.clock()
                    reset = exc.reset_at if exc.reset_at is not None else now + RATE_LIMIT_FALLBACK_SECONDS
                    rate_wait = min(RATE_LIMIT_MAX_SECONDS, max(0, reset - now))
                    rate_until = now + rate_wait
                # Observation failure cannot stop or change the owned run.
            finally:
                with self.lock:
                    self._finish_refresh()
            elapsed = self.clock() - self.started
            interval, _ = idle_interval(self.planner.github.quota_requests - before,
                                        self.planner.config.poll_seconds,
                                        self.planner.github.resource_quotas,
                                        self.planner.coordinator.clock(), elapsed)
            interval = max(interval, elapsed + rate_wait)
