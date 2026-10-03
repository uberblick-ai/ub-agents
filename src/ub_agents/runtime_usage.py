"""Launcher-local usage windows, with bounded, clock-only pauses."""

import json
import math
import os
from pathlib import Path
import socket
import subprocess
import uuid

from .records import iso, seconds, timestamp

MARGIN_SECONDS = 60
FALLBACK_SECONDS = 15 * 60
WINDOWS = {"five_hour": 5 * 60 * 60, "seven_day": 7 * 24 * 60 * 60}


def number(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def deadline(started, reset, length, observed=None):
    reset, length = number(reset), number(length)
    if trusted_reset(started if observed is None else observed, reset, length):
        return reset + MARGIN_SECONDS
    return started + FALLBACK_SECONDS


def trusted_reset(started, reset, length):
    reset, length = number(reset), number(length)
    return reset is not None and length is not None and 0 < reset - started <= length


def pause_rows(state, now):
    rows = []
    for cli, windows in state.get("pauses", {}).items():
        if cli not in {"claude", "codex"} or not isinstance(windows, dict):
            continue
        active = []
        for pause in windows.values():
            if not isinstance(pause, dict):
                continue
            started = number(pause.get("started_at"))
            if started is None or started > now or not isinstance(pause.get("reason"), str):
                continue
            observed = number(pause.get("observed_at", started))
            if observed is None or not started <= observed <= now:
                continue
            end = deadline(started, pause.get("reset_at"), pause.get("window_seconds"), observed)
            if end > now:
                active.append((end, pause["reason"]))
        if active:
            rows.append({"cli": cli, "reason": "; ".join(dict.fromkeys(reason for _, reason in active)),
                         "ends_at": iso(max(end for end, _ in active)),
                         "launcher": state["launcher"]})
    return rows


def process_started(pid):
    """Return a stable process start, empty for an exited process, or unknown."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart="],
                                text=True, capture_output=True, timeout=5,
                                env=os.environ | {"LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return ""
    fields = result.stdout.split()
    if result.returncode or len(fields) != 6:
        return None
    return "" if "Z" in fields[0] else " ".join(fields[1:])


def launcher_alive(state):
    """Unknown inspection must never authorize removal of another launcher's file."""
    pid = state.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # Liveness probe only; never signal another launcher.
    except ProcessLookupError:
        return False
    except OSError:
        return None
    started = process_started(pid)
    if started == "":
        return False
    recorded = state.get("process_started")
    if started is None or not isinstance(recorded, str) or not recorded:
        return None
    return started == recorded


def local_pauses(root, now=None):
    """Read all live launchers on this host without rewriting their files."""
    now = timestamp() if now is None else now
    rows = []
    for path in sorted((Path(root) / ".ub-agents" / "runtime-usage").glob("*.json")):
        try:
            state = json.loads(path.read_text())
            if (state.get("version") != 1 or state.get("host") != socket.gethostname()
                    or not isinstance(state.get("launcher"), str)
                    or state["launcher"] != path.stem
                    or type(state.get("pid")) is not int or state["pid"] <= 0
                    or not isinstance(state.get("pauses"), dict)):
                continue
            if launcher_alive(state) is not True:
                continue
            rows.extend(pause_rows(state, now))
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            continue
    return rows


class RuntimeUsage:
    def __init__(self, root, clock=timestamp, output=print, read_only=False):
        self.root, self.clock, self.output, self.read_only = Path(root), clock, output, read_only
        self.launcher = uuid.uuid4().hex
        self.path = self.root / ".ub-agents" / "runtime-usage" / f"{self.launcher}.json"
        self.readings, self.pauses = {}, {}
        self._write_warning = False
        self._process_started = None

    def reset(self):
        # Each new launch starts empty. Other live launchers own their own files.
        self.readings.clear()
        self.pauses.clear()
        self.close()
        for path in self.path.parent.glob("*.json"):
            try:
                state = json.loads(path.read_text())
                if state.get("host") == socket.gethostname() and launcher_alive(state) is False:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, AttributeError, OverflowError):
                continue

    def close(self):
        """Remove only this launcher's published state and incomplete write."""
        for path in (self.path, self.path.with_suffix(".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.output(f"Cannot remove runtime usage state: {exc}")

    def state(self):
        return {"version": 1, "launcher": self.launcher, "pid": os.getpid(),
                "host": socket.gethostname(), "process_started": self._process_started,
                "readings": self.readings, "pauses": self.pauses}

    def save(self):
        try:
            if self._process_started is None:
                self._process_started = process_started(os.getpid())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state()))
            temporary.replace(self.path)
        except OSError as exc:
            if not self._write_warning:
                self.output(f"Cannot publish runtime usage state: {exc}")
                self._write_warning = True

    def rows(self):
        if self.read_only:
            return local_pauses(self.root, self.clock())
        now, changed = self.clock(), False
        for groups in (self.readings, self.pauses):
            for windows in groups.values():
                for name, reading in list(windows.items()):
                    if deadline(reading["started_at"], reading.get("reset_at"),
                                reading.get("window_seconds"), reading.get("observed_at")) <= now:
                        del windows[name]
                        changed = True
        if changed:
            self.save()
        return pause_rows(self.state(), now)

    def paused(self, cli):
        return next((row for row in self.rows() if row["cli"] == cli), None)

    def bound_wait(self, delay):
        ends = [row["ends_at"] for row in self.rows()]
        if not ends:
            return delay
        return min(delay, max(0, min(map(seconds, ends)) - self.clock()))

    def pause(self, cli, window, reset, length, reason):
        previous = self.paused(cli)
        windows = self.pauses.setdefault(cli, {})
        reading = windows.get(window)
        reset, length = number(reset), number(length)
        if reading is None:
            reading = {"started_at": self.clock(), "observed_at": self.clock(),
                       "reset_at": reset, "window_seconds": length, "reason": reason}
        elif (reset, length) != (reading["reset_at"], reading["window_seconds"]):
            # A new reset supersedes the old one, but untrusted updates retain
            # the first untrusted reading's fallback start. Identical resets stay fixed.
            if (trusted_reset(reading["observed_at"], reading["reset_at"], reading["window_seconds"])
                    and not trusted_reset(self.clock(), reset, length)):
                reading["started_at"] = self.clock()
            reading.update(reset_at=reset, window_seconds=length, observed_at=self.clock())
        reading["reason"] = reason
        windows[window] = reading
        self.save()
        current = self.paused(cli)
        if current and (previous is None or previous["ends_at"] != current["ends_at"]):
            self.output(f"{cli} {reason}; pausing {cli} runs until {current['ends_at']} "
                        f"({(seconds(current['ends_at']) - self.clock()) / 60:g} min)")
        return reading

    def record(self, cli, window, percent, reset, length, status=None):
        self.rows()
        reading = {"started_at": self.clock(), "observed_at": self.clock(), "used_percent": number(percent),
                   "reset_at": number(reset), "window_seconds": number(length), "status": status}
        self.readings.setdefault(cli, {})[window] = reading
        if reading["used_percent"] is not None and reading["used_percent"] >= 90:
            reason = ("usage limit reached" if status == "rejected" else
                      f"{window} usage {reading['used_percent']:g}%")
            pause = self.pause(cli, window, reset, length, reason)
            # Repeated readings cannot extend an untrusted reset's fallback.
            reading["started_at"] = pause["started_at"]
            reading["observed_at"] = pause["observed_at"]
        self.save()

    def limit(self, cli, hint):
        window, reset, length = hint
        pause = self.pause(cli, window, reset, length, "usage limit reached")
        valid = trusted_reset(pause["observed_at"], reset, length)
        end = number(reset) if valid else deadline(pause["started_at"], reset, length, pause["observed_at"])
        return f"{cli} usage limit reached; resets {iso(end)}"
