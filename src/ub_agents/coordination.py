"""Cooperative leases and durable attempts, deliberately not an atomic lock service."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import uuid

from .config import Agent, Runtime
from .errors import AgentError, LostOwnership, RecordError, ValidationError
from .github import Item, links_issue
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
    resume_pr: Item | None = None


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
        resume_pr = None
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
        else:
            try:
                if item.kind == "issue":
                    resume_pr = self.reusable_issue_pr(item, agent, history)
                elif self.pr_owners(item.number):
                    raise ValidationError("A live issue run owns this PR checkpoint")
                if finished and finished[-1].get("result") == "blocked":
                    state, reason = "blocked", "Previous assignment stopped; inspect outcome and use ub-agent retry"
                elif attempt > agent.max_attempts:
                    state, reason = "blocked", "Attempt limit exhausted; inspect failures and use ub-agent retry"
                elif finished and seconds(finished[-1].get("retry_after", finished[-1]["expires"])) > now:
                    state, reason = "backoff", "Durable retry backoff has not elapsed"
                else:
                    try:
                        runtime = self.choose_runtime(item, agent, history)
                    except AgentError as exc:
                        state, reason = "blocked", str(exc)
                    else:
                        if resume_pr:
                            reason = f"Resume draft PR #{resume_pr.number} on {resume_pr.branch}"
            except ValidationError as exc:
                state, reason = "blocked", str(exc)
        return Plan(item, agent, runtime, state, reason, attempt, resume_pr)

    def prior_issue_prs(self, item, agent, history):
        # Resets clear attempt/completion gates, not branch history. Check every
        # recorded branch for this issue/agent, even if a newer lease superseded it.
        branches = sorted({r["branch"] for r in history if r["kind"] == "lease"
                           and r["assignment"] == item.number and r["agent"] == agent.name
                           and r.get("branch")})
        prs = {pr.number: pr for branch in branches for pr in self.github.prs_for_branch(branch)}
        return sorted(prs.values(), key=lambda pr: pr.number)

    def pr_owners(self, number):
        # The repository scan only discovers related issues. Fresh comments, even
        # before resets, establish ownership of the shared checkpoint branch.
        index, _ = self.repository_history()
        assignments = {number} | {r["assignment"] for r in index
                                  if r["kind"] == "lease" and r.get("resume_pr") == number}
        return sorted((r for assignment in assignments for r in
                       live_leases(self.history(assignment), self.clock())
                       if r["assignment"] == number or r.get("resume_pr") == number),
                      key=lambda r: r["id"])

    def reusable_issue_pr(self, item, agent, history):
        prs = self.prior_issue_prs(item, agent, history)
        if not prs:
            return None
        numbers = ", ".join(f"#{pr.number}" for pr in prs)
        problem = None
        if len(prs) != 1:
            problem = "multiple open PRs"
        else:
            pr = self.github.item(prs[0].number, "pr")
            branches = {r.get("branch") for r in history if r["kind"] == "lease"
                        and r["assignment"] == item.number and r["agent"] == agent.name}
            if (pr.state != "open" or pr.merged or not pr.draft or not pr.head
                    or not pr.branch or pr.branch not in branches):
                problem = "PR is no longer a reusable draft on a recorded branch"
            elif pr.head_repository != self.github.repository:
                problem = "PR head is not in this repository"
            elif not links_issue(pr, self.github.repository, item.number):
                problem = "PR body does not link the issue"
            elif not agent.worktree:
                problem = "resuming requires a private worktree"
            elif self.pr_owners(pr.number):
                problem = "PR checkpoint has conflicting live ownership"
            else:
                pr_history = self.history(pr.number)
                if any(r.get("cleanup") == "unconfirmed" for r in latest_leases(pr_history).values()):
                    problem = "PR cleanup is unconfirmed"
                elif any(self.pending_completion(pr_history, r["agent"], self.clock())
                         for r in latest_leases(pr_history).values()):
                    problem = "PR has an outcome awaiting recovery"
            if problem is None:
                return pr
        raise ValidationError(f"Cannot safely resume recorded PR {numbers}: {problem}; inspect and continue "
                              "the existing PR manually. If abandoned, close it before "
                              f"ub-agent retry --number {item.number} --agent {agent.name} --reason TEXT")

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
        if fresh.state != ("recover" if recovery else "ready") or (not recovery and
                (fresh.runtime != plan.runtime or fresh.resume_pr != plan.resume_pr)):
            return None
        now = self.clock()
        record = {"kind": "lease", "run": uuid.uuid4().hex,
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
            source = next(r for r in history if r["kind"] == "lease" and r["id"] == outcome["lease_id"])
            if source.get("resume_pr"):
                record |= {k: source[k] for k in ("resume_pr", "resume_sha", "branch")}
        elif fresh.resume_pr:
            record |= {"branch": fresh.resume_pr.branch, "resume_pr": fresh.resume_pr.number,
                       "resume_sha": fresh.resume_pr.head}
        created = records([self.github.create_comment(current.number, body(record))], self.trusted_actors)[0]
        contenders = live_leases(self.history(current.number), self.clock())
        # Earliest GitHub comment id wins. Each contender has its own record; no
        # read-modify-write race on a shared lease comment is passed off as CAS.
        if not contenders or contenders[0]["id"] != created["id"]:
            self.update(created, state="withdrawn", summary="Lost the cooperative claim election.")
            return None
        pr_number = created.get("resume_pr") or (current.number if current.kind == "pr" else None)
        if pr_number:
            owners = self.pr_owners(pr_number)
            if owners and owners[0]["id"] != created["id"]:
                self.update(created, state="withdrawn", summary="Lost the checkpoint claim election.")
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
        pr_number = lease.get("resume_pr") or (lease["assignment"] if lease["assignment_sha"] else None)
        if pr_number:
            try:
                owners = self.pr_owners(pr_number)
            except AgentError as exc:
                raise LostOwnership(f"Cannot establish checkpoint ownership: {exc}") from exc
            if not owners or owners[0]["id"] != lease["id"]:
                raise LostOwnership("Checkpoint ownership was lost or expired")
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
        reported = self.outcome(lease)
        if reported and (reported["status"], reported["summary"]) == (result, summary):
            summary = None  # The outcome comment already says it.
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
        record |= {"kind": "outcome", "lease_id": lease["id"],
                   "created": iso(self.clock()), "status": status, "summary": summary,
                   "handoff": handoff, "candidate_sha": destination.head, "accepted": False}
        if lease.get("resume_pr"):
            record["resume_pr"] = lease["resume_pr"]
        self.assert_owned(lease)
        return records([self.github.create_comment(lease["assignment"], body(record))], self.trusted_actors)[0]

    def accept(self, lease, outcome):
        self.assert_owned(lease)
        accepted = payload(outcome) | {"accepted": True}
        self.github.update_comment(outcome["id"], body(accepted))
        if outcome.get("handoff") and outcome["handoff"] != lease["assignment"]:
            self.github.create_comment(outcome["handoff"], body(accepted | {"recorded_by": self.actor}))
