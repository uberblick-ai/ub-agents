"""Project-supplied, per-agent checks before new claims; no durable state."""

import shlex
import subprocess
import threading
from time import monotonic

from .execution import stop_group

TIMEOUT_SECONDS = 60
CACHE_SECONDS = 5 * 60


def last_line(text):
    return next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")


def run_check(command, root):
    try:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as exc:
        return " ".join(str(exc).splitlines())
    try:
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return f"Timed out after {TIMEOUT_SECONDS} seconds"
    finally:
        # Checks can spawn helpers too. Drain or interrupt never leaves their
        # process group running, including helpers holding inherited pipes open.
        try:
            stop_group(process)
        finally:
            process.stdout.close()
            process.stderr.close()
    if process.returncode:
        return last_line(stderr) or last_line(stdout) or f"Exited {process.returncode}"
    return None


class AgentHealth:
    def __init__(self, output=print, clock=monotonic):
        self.output, self.clock = output, clock
        self.passed = {}
        self.shown = {}
        self.lock = threading.Lock()

    def check(self, root, agent, checked=None, *, announce=False):
        if not agent.health_check:
            return None
        key = (root, agent.name, agent.health_check)
        # Queue observations during a run share successful probes, but never
        # announce from that worker or hold an assignment's coordination lock.
        with self.lock:
            if checked is not None and key in checked:
                error = checked[key]
            elif self.passed.get(key, float("-inf")) + CACHE_SECONDS > self.clock():
                error = None
            else:
                error = run_check(agent.health_check, root)
                if error is None:
                    self.passed[key] = self.clock()
                else:
                    self.passed.pop(key, None)
            if checked is not None and error is not None:
                # A failure lasts only through this planning pass: several ready
                # items share one probe, and the next poll always retries it.
                checked[key] = error
            command = shlex.join(agent.health_check).replace("\n", r"\n").replace("\r", r"\r")
            if announce:
                previous = self.shown.get(key)
                if error is not None and error != previous:
                    self.output(f"{agent.name}: waiting — health check {command}: {error}")
                    self.shown[key] = error
                elif error is None and previous is not None:
                    self.output(f"{agent.name}: health check {command} passed — claiming resumes")
                    self.shown.pop(key, None)
            return f"Health check {command}: {error}" if error is not None else None
