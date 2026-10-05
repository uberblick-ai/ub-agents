"""Expendable, read-only queue planning alongside a supervised assignment."""

import threading
import subprocess
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
            runner = github.runner or subprocess.run
            def request(*args, **kwargs):
                if self.stop.is_set():
                    raise _Cancelled
                return runner(*args, **kwargs)
            github = GitHub(github.repository, request)
        self.lock = threading.Lock()
        self.started = started
        from .loop import Loop
        self.planner = Loop(loop.config, ObservationReads(github, self.stop),
                            loop.coordinator.actor, output=lambda *_: None)
        self.planner.coordinator.clock = loop.coordinator.clock
        self.planner.usage = loop.usage
        self.planner.maintenance = loop.maintenance
        self.thread = threading.Thread(target=self._run, name="run-planning")

    def start(self):
        self.thread.start()

    def cancel(self):
        with self.lock:
            self.stop.set()
            return self.started

    def close(self):
        self.cancel()
        self.thread.join()

    def _run(self):
        elapsed = self.clock() - self.started
        interval, _ = idle_interval(self.loop.github.quota_requests - self.loop._requests_before,
                                    self.planner.config.poll_seconds,
                                    self.loop.github.resource_quotas,
                                    self.planner.coordinator.clock(), elapsed)
        while not self.stop.wait(max(0, self.started + interval - self.clock())):
            with self.lock:
                if self.stop.is_set():
                    return
                self.started = self.clock()
            observed_at = self.planner.coordinator.clock()
            events = PassEvents()
            self.planner.observer = events
            before = self.planner.github.quota_requests
            rate_wait = 0
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
                # Observation failure cannot stop or change the owned run.
            elapsed = self.clock() - self.started
            interval, _ = idle_interval(self.planner.github.quota_requests - before,
                                        self.planner.config.poll_seconds,
                                        self.planner.github.resource_quotas,
                                        self.planner.coordinator.clock(), elapsed)
            interval = max(interval, elapsed + rate_wait)
