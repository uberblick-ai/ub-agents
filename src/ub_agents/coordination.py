"""Cooperative leases and durable attempts, deliberately not an atomic lock service."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import uuid

from .config import Agent, Runtime
from .errors import AgentError, LostOwnership, RecordError
from .github import Item
from .records import (MARKER, attempts, body, iso, latest_leases, live_leases,
                      payload, records, seconds, timestamp, trusted_comment)


@dataclass(frozen=True)
class Plan:
    item: Item
    agent: Agent
    runtime: Runtime | None
    state: str
    reason: str
    attempt: int


class Coordinator:
    def __init__(self, github, actor, clock=timestamp, trusted_actors=()):
        self.github = github
        self.actor = actor
        self.clock = clock
        self.trusted_actors = {login.casefold() for login in (*trusted_actors, actor)}

    def history(self, number):
        return records(self.github.comments(number), self.trusted_actors)

    def repository_history(self):
        groups = {}
        for comment in self.github.repository_comments():
            if not isinstance(comment, dict):
                raise AgentError("Unreadable repository comment")
            if not trusted_comment(comment, self.trusted_actors):
                continue
            if not isinstance(comment.get("body"), str) or not comment["body"].startswith(MARKER):
                continue
            try:
                number = int(comment["issue_url"].rsplit("/", 1)[1])
            except (KeyError, ValueError, AttributeError) as exc:
                raise AgentError("Coordination comment has no GitHub assignment URL") from exc
            groups.setdefault(number, []).append(comment)
        history, invalid = [], set()
        for number, comments in groups.items():
            try:
                history.extend(records(comments, self.trusted_actors))
            except RecordError:
                invalid.add(number)
        return sorted(history, key=lambda record: record["id"]), invalid

    def plan(self, item, agent, stop_labels, history=None):
        history = self.history(item.number) if history is None else history
        now = self.clock()
        previous = attempts(history, agent.name, now)
        latest = [r for r in latest_leases(history).values() if r["agent"] == agent.name]
        finished = [r for r in latest if r["state"] == "released"]
        attempt = len(previous) + 1
        state, reason = "ready", "Trigger matched"
        runtime = None
        if live_leases(history, now):
            state, reason = "owned", "An unexpired assignment owns this work item"
        elif self.pending_completion(history, agent.name, now):
            state, reason = "recover", "An expired run has an explicit outcome to validate without reexecution"
        elif latest and latest[-1].get("cleanup") == "unconfirmed":
            state, reason = "blocked", "Previous cleanup was unconfirmed; establish termination before an operator reset"
        elif item.labels.intersection(stop_labels):
            state, reason = "parked", "Configured stop label is present"
        elif item.kind == "issue" and any(r["kind"] == "outcome" and r["agent"] == agent.name
                and r.get("accepted") is True and r["status"] == "success"
                and r.get("handoff") and self.released_success(history, r)
                and r["id"] > self.reset_boundary(history, agent.name) for r in history):
            state, reason = "completed", "Issue assignment completed; durable handoff suppresses duplicate pickup"
        elif finished and finished[-1].get("result") == "blocked":
            state, reason = "blocked", "Previous assignment stopped; inspect outcome and use ub-agent retry"
        elif attempt > agent.max_attempts:
            state, reason = "blocked", "Attempt limit exhausted; inspect failures and use ub-agent retry"
        elif finished and seconds(finished[-1].get("retry_after", finished[-1]["expires"])) > now:
            state, reason = "backoff", "Durable retry backoff has not elapsed"
        elif (item.kind == "issue" and latest and latest[-1].get("branch")
              and self.github.prs_for_branch(latest[-1]["branch"])):
            state, reason = "blocked", "Previous run branch already has an open PR; inspect its handoff before retrying"
        else:
            try:
                runtime = self.choose_runtime(item, agent, history)
            except AgentError as exc:
                state, reason = "blocked", str(exc)
        return Plan(item, agent, runtime, state, reason, attempt)

    def choose_runtime(self, item, agent, history):
        def installed(executable):
            if "/" in executable:
                path = Path(executable) if Path(executable).is_absolute() else agent.cwd / executable
                return path.is_file() and os.access(path, os.X_OK)
            return bool(shutil.which(executable))

        if agent.command:
            if not installed(agent.command[0]):
                raise AgentError(f"Command is not installed: {agent.command[0]}")
            return None
        eligible = list(agent.runtimes)
        if agent.different_from:
            if item.kind != "pr":
                raise AgentError("Independent candidate execution requires a PR")
            source = [r for r in history if r["kind"] == "outcome" and r["status"] == "success"
                      and r.get("accepted") is True and r["agent"] == agent.different_from
                      and r.get("candidate_sha") == item.head]
            if not source:
                raise AgentError(f"No accepted {agent.different_from} provenance for candidate {item.head}")
            source = source[-1]
            origin = self.history(source["assignment"])
            lease = next((r for r in origin if r["kind"] == "lease" and r["id"] == source["lease_id"]), None)
            if lease and any(lease.get(k) != source.get(k) for k in
                             ("run", "agent", "actor", "runtime", "provider", "assignment", "assignment_sha")):
                raise AgentError("Candidate provenance does not match its source lease")
            if not self.released_success(origin, source):
                raise AgentError("Candidate provenance has no successfully released source lease")
            try:
                cli, model, effort = source["runtime"].split(":")
                prior = Runtime(cli, model, effort, source["provider"])
            except (KeyError, ValueError) as exc:
                raise AgentError("Candidate runtime provenance is incomplete") from exc
            eligible = [r for r in eligible if r.different_from(prior)]
            if not eligible:
                raise AgentError("No runtime has a different CLI, provider, and model from the candidate author")
        for runtime in eligible:
            executable = runtime.command[0] if runtime.command else runtime.cli
            if installed(executable):
                return runtime
        raise AgentError("No eligible runtime executable is installed")

    @staticmethod
    def reset_boundary(history, agent):
        return max((r["id"] for r in history if r["kind"] == "reset" and r["agent"] == agent), default=0)

    @staticmethod
    def released_success(history, outcome):
        source = next((r for r in history if r["kind"] == "lease" and r["id"] == outcome["lease_id"]), None)
        if (source is None or source.get("cleanup") == "unconfirmed"
                or any(source.get(k) != outcome.get(k) for k in
                       ("run", "agent", "actor", "runtime", "provider", "assignment", "assignment_sha"))):
            return False
        return ((source["state"] == "released" and source.get("result") == "success")
                or any(r["kind"] == "lease" and r["state"] == "released" and r.get("result") == "success"
                       and r.get("recovered_lease_id") == source["id"] for r in history))

    def pending_completion(self, history, agent, now):
        latest = [r for r in latest_leases(history).values() if r["agent"] == agent]
        if not latest:
            return None
        lease = latest[-1]
        if lease["state"] not in {"running", "claiming"} or seconds(lease["expires"]) > now:
            return None
        source_id = lease.get("recovered_lease_id", lease["id"])
        source = next((r for r in history if r["kind"] == "lease" and r["id"] == source_id), None)
        if source is None or source["state"] not in {"running", "claiming"} or source.get("cleanup") == "unconfirmed":
            return None
        matches = [outcome for outcome in history
                   if (outcome["kind"] == "outcome" and outcome["lease_id"] == source_id
                    and all(outcome.get(k) == source.get(k) for k in
                            ("run", "agent", "actor", "runtime", "provider", "assignment", "assignment_sha"))
                    and seconds(outcome["created"]) <= seconds(source["expires"]))]
        if len(matches) > 1:
            raise RecordError("Expired run reported conflicting outcomes; inspect GitHub before resetting")
        return matches[0] if matches else None

    def claim(self, plan, stop_labels=(), recovery=False):
        # Reobserve state immediately before claiming. This also handles a label/head
        # changing after queue enumeration, without charging an attempt.
        current = self.github.item(plan.item.number, plan.item.kind)
        if current.head != plan.item.head or (not recovery and
                (current.state != "open" or not current.labels.intersection(plan.agent.triggers))):
            return None
        history = self.history(current.number)
        fresh = self.plan(current, plan.agent, stop_labels, history)
        if fresh.state != ("recover" if recovery else "ready") or (not recovery and fresh.runtime != plan.runtime):
            return None
        now = self.clock()
        record = {"version": 1, "kind": "lease", "run": uuid.uuid4().hex,
                  "agent": plan.agent.name, "assignment": current.number,
                  "assignment_sha": current.head,
                  "branch": current.branch, "runtime": plan.runtime.name if plan.runtime else "direct",
                  "triggers": sorted(current.labels.intersection(plan.agent.triggers)),
                  "provider": plan.runtime.provider if plan.runtime else "direct",
                  "actor": self.actor, "created": iso(now),
                  "expires": iso(now + plan.agent.lease_seconds), "state": "claiming",
                  "attempt": len(attempts(history, plan.agent.name, now)) + 1, "started": False}
        if recovery:
            outcome = self.pending_completion(history, plan.agent.name, now)
            if outcome is None:
                return None
            record |= {"mode": "recovery", "recovered_lease_id": outcome["lease_id"],
                       "recovered_run": outcome["run"]}
        created = records([self.github.create_comment(current.number, body(record))], self.trusted_actors)[0]
        contenders = live_leases(self.history(current.number), self.clock())
        # Earliest GitHub comment id wins. Each contender has its own record; no
        # read-modify-write race on a shared lease comment is passed off as CAS.
        if not contenders or contenders[0]["id"] != created["id"]:
            self.update(created, state="withdrawn", summary="Lost the cooperative claim election.")
            return None
        if self.clock() >= seconds(created["expires"]):
            raise LostOwnership("Lease expired during claiming")
        return created

    def update(self, lease, **changes):
        updated = payload(lease) | changes
        result = records([self.github.update_comment(lease["id"], body(updated))], self.trusted_actors)[0]
        lease.clear()
        lease.update(result)
        return lease

    def assert_owned(self, lease):
        try:
            contenders = live_leases(self.history(lease["assignment"]), self.clock())
        except AgentError as exc:
            raise LostOwnership(f"Cannot establish ownership: {exc}") from exc
        if (not contenders or contenders[0]["id"] != lease["id"]
                or contenders[0]["run"] != lease["run"]
                or contenders[0]["actor"].casefold() != self.actor.casefold()
                or any(contenders[0].get(k) != lease.get(k) for k in
                       ("agent", "runtime", "provider", "assignment", "assignment_sha"))):
            raise LostOwnership("Assignment ownership was lost or expired")
        return contenders[0]

    def renew(self, lease, duration):
        self.assert_owned(lease)
        # Reuse the same durable comment. No model or detached renewal helper.
        try:
            self.update(lease, expires=iso(self.clock() + duration))
        except AgentError as exc:
            raise LostOwnership(f"Lease renewal failed: {exc}") from exc
        self.assert_owned(lease)

    def release(self, lease, result, summary, backoff=0):
        self.assert_owned(lease)
        now = self.clock()
        # A released lease expires now; retry_after is only needed for a backoff.
        self.update(lease, state="released", result=result, summary=summary, expires=iso(now),
                    retry_after=iso(now + backoff) if backoff else None)

    def outcome(self, lease):
        matches = [r for r in self.history(lease["assignment"])
                   if r["kind"] == "outcome" and r["run"] == lease["run"]
                   and r["lease_id"] == lease["id"] and r["actor"] == lease["actor"]
                   and r["assignment_sha"] == lease["assignment_sha"]
                   and r["agent"] == lease["agent"] and r["runtime"] == lease["runtime"]
                   and r.get("provider") == lease["provider"]]
        if len(matches) > 1:
            raise RecordError("Run reported conflicting outcomes")
        return matches[0] if matches else None

    def report(self, lease, status, summary, handoff=None):
        self.assert_owned(lease)
        if self.outcome(lease):
            raise AgentError("This run already has an outcome")
        destination = self.github.item(handoff, "pr") if handoff else self.github.item(lease["assignment"])
        record = {k: lease[k] for k in ("run", "agent", "assignment", "assignment_sha",
                                        "runtime", "provider", "actor")}
        record |= {"version": 1, "kind": "outcome", "lease_id": lease["id"],
                   "created": iso(self.clock()), "status": status, "summary": summary,
                   "handoff": handoff, "candidate_sha": destination.head, "accepted": False}
        self.assert_owned(lease)
        return records([self.github.create_comment(lease["assignment"], body(record))], self.trusted_actors)[0]

    def accept(self, lease, outcome):
        self.assert_owned(lease)
        accepted = payload(outcome) | {"accepted": True}
        self.github.update_comment(outcome["id"], body(accepted))
        if outcome.get("handoff") and outcome["handoff"] != lease["assignment"]:
            self.github.create_comment(outcome["handoff"], body(accepted | {"recorded_by": self.actor}))
