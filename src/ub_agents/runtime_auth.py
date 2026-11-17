"""Cache CLI login probes for otherwise-ready work, without durable state."""

import threading
from time import monotonic

from .agent_health import CACHE_SECONDS, run_check

AUTH_PROBES = {"codex": ("codex", "login", "status"),
               "claude": ("claude", "auth", "status")}
LOGIN_COMMANDS = {"codex": "codex login", "claude": "claude auth login"}


class RuntimeAuth:
    def __init__(self, output=print, clock=monotonic):
        self.output, self.clock = output, clock
        self.passed = {}
        self.shown = {}
        self.lock = threading.Lock()

    def check(self, root, cli, checked=None):
        key = (root, cli)
        with self.lock:
            if checked is not None and key in checked:
                return checked[key]
            if self.passed.get(key, float("-inf")) + CACHE_SECONDS > self.clock():
                return None
            # Probe output can include credentials. Publish only the fixed login
            # remedy, including when the probe fails to start or times out.
            error = run_check(AUTH_PROBES[cli], root)
            if error is None:
                self.passed[key] = self.clock()
                return None
            self.passed.pop(key, None)
            reason = f"{cli} is not logged in; run {LOGIN_COMMANDS[cli]}"
            if checked is not None:
                checked[key] = reason
            return reason

    def notice(self, root, agent, reason, *, announce=False):
        if not announce:
            return
        key = (root, agent.name)
        with self.lock:
            previous = self.shown.get(key)
            if reason is not None and reason != previous:
                self.output(f"{agent.name}: waiting — {reason}")
                self.shown[key] = reason
            elif reason is None and previous is not None:
                self.output(f"{agent.name}: runtime login check passed — claiming resumes")
                self.shown.pop(key, None)
