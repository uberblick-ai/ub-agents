"""Read structured usage only, from an owned run's log and Codex session."""

import json
from pathlib import Path
import uuid

from .runtime_usage import WINDOWS, number


class JsonLines:
    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, b""

    def read(self, final=False):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError:
            return
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        if final and self.pending:
            lines.append(self.pending)
            self.pending = b""
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                yield event


class UsageOutput:
    def __init__(self, cli, run_dir, usage, env):
        self.cli, self.usage = cli, usage
        self.log = JsonLines(run_dir / "process.log")
        self.codex_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
        self.thread = None
        self.session = None
        self.reached = False
        self.hint = ("limit", None, None)

    def poll(self, final=False):
        for event in self.log.read(final):
            if self.cli == "codex" and event.get("type") == "thread.started":
                try:
                    self.thread = str(uuid.UUID(event["thread_id"]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            self.event(event)
        if self.cli == "codex" and self.thread:
            if self.session is None:
                # The exact fresh thread, never --last or another run's transcript.
                matches = list((self.codex_home / "sessions").glob(
                    f"*/*/*/rollout-*-{self.thread}.jsonl"))
                if len(matches) == 1:
                    self.session = JsonLines(matches[0])
            if self.session:
                for event in self.session.read(final):
                    if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict):
                        self.event(event["payload"])

    def event(self, event):
        if self.cli == "claude":
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info")
                if not isinstance(info, dict):
                    return
                status = info.get("status")
                windows = info.get("unifiedWindows")
                hints = []
                if isinstance(windows, dict):
                    for name, length in WINDOWS.items():
                        window = windows.get(name)
                        if isinstance(window, dict):
                            used = number(window.get("utilization"))
                            self.usage.record(self.cli, name, used * 100 if used is not None else None,
                                              window.get("resetsAt"), length, status)
                            hints.append((used or 0, name, window.get("resetsAt"), length))
                if hints and not self.reached:
                    _, name, reset, length = max(hints, key=lambda w: w[0])
                    self.hint = (name, reset, length)
                if status == "rejected":
                    name = info.get("rateLimitType") or "five_hour"
                    name = name if isinstance(name, str) and name in WINDOWS else "limit"
                    self.hint = (name, info.get("resetsAt"), WINDOWS.get(name))
                    if isinstance(windows, dict) and isinstance(windows.get(name), dict):
                        self.hint = (name, windows[name].get("resetsAt"), WINDOWS.get(name))
                    self.reached = True
            elif (event.get("type") in {"assistant", "result", "error"}
                  and (event.get("error") == "rate_limit" or event.get("api_error_status") == 429)):
                self.reached = True
        else:
            if (event.get("type") == "error"
                    and event.get("codex_error_info") == "usage_limit_exceeded"):
                self.reached = True
            snapshot = event.get("rate_limits")
            if not isinstance(snapshot, dict):
                return
            windows = []
            for name in ("primary", "secondary"):
                window = snapshot.get(name)
                if isinstance(window, dict):
                    minutes = number(window.get("window_minutes"))
                    length = minutes * 60 if minutes is not None else None
                    used = number(window.get("used_percent"))
                    self.usage.record(self.cli, name, used, window.get("resets_at"), length,
                                      "rejected" if snapshot.get("rate_limit_reached_type") else None)
                    windows.append((used or 0, name, window.get("resets_at"), length))
            if windows:
                _, name, reset, length = max(windows, key=lambda w: w[0])
                self.hint = (name, reset, length)
            if snapshot.get("rate_limit_reached_type"):
                self.reached = True
