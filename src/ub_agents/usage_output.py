"""Detect reported limits and reset times from an owned run's output."""

import json
from pathlib import Path
import uuid

from .runtime_usage import number


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
        self.reset_time = None
        self.summary = None

    def poll(self, final=False):
        for event in self.log.read(final):
            if self.cli == "codex" and self.thread is None and event.get("type") == "thread.started":
                try:
                    self.thread = str(uuid.UUID(event["thread_id"]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            self.event(event)
        if final and self.cli == "codex" and self.thread and not self.reached:
            if self.session is None:
                # The exact fresh thread, never --last or another run's transcript.
                try:
                    matches = list((self.codex_home / "sessions").glob(
                        f"*/*/*/rollout-*-{self.thread}.jsonl"))
                except OSError:
                    matches = []
                if len(matches) == 1:
                    self.session = JsonLines(matches[0])
            if self.session:
                for event in self.session.read(final):
                    if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict):
                        self.event(event["payload"])

    def event(self, event):
        reached = False
        if self.cli == "claude":
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info")
                if not isinstance(info, dict):
                    return
                reset = info.get("resetsAt")
                windows = info.get("unifiedWindows")
                name = info.get("rateLimitType")
                if reset is None and isinstance(windows, dict) and isinstance(name, str):
                    window = windows.get(name)
                    if isinstance(window, dict):
                        reset = window.get("resetsAt")
                self.reset_time = reset
                reached = info.get("status") == "rejected"
            elif event.get("type") in ("assistant", "result", "error"):
                reached = event.get("error") == "rate_limit" or event.get("api_error_status") == 429
                if "resetsAt" in event:
                    self.reset_time = event["resetsAt"]
        elif self.cli == "codex":
            snapshot = event.get("rate_limits")
            if isinstance(snapshot, dict):
                # With no utilization tracking, use the latest reported reset.
                resets = [number(window.get("resets_at")) for name in ("primary", "secondary")
                          if isinstance(window := snapshot.get(name), dict)]
                self.reset_time = max((reset for reset in resets if reset is not None), default=None)
                reached = bool(snapshot.get("rate_limit_reached_type"))
            if event.get("type") == "error" and event.get("codex_error_info") == "usage_limit_exceeded":
                reached = True
            if "resets_at" in event:
                self.reset_time = event["resets_at"]
        if reached:
            self.reached = True
            self.summary = self.usage.limit(self.cli, self.reset_time)
