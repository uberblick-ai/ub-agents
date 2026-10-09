"""Expendable, read-only queue planning alongside a supervised assignment."""

import threading
import subprocess
from copy import deepcopy
from time import monotonic

from .errors import GitHubError
from .github import GitHub, RATE_LIMIT_FALLBACK_SECONDS, RATE_LIMIT_MAX_SECONDS
from .polling import IDLE_MAX_SECONDS, idle_interval
from .rate_limits import READS
from .request_stats import COUNTERS


class _Cancelled(Exception):
    pass


class ObservationReads:
    """Forbid mutations and stop between reads, without touching the run's client."""

    def __init__(self, github, stop):
        self.github, self.stop = github, stop

    def __getattr__(self, name):
        if name in {"repository", "resource_quotas", "comment_window_start", *COUNTERS}:
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
            github.comment_window_start = source.comment_window_start
        self.lock = threading.Lock()
        self.refreshing = False
        self.started = started
        self.rate_until = None
        from .loop import Loop
        self.planner = Loop(loop.config, ObservationReads(github, self.stop),
                            loop.coordinator.actor, output=lambda *_: None)
        self.planner.discovery_budget = loop.discovery_budget
        # Retain discovery inputs, not the launcher's client or pass-local state.
        for name in ("items", "closed_items", "comments_index", "cache", "comment_store",
                     "reconciled_comments", "comment_window_start", "repository_index"):
            setattr(self.planner.discovery, name, deepcopy(getattr(loop.discovery, name)))
        self.planner.coordinator.clock = loop.coordinator.clock
        self.planner.usage = loop.usage
        self.planner.health = loop.health
        self.planner.maintenance = loop.maintenance
        self.thread = threading.Thread(target=self._run, name="run-planning")

    def start(self):
        self.thread.start()

    def cancel(self):
        with self.lock:
            self.stop.set()
            self._finish_refresh()
            return self.rate_until

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
        interval, _ = idle_interval(self.loop.discovery_budget.wait_seconds(),
                                    self.planner.config.poll_seconds,
                                    self.loop.github.resource_quotas,
                                    self.planner.coordinator.clock(), elapsed)
        rate_until = None
        while not self._wait(max(0, self.started + interval - self.clock()), rate_until):
            # Other discovery can debit while this worker sleeps, including a
            # cancelled predecessor's final response. Forced refreshes bypass
            # admission, and an hour from the previous start remains the cap.
            if not self.refreshing:
                delay = min(self.loop.discovery_budget.wait_seconds(),
                            max(0, self.started + IDLE_MAX_SECONDS - self.clock()))
                if delay:
                    interval = self.clock() - self.started + delay
                    continue
            with self.lock:
                if self.stop.is_set():
                    return
                self.started = self.clock()
                self.rate_until = None
            observed_at = self.planner.coordinator.clock()
            events = PassEvents()
            self.planner.observer = events
            rate_wait = 0
            rate_until = None
            try:
                # Exhaust the ranked queue. No tick, recovery, parking or runtime
                # maintenance is reachable through this path.
                with self.planner.discovery_pass("observation", self.loop.pass_output):
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
                    with self.lock:
                        if not self.stop.is_set():
                            self.rate_until = rate_until
                # Observation failure cannot stop or change the owned run.
            finally:
                with self.lock:
                    self._finish_refresh()
            elapsed = self.clock() - self.started
            interval, _ = idle_interval(self.loop.discovery_budget.wait_seconds(),
                                        self.planner.config.poll_seconds,
                                        self.planner.github.resource_quotas,
                                        self.planner.coordinator.clock(), elapsed)
            interval = max(interval, elapsed + rate_wait)
