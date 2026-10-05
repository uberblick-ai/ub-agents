"""Bounded launcher observations. Never an input to coordination or recovery."""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import uuid

from .records import iso, seconds, timestamp
from .attention import attention_details, notice_summary
from .notices import ACTION_MARKER
from .github import closing_issues
from .eligibility import AgentMatches
from .run_history import display_run, merge_record, observed_blockers, sort_runs
from . import __version__

VERSION = 1
MAX_PLANS = 100
MAX_OUTCOMES = 20
MAX_ADVISORIES = 128
MAX_TEXT = 2048
DESCRIPTION_PREVIEW = 256
MAX_BYTES = 64 * 1024
HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
RETAINED_SESSIONS = 20


def unavailable(reason):
    return {"available": False, "reason": reason}


class Publisher:
    """Nonblocking, atomic datagram mailbox to an expendable filesystem worker.

    Both ends are retained here so a full mailbox can discard pending snapshots
    before sending the newest one. Messages are complete snapshots, never events.
    The lifecycle pipe closes on clean exit *and* process death. The worker drains
    the mailbox and marks the session ended without a launcher-side wait.
    """

    def __init__(self, root, output=print, command=None):
        self.output = output
        self.failed = False
        self.warning_lock = threading.Lock()
        self.process = None
        self.sender = self.receiver = self.life_write = None
        life_read = error_read = error_write = None
        error_stream = None
        try:
            self.sender, self.receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            # Darwin's default Unix datagram send buffer is only 2 KiB. Reserve
            # per-socket space for a complete snapshot on both platforms.
            self.sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, MAX_BYTES * 2)
            self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, MAX_BYTES * 2)
            self.sender.setblocking(False)
            self.receiver.setblocking(False)
            life_read, self.life_write = os.pipe()
            error_read, error_write = os.pipe()
            argv = command or [sys.executable, "-P", "-m", "ub_agents.observation_worker"]
            self.process = subprocess.Popen(
                [*argv, str(root), str(self.receiver.fileno()), str(life_read), str(error_write), str(self.sender.fileno())],
                pass_fds=(self.receiver.fileno(), life_read, error_write, self.sender.fileno()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

            # Transfer descriptor ownership before starting the listener. A signal
            # can interrupt Thread.start after the thread has already begun.
            error_stream = os.fdopen(error_read, "rb")
            error_read = None
            # Diagnostics and reaping also stay off the execution path.
            def diagnostic():
                try:
                    with error_stream as stream:
                        detail = stream.read(1024).decode("utf-8", errors="replace")
                    if detail:
                        self.warning(detail)
                finally:
                    code = self.process.wait()
                    if code:
                        self.warning(f"Observation worker exited {code}")
            self.diagnostics = threading.Thread(target=diagnostic, daemon=True)
            self.diagnostics.start()
        except BaseException as exc:
            self.close()
            if not isinstance(exc, (OSError, RuntimeError)):
                raise
            self.warning(str(exc))
        finally:
            for descriptor in (life_read, error_write, error_read):
                if descriptor is not None:
                    os.close(descriptor)

    def warning(self, detail):
        if not self.warning_lock.acquire(blocking=False):
            return
        try:
            if not self.failed:
                self.failed = True
                try:
                    self.output(f"Cannot publish launcher observations: {' '.join(detail.split())[:512]}")
                except Exception:
                    pass
        finally:
            self.warning_lock.release()

    def submit(self, data):
        if self.failed or self.life_write is None:
            return
        try:
            self.sender.send(data)
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}:
                self.warning(str(exc))
                return
            # A stuck writer cannot grow a backlog or make the producer wait.
            for _ in range(256):
                try:
                    self.receiver.recv(MAX_BYTES + 1)
                except BlockingIOError:
                    break
            try:
                self.sender.send(data)
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}:
                    self.warning(str(exc))

    def close(self):
        descriptor, self.life_write = self.life_write, None
        if descriptor is not None:
            os.close(descriptor)
        for endpoint in (self.sender, self.receiver):
            if endpoint is not None:
                endpoint.close()
        # Deliberately no wait, join, filesystem operation or worker callback.


class Observations:
    """Pure, bounded state reduction on the launcher thread; publication is optional."""

    def __init__(self, config, actor, config_path, publisher, clock=timestamp):
        self.publisher, self.clock = publisher, clock
        self.root = config.root.resolve()
        self.stop_labels = config.stop_labels
        self.state = {
            "version": VERSION, "base_version": __version__, "session": uuid.uuid4().hex, "pid": os.getpid(),
            "host": socket.gethostname(), "actor": actor,
            "actor_reason": None if actor else "Authentication has not completed",
            "repository": config.repository, "config_path": str(config_path) if config_path else None,
            "config_path_reason": None if config_path else "No configuration path supplied",
            "started_at": iso(clock()), "published_at": iso(clock()), "ended": False,
            "activity": {"state": "polling"}, "assignment": None, "latest_pass": None, "update": None,
            "outcomes": [], "histories": {}, "action_needed": {}, "coordination_authors": {},
            "omitted": {"plans": 0, "outcomes": 0},
            "limits": {"plans": MAX_PLANS, "outcomes": MAX_OUTCOMES, "text": MAX_TEXT,
                       "bytes": MAX_BYTES, "heartbeat_seconds": HEARTBEAT_SECONDS,
                       "stale_seconds": STALE_SECONDS},
        }
        self.source_run = None
        self.previous_histories = {}
        self.pass_rows = {}
        self.pass_histories = {}
        self.pass_omitted = 0
        self.kept_keys = set()
        self._batching = False
        self.emit()

    @staticmethod
    def bound(value, shortened):
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                shortened["fields"] += 1
                shortened["characters"] += len(value) - MAX_TEXT
            return value[:MAX_TEXT]
        if isinstance(value, dict):
            return {key: Observations.bound(val, shortened) for key, val in value.items()}
        if isinstance(value, list):
            return [Observations.bound(val, shortened) for val in value]
        return value

    @classmethod
    def bounded(cls, row):
        shortened = {"fields": 0, "characters": 0}
        result = cls.bound(row, shortened)
        result["shortened"] = shortened
        return result

    @classmethod
    def bounded_run(cls, row):
        shortened = {"fields": 0, "characters": 0}
        cls.bound(display_run(row), shortened)
        result = cls.bound(row, {"fields": 0, "characters": 0})
        result["shortened"] = shortened if shortened["fields"] else row.get("shortened", shortened)
        return result

    def emit(self):
        if self._batching:
            return
        shortened = {"fields": 0, "characters": 0}
        histories = {}
        for key, history in self.state["histories"].items():
            counts = dict(history.get("shortened", {"fields": 0, "characters": 0}))
            for run in history["runs"]:
                for name in counts:
                    counts[name] += run.get("shortened", {}).get(name, 0)
            histories[key] = history | {"runs": [display_run(run) for run in history["runs"]],
                                        "shortened": counts}
        state = self.bound(self.state | {"histories": histories}, shortened)
        groups = ([state["latest_pass"]["rows"]] if state["latest_pass"] else []) + [state["outcomes"],
                  list(state["histories"].values())]
        for rows in groups:
            for row in rows:
                for key in shortened:
                    shortened[key] += row.get("shortened", {}).get(key, 0)
        state["shortened"] = shortened
        while True:
            data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) <= MAX_BYTES:
                self.publisher.submit(data)
                return
            # Account for row bytes once instead of repeatedly serializing the
            # entire over-limit snapshot for each dropped row.
            excess = len(data) - MAX_BYTES + 32
            # Advisory text can always be loaded explicitly; preserve work and
            # histories before it. This only changes this publication's copy.
            for key in tuple(state["action_needed"]):
                if excess <= 0:
                    break
                excess -= self.byte_size(state["action_needed"].pop(key)) + len(key) + 4
            rows = state["latest_pass"]["rows"] if state["latest_pass"] else []
            # Prefer a shorter, still-available description over losing history.
            # Work on this publication's copy, never the retained launcher state.
            previews = [row["description"] for row in rows if row.get("description", {}).get("available")
                        and len(row["description"].get("text", "")) > DESCRIPTION_PREVIEW]
            for description in sorted(previews, key=lambda d: self.byte_size(d), reverse=True):
                if excess <= 0:
                    break
                before = self.byte_size(description)
                omitted = len(description["text"]) - DESCRIPTION_PREVIEW
                description["text"] = description["text"][:DESCRIPTION_PREVIEW]
                description["omitted_characters"] += omitted
                state["shortened"]["fields"] += 1
                state["shortened"]["characters"] += omitted
                excess -= before - self.byte_size(description)
            # Omit globally oldest surplus runs, preserving the newest run of
            # every referenced item, including the current assignment.
            while excess > 0:
                candidates = [(key, history) for key, history in state["histories"].items()
                              if len(history["runs"]) > 1]
                if not candidates:
                    break
                _, history = min(candidates, key=lambda pair: (
                    seconds(pair[1]["runs"][0]["time"]) if pair[1]["runs"][0].get("time") else 0,
                    pair[0]))
                row = history["runs"].pop(0)
                excess -= self.byte_size(row) + 1
                history["omitted_runs"] += 1
            # If even one run per item cannot fit, omit later plans and older
            # session outcomes together with histories no longer referenced.
            for key, group, index in (("plans", rows, -1), ("outcomes", state["outcomes"], 0)):
                while excess > 0 and group:
                    row = group.pop(index)
                    excess -= len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                    state["omitted"][key] += 1
                    for history in self.prune_histories(state):
                        excess -= len(json.dumps(history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            if excess > 0:
                raise ValueError("Observation envelope exceeds its size limit")

    @staticmethod
    def byte_size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def warning(self, detail):
        self.publisher.warning(detail)

    def configure(self, config, actor, path):
        self.stop_labels = config.stop_labels
        self.state["coordination_authors"] = {}
        if config.repository != self.state["repository"]:
            self.state["action_needed"] = {}
        self.state.update(repository=config.repository, actor=actor,
                          config_path_reason=None if path else "No configuration path supplied",
                          actor_reason=None if actor else "Authentication unavailable",
                          config_path=str(path) if path else None)
        self.emit()

    def coordination_author(self, login, trusted, reason):
        authors = self.state["coordination_authors"]
        key = login.casefold()
        value = {"trusted": trusted, "reason": reason}
        previous = authors.pop(key, None)
        authors[key] = value
        if previous == value:
            return
        while len(authors) > MAX_ADVISORIES:
            authors.pop(next(iter(authors)))
        self.emit()

    def action_needed(self, number, comment, emit=True):
        notices = self.state["action_needed"]
        key = str(number)
        if comment is None:
            notices.pop(key, None)
        else:
            cached = notices.get(key)
            if cached and cached["id"] > comment["id"]:
                return
            notices.pop(key, None)
            body = comment["body"]
            notices[key] = {"id": comment["id"], "text": body[:MAX_TEXT],
                            "author": comment["user"]["login"], "created_at": comment.get("created_at"),
                            "omitted_characters": max(0, len(body) - MAX_TEXT)}
            while len(notices) > MAX_ADVISORIES:
                notices.pop(next(iter(notices)))
            latest = self.state["latest_pass"] or {}
            for row in latest.get("rows", ()):
                if row["item"] == number and row["state"] in {"parked", "blocked", "failed"}:
                    row.update(waiting_since=comment.get("created_at"),
                               attention_reason=notice_summary(body)[:MAX_TEXT])
                    if row["attention_reason"] and row["attention_reason"] not in row["reason"]:
                        row["reason"] += (": " if row["reason"] else "") + row["attention_reason"]
        if emit:
            self.emit()

    def activity(self, state, until=None, reason=None):
        self.state["activity"] = {"state": state, "until": until, "reason": reason}
        self.emit()

    def update(self, banner):
        self.state["update"] = banner
        self.emit()

    def observation_pass(self, started, events):
        """Publish only a completed worker pass, preserving live run updates."""
        activity = self.state["activity"]
        assignment = self.state["assignment"]
        key = str(assignment["item"]) if assignment else None
        history = self.state["histories"].get(key)
        self._batching = True
        try:
            self.begin_pass(started)
            for method, args in events:
                getattr(self, method)(*args)
            self.complete_pass()
            if history is not None:
                self.state["histories"][key] = history
        finally:
            self.state["activity"] = activity
            self._batching = False
        self.emit()

    def begin_pass(self, started=None):
        # Keep rows and histories until replanned, reconciled by discovery or finished.
        # Track its plans separately to apply the new order only on completion.
        self.previous_histories = dict(self.state["histories"])
        rows = self.state["latest_pass"]["rows"] if self.state["latest_pass"] else []
        self.state["latest_pass"] = {"started_at": iso(self.clock() if started is None else started),
                                     "state": "partial", "rows": list(rows)}
        self.pass_rows = {}
        self.pass_histories = {}
        self.pass_omitted = 0
        self.kept_keys = {(row["item"], row["agent"]) for row in rows}
        self.state["omitted"]["plans"] = 0
        self.prune_histories(self.state)
        self.activity("polling")

    def discovered(self, items, agents, all_open=False):
        """Drop closed carried rows and untriggered Eligible rows from read inputs.

        A full repository open-item list also observes missing items as closed.
        Item reads only reconcile that item. Open Needs attention rows remain
        until replanned or the pass completes. Plans reached in this pass always
        take precedence, including recovery for closed or untriggered items.
        """
        latest = self.state["latest_pass"]
        if latest is None or not self.kept_keys:
            return
        matched = {number: {a.name for a in AgentMatches.for_item(items[number], agents).matched}
                   for number in {row["item"] for row in latest["rows"]} if number in items}
        removed = {(row["item"], row["agent"]) for row in latest["rows"]
                   if (row["item"], row["agent"]) in self.kept_keys
                   and (row["item"] in items or all_open)
                   and (row["state"] in {"ready", "recover", "backoff", "waiting"}
                        or row["item"] not in items or items[row["item"]].state != "open")
                   and row["agent"] not in matched.get(row["item"], ())}
        if not removed:
            return
        latest["rows"] = [row for row in latest["rows"] if (row["item"], row["agent"]) not in removed]
        self.kept_keys.difference_update(removed)
        self.prune_histories(self.state)
        self.emit()

    def complete_pass(self):
        self.state["latest_pass"].update(state="complete", rows=list(self.pass_rows.values()))
        self.state["histories"].update(self.pass_histories)
        self.state["omitted"]["plans"] = self.pass_omitted
        self.prune_histories(self.state)
        self.previous_histories.clear()
        self.pass_rows = {}
        self.pass_histories = {}
        self.kept_keys.clear()
        self.emit()

    @staticmethod
    def prune_histories(state):
        rows = state["latest_pass"]["rows"] if state["latest_pass"] else []
        referenced = {str(row["item"]) for row in rows + state["outcomes"]}
        referenced.update(row["history_key"] for row in rows if "history_key" in row)
        if state["assignment"]:
            referenced.add(str(state["assignment"]["item"]))
        return [state["histories"].pop(key) for key in tuple(state["histories"]) if key not in referenced]

    def item_history(self, plan, filing=None):
        closing = sorted(closing_issues(plan.item, self.state["repository"])) if plan.item.kind == "pr" else []
        source = (plan.item if plan.item.kind == "issue" else
                  filing if closing and filing and filing.kind == "issue" and filing.number == closing[0] else None)
        runs = []
        for record in plan.history:
            merge_record(runs, record, self.stop_labels)
        sort_runs(runs)
        history = {"item": plan.item.number, "kind": plan.item.kind, "title": plan.item.title,
                   "closes": closing[0] if closing else None,
                   "filing": ({"author": source.author, "time": source.created_at}
                              if source and source.author and source.created_at else None),
                   "runs": [self.bounded_run(run) for run in runs[-MAX_OUTCOMES:]],
                   "omitted_runs": max(0, len(runs) - MAX_OUTCOMES)}
        key = str(plan.item.number)
        cached = (self.pass_histories.get(key) or self.state["histories"].get(key) or
                  self.previous_histories.get(key))
        if not plan.history_read and cached:
            history.update(runs=[dict(run) for run in cached["runs"]], omitted_runs=cached["omitted_runs"],
                           filing=history["filing"] or cached["filing"])
        observed_blockers(history, plan.item, self.stop_labels)
        return self.bounded(history)

    def plan(self, plan, filing=None, comments=None, authors=None):
        if comments is not None:
            epoch = max((r["id"] for r in plan.history if r["kind"] in {"lease", "reset"}), default=0)
            for comment in comments:
                login = (comment.get("user") or {}).get("login")
                if (isinstance(login, str) and login.casefold() in (authors or {})
                        and isinstance(comment.get("body"), str) and comment["body"].startswith(ACTION_MARKER)):
                    trusted = authors[login.casefold()]
                    self.coordination_author(login, trusted, None if trusted else
                                             "The launcher account is not authorized for coordination records")
            notices = [c for c in comments if isinstance(c.get("body"), str)
                       and c["body"].startswith(ACTION_MARKER) and c["id"] > epoch
                       and isinstance((c.get("user") or {}).get("login"), str)
                       and (authors or {}).get((c.get("user") or {}).get("login", "").casefold())]
            self.action_needed(plan.item.number, max(notices, key=lambda c: c["id"], default=None), emit=False)
        notice = self.state["action_needed"].get(str(plan.item.number))
        if notice and any(record["kind"] in {"lease", "reset"} and record["id"] > notice["id"]
                          for record in plan.history):
            self.state["action_needed"].pop(str(plan.item.number), None)
            notice = None
        row = {"item": plan.item.number, "kind": plan.item.kind, "title": plan.item.title,
               "agent": plan.agent.name, "state": plan.state, "reason": plan.reason,
               "priority": plan.priority.rsplit(":", 1)[-1] if plan.priority else None,
               "runtime": plan.runtime.name if plan.runtime else None,
               "failures": plan.attempt - 1, "max_attempts": plan.agent.max_attempts,
               "observed_at": iso(self.clock()), "description": (
                   {"available": True, "text": plan.item.body,
                    "omitted_characters": max(0, len(plan.item.body) - MAX_TEXT)}
                   if isinstance(plan.item.body, str) else unavailable("Description was not read")),
               "owner": None}
        row.update(attention_details(plan, self.stop_labels, notice))
        summary = row.get("attention_reason")
        if summary and summary not in row["reason"]:
            row["reason"] += (": " if row["reason"] else "") + summary
        if plan.owner:
            row["owner"] = {key: plan.owner.get(key) for key in ("actor", "host", "run")}
            row["owner"]["host_reason"] = None if plan.owner.get("host") else "Host not recorded"
        row = self.bounded(row)
        key = (row["item"], row["agent"])
        self.kept_keys.discard(key)
        if key in self.pass_rows or len(self.pass_rows) < MAX_PLANS:
            self.pass_rows[key] = row
        else:
            self.pass_omitted += 1
        rows = self.state["latest_pass"]["rows"]
        existing = next((i for i, r in enumerate(rows)
                         if (r["item"], r["agent"]) == key), None)
        if existing is not None:
            rows[existing] = row
        elif len(rows) < MAX_PLANS:
            rows.append(row)
        visible = {(r["item"], r["agent"]) for r in rows}
        self.state["omitted"]["plans"] = self.pass_omitted + len(self.pass_rows.keys() - visible)
        listed = any(r["item"] == plan.item.number for r in rows)
        if listed or key in self.pass_rows:
            history = self.item_history(plan, filing)
            if key in self.pass_rows:
                self.pass_histories[str(plan.item.number)] = history
            if listed:
                # Other agents for this item may still show the prior pass. Give
                # those rows their own history reference before replacing it.
                cached = self.state["histories"].get(str(plan.item.number))
                for index, kept in enumerate(rows):
                    kept_key = (kept["item"], kept["agent"])
                    if (kept["item"] == plan.item.number and kept_key in self.kept_keys
                            and "history_key" not in kept and cached and cached != history):
                        history_key = f'{kept["item"]}:{kept["agent"]}'
                        self.state["histories"][history_key] = cached
                        rows[index] = kept | {"history_key": history_key}
                self.state["histories"][str(plan.item.number)] = history
                self.prune_histories(self.state)
        for outcome in self.state["outcomes"]:
            if outcome["target"] == plan.item.number and outcome["completed"]:
                outcome["human_blocker"] = sorted(plan.item.labels.intersection(self.stop_labels))
                outcome["blocker_observed_at"] = row["observed_at"]
        self.emit()

    def assignment(self, plan):
        self.state["histories"].setdefault(str(plan.item.number),
                                         self.pass_histories.get(str(plan.item.number)) or self.item_history(plan))
        self.state["assignment"] = {
            "item": plan.item.number, "kind": plan.item.kind, "agent": plan.agent.name,
            "title": plan.item.title[:MAX_TEXT], "attempt": plan.attempt,
            "priority": plan.priority.rsplit(":", 1)[-1] if plan.priority else None,
            "run": None, "runtime": None, "lease_state": None, "lease_expires": None,
            "process": "claiming", "process_reason": "No process has been recorded",
            "process_log": None, "context_path": None,
            "paths_reason": "No lease has been recorded",
        }
        self.source_run = None
        self.activity("running assignment")

    def record(self, record):
        if record["kind"] == "reset" or (record["kind"] == "lease" and record["state"] == "claiming"):
            self.state["action_needed"].pop(str(record["assignment"]), None)
        history = self.state["histories"].get(str(record["assignment"]))
        if history:
            merge_record(history["runs"], record, self.stop_labels)
            history["runs"] = [self.bounded_run(row) for row in history["runs"]]
            sort_runs(history["runs"])
            while len(history["runs"]) > MAX_OUTCOMES:
                history["runs"].pop(0)
                history["omitted_runs"] += 1
        assignment = self.state["assignment"]
        if not assignment or (record["assignment"], record["agent"]) != (assignment["item"], assignment["agent"]):
            self.emit()
            return
        if record["kind"] == "lease":
            if assignment["run"] not in {None, record["run"]}:
                return
            self.source_run = record.get("recovered_run")
            assignment.update(run=record["run"], runtime=record["runtime"],
                              lease_state=record["state"], lease_expires=record["expires"])
            if record.get("attempt") is not None:
                assignment["attempt"] = record["attempt"]
            if record.get("mode") == "recovery":
                assignment.update(process="recovery", process_reason="Recovery starts no agent process",
                                  recovered_run=self.source_run,
                                  paths_reason="This recovery has no process log or context")
            else:
                if record["state"] == "released" and assignment["process"] in {"starting", "running"}:
                    assignment.update(process="exited", process_reason="Supervisor released after execution ended")
                directory = self.root / ".ub-agents" / "runs" / record["run"]
                assignment.update(process_log=str(directory / "process.log"),
                                  context_path=str(directory / "context.json"), paths_reason=None)
                if record["state"] == "running" and assignment["process"] == "claiming":
                    assignment.update(process="starting", process_reason="No live process has been recorded")
            if record.get("result"):
                assignment.update(result=record["result"], summary=record.get("summary", "")[:MAX_TEXT])
            for outcome in self.state["outcomes"]:
                if outcome["run"] == (self.source_run or record["run"]):
                    if record.get("result"):
                        outcome.update(result=record["result"], summary=record.get("summary", "")[:MAX_TEXT])
        elif record["kind"] == "outcome" and record["run"] == (self.source_run or assignment["run"]):
            finalized = bool(record.get("accepted") and record.get("transition_complete"))
            acceptance = ("rejected" if record.get("rejected") else "finalized" if finalized else
                          "accepted" if record.get("accepted") else "unaccepted")
            row = {"item": record["assignment"], "agent": record["agent"], "run": record["run"],
                   "kind": assignment["kind"], "title": assignment["title"],
                   "handoff": record.get("handoff"),
                   "runtime": record.get("runtime") or assignment.get("runtime"),
                   "result": assignment.get("result", record["status"]),
                   "summary": assignment.get("summary", record["summary"]),
                   "report_result": record["status"],
                   "time": record["created"], "observed_at": iso(self.clock()),
                   "acceptance": acceptance, "rejection": record.get("rejected"),
                   "completed": finalized, "transition_complete": bool(record.get("transition_complete")),
                   "recovered": self.source_run is not None,
                   "target": record.get("handoff") or record["assignment"],
                   "human_blocker": (sorted(set(record.get("transition", {}).get("add", ()))
                                            .intersection(self.stop_labels)) if finalized else None),
                   "blocker_reason": None if finalized else "Transition is not finalized",
                   "blocker_observed_at": iso(self.clock()) if finalized else None}
            row = self.bounded(row)
            outcomes = self.state["outcomes"]
            existing = next((i for i, r in enumerate(outcomes) if r["run"] == record["run"]), None)
            if existing is not None:
                outcomes[existing] = row
            else:
                outcomes.append(row)
                if len(outcomes) > MAX_OUTCOMES:
                    outcomes.pop(0)
                    self.state["omitted"]["outcomes"] += 1
                    self.prune_histories(self.state)
        self.emit()

    def process(self, state, reason):
        if self.state["assignment"]:
            self.state["assignment"].update(process=state, process_reason=reason[:MAX_TEXT])
            self.emit()

    def clear_assignment(self):
        self.state["assignment"] = None
        if self.state["activity"]["state"] != "stopping":
            self.state["activity"] = {"state": "polling"}
        self.emit()

    def close(self):
        self.state["ended"] = True
        self.activity("stopping")
        self.publisher.close()
