"""One serial observe → claim → execute → observe → release loop."""

import json
import os
import threading
import time

from .coordination import Coordinator, Plan
from .errors import AgentError, CleanupError, LostOwnership, RecordError, ValidationError
from .execution import Workspace, command_for, supervise
from .records import attempts, iso, latest_leases, lease_summary, seconds, timestamp


class Loop:
    def __init__(self, config, github, actor, stop_event=None, output=print):
        self.config = config
        self.github = github
        self.coordinator = Coordinator(github, actor, trusted_actors=config.operators)
        self.stop_event = stop_event or threading.Event()
        self.output = output
        # The candidate cannot rewrite the operator's configured task policy.
        self.instructions = {a.name: a.instructions.read_text() if a.instructions else ""
                             for a in config.agents}

    def plans(self):
        plans = []
        items = {item.number: item for item in self.github.observe()}
        history_index, invalid = self.coordinator.repository_history()
        now = self.coordinator.clock()
        unfinished = {r["assignment"] for r in latest_leases(history_index).values()
                      if r["state"] in {"claiming", "running"} or r.get("result") in {"retry", "blocked"}}
        for number in sorted(unfinished | invalid):
            if number not in items:
                item = self.github.item(number)
                items[item.number] = item
        for item in sorted(items.values(), key=lambda item: item.number):
            matched = [a for a in self.config.agents if item.labels.intersection(a.triggers)
                       and a.kind in {"either", item.kind} and item.state == "open"]
            if not matched and item.number not in (unfinished | invalid):
                continue
            # Discovery is cached, but authority always comes from a fresh item read.
            try:
                history = self.coordinator.history(item.number)
            except RecordError as exc:
                plans.extend(Plan(item, a, None, "blocked", str(exc), 1)
                             for a in matched or self.config.agents)
                continue
            latest = latest_leases(history)
            for agent in self.config.agents:
                record = latest.get((item.number, agent.name))
                try:
                    pending = self.coordinator.pending_completion(history, agent.name, now)
                except RecordError as exc:
                    plans.append(Plan(item, agent, None, "blocked", str(exc), 1))
                    continue
                if (agent in matched or pending or (record and record["state"] in {"claiming", "running"}
                                                    and seconds(record["expires"]) > now)):
                    plans.append(self.coordinator.plan(item, agent, self.config.stop_labels, history))
                elif record and (record["state"] in {"claiming", "running"}
                                 or record.get("result") in {"retry", "blocked"}):
                    if record.get("cleanup") == "unconfirmed":
                        reason = f"Previous cleanup was unconfirmed: {lease_summary(history, record)}"
                    elif record["state"] == "released":
                        reason = f"Last run {record['result']}: {lease_summary(history, record)}"
                    else:
                        reason = "Expired run has no outcome or matching trigger"
                    plans.append(Plan(item, agent, None, "blocked",
                        f"{reason}; inspect GitHub and restore a trigger before retrying",
                        len(attempts(history, agent.name, now)) + 1))
        return plans

    def tick(self):
        for plan in self.plans():
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            if plan.state == "ready":
                if self.execute(plan):
                    return True
            elif plan.state == "recover":
                if self.recover(plan):
                    return True
            else:
                self.output(f"#{plan.item.number} {plan.agent.name}: {plan.state} — {plan.reason}")
        return False

    def execute(self, plan):
        lease = self.coordinator.claim(plan, self.config.stop_labels)
        if lease is None:
            return False
        run_dir = self.config.root / ".ub-agent" / "runs" / lease["run"]
        run_dir.mkdir(parents=True, exist_ok=True)
        workspace = Workspace(self.config, plan.agent, plan.item, lease, self.github)
        self.output(f"#{plan.item.number} {plan.agent.name}: claimed {lease['run']} ({lease['runtime']})")
        self.output(f"Logs: {run_dir}")

        def diagnostic(event, **details):
            with (run_dir / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"time": iso(timestamp()), "event": event, **details}) + "\n")

        def record_uncertainty(exc):
            diagnostic("cleanup-unconfirmed", error=str(exc))
            cause = exc
            while cause is not None:
                if isinstance(cause, LostOwnership):
                    return  # Even a transient ownership loss forbids further writes.
                cause = cause.__context__
            try:
                self.coordinator.assert_owned(lease)
                self.coordinator.update(lease, cleanup="unconfirmed", summary=str(exc))
            except AgentError as failure:
                diagnostic("cleanup-verdict-unrecorded", error=str(failure))

        def cleanup_workspace(record=True):
            try:
                workspace.cleanup()
            except CleanupError as exc:
                if record:
                    record_uncertainty(exc)
                raise

        next_renewal = time.monotonic() + plan.agent.renewal_seconds

        def heartbeat():
            nonlocal next_renewal
            if timestamp() >= seconds(lease["expires"]):
                raise LostOwnership("Local lease deadline expired")
            if time.monotonic() >= next_renewal:
                self.coordinator.renew(lease, plan.agent.lease_seconds)
                next_renewal = time.monotonic() + plan.agent.renewal_seconds
                diagnostic("renewed", expires=lease["expires"])

        interrupted = False
        result, summary = "retry", "Assignment ended without a validated outcome"
        outcome = None
        try:
            self.coordinator.assert_owned(lease)
            self.coordinator.update(lease, state="running", started=True)
            cwd = workspace.prepare()
            self.coordinator.update(lease, branch=lease.get("branch"))
            fresh = self.github.item(plan.item.number, plan.item.kind)
            if (fresh.head != plan.item.head or fresh.state != "open"
                    or not fresh.labels.intersection(plan.agent.triggers)
                    or fresh.labels.intersection(self.config.stop_labels)):
                raise AgentError("Trigger or candidate changed before execution")
            self.coordinator.assert_owned(lease)
            context = {"repository": self.config.repository, "assignment": plan.item.number,
                       "kind": plan.item.kind, "title": plan.item.title, "body": plan.item.body,
                       "candidate_sha": plan.item.head, "run": lease["run"],
                       "agent": plan.agent.name, "branch": lease.get("branch")}
            context_path = run_dir / "context.json"
            context_path.write_text(json.dumps(context, indent=2))
            env = os.environ.copy()
            env.update({"UB_AGENT_REPOSITORY": self.config.repository,
                        "UB_AGENT_ASSIGNMENT": str(plan.item.number),
                        "UB_AGENT_RUN": lease["run"], "UB_AGENT_LEASE_ID": str(lease["id"]),
                        "UB_AGENT_CONTEXT": str(context_path),
                        "UB_AGENT_OPERATORS": json.dumps(sorted(self.coordinator.trusted_actors)),
                        "UB_AGENT_CANDIDATE_SHA": plan.item.head or "",
                        "UB_AGENT_BRANCH": lease.get("branch") or ""})
            instructions = self.instructions[plan.agent.name]
            prompt = (f"You are the project-configured agent {plan.agent.name}.\n"
                      f"Assignment context:\n{json.dumps(context, indent=2)}\n\n"
                      f"Project instructions:\n{instructions}\n\n"
                      "Read shared repository guidance and the original issue requirements, acceptance "
                      "criteria, current code/diff, and candidate-specific checks on GitHub. "
                      "Use a fresh session; do not consume implementation reasoning transcripts. "
                      "Do not renew claims or start detached heartbeat helpers. The launcher owns renewal. "
                      "Apply only project-authorized handoffs and permissions. Remove the triggering "
                      "labels before reporting success, or close the assignment when policy permits. "
                      "Record the explicit durable outcome with ub-agent report --status "
                      "success|retry|blocked --summary 'what happened' [--handoff PR_NUMBER]. "
                      "Issue-to-PR handoffs must link the issue in the PR body. "
                      "For candidate acceptance, results and checks must name the assigned SHA.\n")
            diagnostic("started", cwd=str(cwd))
            code = supervise(command_for(plan.agent, plan.runtime), cwd, env, run_dir,
                             plan.agent.timeout_seconds, heartbeat, self.stop_event,
                             prompt if plan.runtime else None)
            # No acceptance or release until all attributable execution has ended.
            workspace.cleanup()
            self.coordinator.assert_owned(lease)
            outcome = self.coordinator.outcome(lease)
            if outcome is None:
                result = "blocked" if code != 0 else "retry"
                summary = f"Execution exited {code} without an explicit GitHub outcome; inspect process.log"
            elif outcome["status"] != "success":
                result, summary = outcome["status"], outcome["summary"]
            elif code != 0:
                result, summary = "blocked", f"Success report conflicts with execution exit {code}"
            else:
                try:
                    self.validate_success(plan, outcome)
                except ValidationError:
                    result = "blocked"
                    raise
                except AgentError as exc:
                    raise LostOwnership(f"Cannot observe completion; leave expiry recovery: {exc}") from exc
                try:
                    self.coordinator.accept(lease, outcome)
                except AgentError as exc:
                    raise LostOwnership(f"Cannot finalize durable outcome; leave expiry recovery: {exc}") from exc
                result, summary = "success", outcome["summary"]
        except CleanupError as exc:
            record_uncertainty(exc)
            raise
        except LostOwnership as exc:
            diagnostic("ownership-lost", error=str(exc))
            cleanup_workspace(record=False)
            # Do not renew, report, release, or accept after losing ownership.
            raise
        except KeyboardInterrupt:
            interrupted = True
            summary = "Launcher interrupted; attributable execution terminated"
            cleanup_workspace()
        except (AgentError, OSError) as exc:
            summary = str(exc)
            cleanup_workspace()
        delay = min(plan.agent.max_backoff_seconds,
                    plan.agent.backoff_seconds * (2 ** min(lease["attempt"] - 1, 32))) if result == "retry" else 0
        # Framework failures are themselves explicit durable outcomes. If GitHub is
        # unreadable, this fails closed and the last lease expires without a lie.
        if outcome is None:
            # A report can precede a timeout/interruption. Keep that report
            # unaccepted and persist the supervisor's actual verdict on the lease.
            outcome = self.coordinator.outcome(lease)
        if outcome is None:
            self.coordinator.report(lease, result, summary)
        self.coordinator.release(lease, result, summary, delay)
        diagnostic("released", result=result, summary=summary)
        self.output(f"#{plan.item.number} {plan.agent.name}: {result} — {summary}")
        if interrupted:
            raise KeyboardInterrupt
        return True

    def validate_success(self, plan, outcome):
        current = self.github.item(plan.item.number, plan.item.kind)
        if (current.state == "open" and current.labels.intersection(plan.agent.triggers)
                and not (plan.item.kind == "issue" and outcome.get("handoff"))):
            raise ValidationError("Success report left assignment trigger labels in place")
        destination = (self.github.item(outcome["handoff"], "pr")
                       if outcome.get("handoff") else current)
        if outcome["candidate_sha"] != destination.head:
            raise ValidationError("Outcome candidate SHA does not match the observed PR head")
        if plan.agent.different_from and current.head != plan.item.head:
            raise ValidationError("Independent result is stale: the assigned candidate moved")
        if outcome.get("handoff") and plan.item.kind == "issue":
            import re
            issue = plan.item.number
            link = f"https://github.com/{self.config.repository}/issues/{issue}"
            if not re.search(rf"(?<![\w/])#{issue}\b", destination.body) and link not in destination.body:
                raise ValidationError("Implementation PR body does not link its original issue")

    def recover(self, plan):
        history = self.coordinator.history(plan.item.number)
        outcome = self.coordinator.pending_completion(history, plan.agent.name, self.coordinator.clock())
        if outcome is None:
            return False
        recovery = self.coordinator.claim(plan, self.config.stop_labels, recovery=True)
        if recovery is None:
            return False
        result, summary = outcome["status"], outcome["summary"]
        if result == "success":
            from dataclasses import replace
            original = replace(plan.item, head=outcome["assignment_sha"])
            try:
                if plan.item.labels.intersection(self.config.stop_labels):
                    raise ValidationError("A configured stop label now parks the work")
                self.validate_success(replace(plan, item=original), outcome)
            except ValidationError as exc:
                result, summary = "blocked", f"Recorded outcome cannot be recovered: {exc}"
            except AgentError as exc:
                raise LostOwnership(f"Cannot observe recovered completion; leave expiry recovery: {exc}") from exc
            else:
                self.coordinator.accept(recovery, outcome)
        self.coordinator.update(recovery, recovered_run=outcome["run"],
                                recovered_lease_id=outcome["lease_id"])
        self.coordinator.report(recovery, result, f"Recovered {outcome['run']}: {summary}")
        # Expiry permits recovery; it is not positive proof of the old process's death.
        delay = min(plan.agent.max_backoff_seconds, plan.agent.backoff_seconds
                    * (2 ** min(max(0, plan.attempt - 2), 32))) if result == "retry" else 0
        self.coordinator.release(recovery, result, f"Recovered {outcome['run']}: {summary}", delay)
        self.output(f"#{plan.item.number} {plan.agent.name}: recovered durable outcome; no execution started")
        return True

    def launch(self, once=False):
        while not self.stop_event.is_set():
            worked = self.tick()
            if once:
                return
            if not worked:
                self.output("Waiting for eligible GitHub work")
                self.stop_event.wait(self.config.poll_seconds)
        raise KeyboardInterrupt
