"""Ordered, advisory project notifications, independent of run supervision."""

from collections import deque
import json
import os
import subprocess
import threading
from time import monotonic

from .execution import stop_group
from .records import resolve_transition


class EventHooks:
    def __init__(self, output=print):
        self.output = output
        self._condition = threading.Condition()
        self._pending = deque()
        self._thread = None
        self._closing = False
        self._shutdown_deadline = None
        self._active_timeout = 0

    def _notice(self, message):
        try:
            self.output(f"Events hook: {message}")
        except Exception:
            pass  # Even a failed notice must not affect coordination.

    def emit(self, config, lease, event, report=None):
        """Snapshot only this launcher's committed start/release, without waiting."""
        hook = config.events
        if hook is None or event not in hook.on:
            return
        try:
            kind = "pr" if lease.get("assignment_sha") else "issue"
            context = {"event": event, "repository": config.repository,
                       "run": lease["run"], "agent": lease["agent"],
                       "assignment": lease["assignment"], "kind": kind,
                       "url": f"https://github.com/{config.repository}/{'pull' if kind == 'pr' else 'issues'}/{lease['assignment']}",
                       "attempt": lease["attempt"], "mode": lease.get("mode")}
            if event == "outcome":
                report = report or {}
                transition = {}
                if (lease["result"] == "success" and report.get("accepted")
                        and report.get("transition_complete")):
                    agent = next(a for a in config.agents if a.name == lease["agent"])
                    transition = resolve_transition(report["transition"], agent.triggers, config.stop_labels)
                context |= {"status": lease["result"], "summary": lease["summary"],
                            "outcome": report.get("outcome") if transition else None,
                            "actions": report.get("actions", []), "options": report.get("options", []),
                            "handoff": report.get("handoff"),
                            "labels_added": sorted(set(transition.get("add", ()))),
                            "labels_removed": sorted(set(transition.get("remove", ())))}
            data = (json.dumps(context) + "\n").encode("utf-8")
            with self._condition:
                if self._closing:
                    return
                self._pending.append((config.root, hook, data))
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="ub-agents-events", daemon=True)
                    try:
                        self._thread.start()
                    except Exception:
                        self._thread = None
                        self._pending.clear()
                        raise
                self._condition.notify_all()
        except Exception as exc:
            self._notice(f"cannot queue {event}: {exc}")

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if not self._pending:
                    return
                root, hook, data = self._pending.popleft()
                self._active_timeout = hook.timeout_seconds
            try:
                self._deliver(root, hook, data)
            except Exception as exc:
                self._notice(str(exc))
            finally:
                with self._condition:
                    self._active_timeout = 0

    def _remaining(self, deadline):
        with self._condition:
            if self._shutdown_deadline is not None:
                deadline = min(deadline, self._shutdown_deadline)
        return deadline - monotonic()

    def _deliver(self, root, hook, data):
        deadline = monotonic() + hook.timeout_seconds
        if self._remaining(deadline) <= 0:
            self._notice("shutdown timeout; queued event discarded")
            return
        env = {key: value for key, value in os.environ.items() if not key.startswith("UB_AGENTS_")}
        process = subprocess.Popen(list(hook.command), cwd=root, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            while True:
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    self._notice(f"timed out (limit {hook.timeout_seconds:g}s or launcher shutdown)")
                    return
                try:
                    process.communicate(input=data, timeout=min(0.1, remaining))
                    if process.returncode:
                        self._notice(f"command exited {process.returncode}")
                    return
                except subprocess.TimeoutExpired:
                    data = None  # communicate resumes the same stdin after a timeout.
        finally:
            # Stop descendants even when the command exited successfully. This
            # supervisor is advisory: uncertain termination never blocks a run.
            stop_group(process, grace=0.1)

    def close(self):
        """Drain in order within one timeout budget, then terminate remaining hooks."""
        with self._condition:
            self._closing = True
            if self._shutdown_deadline is None:
                timeout = max([self._active_timeout] + [h.timeout_seconds for _, h, _ in self._pending])
                self._shutdown_deadline = monotonic() + timeout
            self._condition.notify_all()
            worker = self._thread
        if worker is not None:
            try:
                worker.join()
            finally:
                # An interrupt during shutdown still cannot leave an owned hook.
                with self._condition:
                    self._shutdown_deadline = 0
                worker.join()
