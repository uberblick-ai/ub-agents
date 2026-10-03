"""One read-only evaluation and bounded action for CLI and launcher recovery."""

from dataclasses import dataclass, field, replace
import subprocess
import uuid

from .coordination import Plan
from .errors import AgentError, TransitionPaused, ValidationError
from .records import (attempts, backoff, body, fingerprint, iso, latest_leases,
                      lease_by_id, live_leases, records, same_run, seconds)
from . import shutdown


@dataclass
class Recovery:
    number: int
    agent: str
    entry_point: str
    lease: dict | None = None
    source: dict | None = None
    recovery_lease: dict | None = None
    outcome: dict | None = None
    evidence: list[dict] = field(default_factory=list)
    result: str = "refused or waiting"
    reason: str = ""
    proposed: str = "Keep the claim"
    applied: bool = False

    @property
    def eligible(self):
        return bool(self.evidence) and all(check["ok"] for check in self.evidence)

    def check(self, name, operation):
        try:
            detail = operation()
            self.evidence.append({"check": name, "ok": True, "detail": detail or "confirmed"})
        except (AgentError, OSError, ValueError, KeyError, IndexError, TypeError,
                subprocess.SubprocessError) as exc:
            self.evidence.append({"check": name, "ok": False, "detail": str(exc)})
            if not self.reason:
                self.reason = f"{name}: {exc}"

    def line(self):
        active = self.recovery_lease or self.lease
        expiry = f"; lease expires {active['expires']}" if active and not self.applied else ""
        return f"#{self.number} {self.agent}: {self.result} — {self.reason}{expiry}"

    def render(self, output):
        output(f"Item #{self.number}; role {self.agent}; entry point {self.entry_point}")
        if self.lease:
            lease = self.lease
            output(f"Lease comment {lease['id']}; run {lease['run']}; attempt {lease['attempt']}; "
                   f"actor @{lease['actor']}; host {lease.get('host', 'unknown')}; "
                   f"identity {lease.get('host_identity', 'legacy/ambiguous')}; expires {lease['expires']}")
        if self.source and self.lease and self.source["id"] != self.lease["id"]:
            output(f"Original lease comment {self.source['id']}; run {self.source['run']}")
        if self.recovery_lease:
            output(f"Recovery claim comment {self.recovery_lease['id']}; run {self.recovery_lease['run']}; "
                   f"state {self.recovery_lease['state']}; expires {self.recovery_lease['expires']}")
        if self.outcome:
            outcome = self.outcome
            acceptance = "accepted" if outcome["accepted"] else "unaccepted"
            rejection = f"; rejected: {outcome['rejected']}" if outcome.get("rejected") else ""
            output(f"Reported outcome {outcome['id']}: {outcome.get('outcome', outcome['status'])}; "
                   f"{acceptance}{rejection}; {outcome['summary']}")
        else:
            output("Reported outcome: none")
        for check in self.evidence:
            output(f"  {check['check']}: {'pass' if check['ok'] else 'fail'} — {check['detail']}")
        output(f"Proposed action: {self.proposed}")
        output(self.line())


def _require(condition, reason):
    if not condition:
        raise AgentError(reason)


def inspect(loop, number, agent, entry_point="cli"):
    co = loop.coordinator
    decision = Recovery(number, agent.name, entry_point)
    history = []

    def read_history():
        nonlocal history
        history = co.history(number)
        decision.lease = latest_leases(history).get((number, agent.name))
        if decision.lease is None:
            other = records(co.github.comments(number))
            decision.lease = latest_leases(other).get((number, agent.name))
            if decision.lease is not None:
                history = other  # Display the foreign lease, but actor check refuses it.
        _require(decision.lease is not None, "No current lease for this actor and role")

    decision.check("coordination history", read_history)
    lease = decision.lease
    if lease is None:
        return decision
    source = lease_by_id(history, lease.get("recovered_lease_id", lease["id"]))
    decision.source = source
    decision.check("source lease", lambda: _require(source is not None, "Original lease is missing"))
    if source is None:
        return decision
    matches = [r for r in history if r["kind"] == "outcome" and r["lease_id"] == source["id"]
               and same_run(r, source) and seconds(r["created"]) <= seconds(source["expires"])]
    decision.outcome = matches[0] if len(matches) == 1 else None
    decision.check("reported outcome", lambda: _require(len(matches) <= 1, "Conflicting reports"))
    decision.check("actor", lambda: _require(lease["actor"].casefold() == co.actor.casefold()
                   and source["actor"].casefold() == co.actor.casefold(), "Lease belongs to another actor"))
    if lease["state"] == "released" and lease.get("supersedes_lease_id") is not None and decision.eligible:
        recorded = lease.get("recovery", {})
        if recorded.get("result"):
            decision.result = recorded["result"]
            decision.reason = f"Already recovered: {recorded['summary']}"
            decision.proposed = "Recovery already finished; no writes needed"
            decision.applied = True
            return decision
    decision.check("unfinished lease", lambda: _require(lease["state"] in {"claiming", "running"},
                                                        "Lease already released"))
    decision.check("lease cleanup verdict", lambda: _require(lease.get("cleanup") != "unconfirmed"
                   and source.get("cleanup") != "unconfirmed", "Lease records unconfirmed cleanup"))
    # Only a crashed early-recovery claim may use expiry here. Legacy execution
    # leases retain the existing expiry planner; this operation never force-clears.
    expired = seconds(lease["expires"]) <= co.clock() and lease.get("supersedes_lease_id") is not None
    for observed in (source,) if source["id"] == lease["id"] else (source, lease):
        label = "source" if observed["id"] == source["id"] else "recoverer"
        if expired:
            decision.check(f"{label} shutdown authority", lambda: "recovery claim expired")
            continue

        def host(observed=observed):
            identity = observed.get("host_identity")
            _require(shutdown.valid_host(identity), "Missing or ambiguous machine/boot identity; wait for expiry")
            _require(identity == shutdown.host_identity(), "Another machine or boot owns this lease")

        def supervisor(observed=observed):
            identity = observed.get("supervisor")
            _require(shutdown.valid_supervisor(identity), "Missing supervisor birth identity; wait for expiry")
            current = shutdown.process_identity(identity["pid"])
            if current is not None:
                raise AgentError("Supervisor is still alive" if current == identity else "Supervisor PID was reused")

        decision.check(f"{label} host identity", host)
        decision.check(f"{label} supervisor exited", supervisor)
        decision.check(f"{label} agent group stopped", lambda observed=observed:
                       shutdown.agent_group_stopped(loop.config, observed))
        decision.check(f"{label} hook groups confirmed stopped", lambda observed=observed:
                       shutdown.hook_groups_stopped(loop.config, observed))
        decision.check(f"{label} durable cleanup confirmation", lambda observed=observed:
                       shutdown.cleanup_confirmed(loop.config, observed))
        decision.check(f"{label} cleanup diagnostics", lambda observed=observed:
                       shutdown.diagnostics_confirmed(loop.config, observed))

    def exact():
        branch_history = co.history(number)
        item = co.github.item(number)
        _require(co.shared_branch_owner(item, agent, branch_history) is None,
                 "Another live lease or unconfirmed cleanup on the shared branch requires waiting")
        fresh = co.history(number)
        current = latest_leases(fresh).get((number, agent.name))
        _require(current is not None and current["id"] == lease["id"]
                 and fingerprint(current) == fingerprint(lease), "Lease was updated, released or replaced")
        others = [r for r in live_leases(fresh, co.clock()) if r["id"] != lease["id"]]
        _require(not others, "Another live lease on this item requires waiting")

    decision.check("exact claim and branch ownership", exact)
    if not decision.eligible:
        return decision
    verdict = lease if lease.get("result") in {"retry", "blocked"} else source
    if verdict.get("result") in {"retry", "blocked"} and verdict.get("attempt_effect") in {"failure", "unchanged"}:
        decision.proposed = f"Release with persisted supervisor verdict {verdict['result']} ({verdict['attempt_effect']})"
        decision.result = "preview: interrupted work released for normal retry" if verdict["result"] == "retry" else "preview: recovered outcome rejected (blocked or parked)"
    elif decision.outcome:
        decision.proposed = "Validate the reported outcome and resume normal finalization; accept only if valid"
        decision.result = ("preview: recovered completion (acceptance pending validation)" if decision.outcome["status"] == "success" and not decision.outcome.get("rejected")
                           else "preview: recovered outcome rejected (blocked or parked)" if decision.outcome["status"] == "blocked" or decision.outcome.get("rejected")
                           else "preview: interrupted work released for normal retry")
    else:
        decision.proposed = "Record interruption and release for normal retry (+1 consecutive failure, bounded backoff)"
        decision.result = "preview: interrupted work released for normal retry"
    decision.reason = "Eligible; preview only, no recovery applied"
    return decision


def apply(loop, number, agent, entry_point="cli", before_write=None):
    # Preview evidence never authorizes a write: apply always evaluates anew.
    decision = inspect(loop, number, agent, entry_point)
    if decision.applied or not decision.eligible:
        return decision
    co = loop.coordinator
    target, source = decision.lease, decision.source
    now = co.clock()
    claim = {"kind": "lease", "mode": "recovery", "run": uuid.uuid4().hex,
             "agent": agent.name, "actor": co.actor, "assignment": number,
             "assignment_sha": source.get("assignment_sha"), "branch": source.get("branch"),
             "runtime": source["runtime"], "created": iso(now), "expires": iso(now + agent.lease_seconds),
             "state": "claiming", "started": False, "attempt": source["attempt"], "attempt_effect": "pending",
             "recovered_lease_id": source["id"], "recovered_run": source["run"],
             "supersedes_lease_id": target["id"], "observed_lease": fingerprint(target),
             "recovery": {"entry_point": entry_point, "actor": co.actor, "evidence": decision.evidence,
                          "lease_id": target["id"], "run": target["run"]}, **shutdown.identity_fields()}
    if target.get("result") in {"retry", "blocked"} and target.get("attempt_effect") in {"failure", "unchanged"}:
        # Carry a crashed recoverer's verdict in the claim itself. Supersession
        # must not temporarily hide its durable failure or extend its backoff.
        claim |= {key: target.get(key) for key in ("result", "summary", "attempt_effect", "retry_after", "unreported")}
    try:
        item = co.github.item(number)
        if before_write is not None:
            before_write()
        # Last read immediately before the claim write, including every branch
        # reservation. Election then resolves concurrently-created contenders.
        fresh = inspect(loop, number, agent, entry_point)
        if not fresh.eligible or fingerprint(fresh.lease) != fingerprint(target):
            fresh.reason = fresh.reason if not fresh.eligible else "Lease changed before recovery claim"
            fresh.result = "refused or waiting"
            return fresh
        decision = fresh
        source = decision.source
        claim["recovery"]["evidence"] = decision.evidence
        recovery = records([co.github.create_comment(number, body(claim))], co.actor)[0]
        decision.recovery_lease = recovery
        if co.on_claim is not None:
            co.on_claim(recovery)
        co.assert_owned(recovery)
        # Recovery executes no processes or hooks. Its own evidence allows a
        # later recoverer to prove shutdown, once this supervisor exits.
        shutdown.confirm_cleanup(loop.config, recovery)
        outcome = co.outcome(source)
        if outcome and seconds(outcome["created"]) > seconds(source["expires"]):
            outcome = None
        decision.outcome = outcome
        verdict = target if target.get("result") in {"retry", "blocked"} else source
        persisted = verdict.get("result") in {"retry", "blocked"} and verdict.get("attempt_effect") in {"failure", "unchanged"}
        if persisted:
            result, summary, effect = verdict["result"], verdict.get("summary", "Supervisor interrupted"), verdict["attempt_effect"]
        elif outcome is None:
            result, summary, effect = "retry", "Interrupted run ended without an explicit outcome", "failure"
        else:
            result, summary = outcome["status"], outcome["summary"]
            effect = "unchanged" if result == "blocked" else "failure"
            if result == "success":
                plan = Plan(replace(item, head=source.get("assignment_sha")), agent, None, "recover", "", source["attempt"])
                try:
                    loop.finalize(recovery, plan, outcome, "recovered completion")
                    effect = "reset"
                except ValidationError as exc:
                    result, summary = "blocked", str(exc)
                    effect = "unchanged" if isinstance(exc, TransitionPaused) else "failure"
                except (AgentError, OSError):
                    raise
                except Exception as exc:
                    result, summary, effect = "blocked", f"Unclassified recovery failure: {exc}", "failure"
        # Classify before report/release so a crash cannot double-count, forget a
        # verdict, or turn an early report into success on a later recovery.
        history = co.history(number)
        classified = recovery | {"result": result, "attempt_effect": effect}
        failures = len(attempts([classified if r["id"] == recovery["id"] else r for r in history],
                                agent.name, co.clock()))
        if persisted and verdict.get("retry_after"):
            delay = max(0, seconds(verdict["retry_after"]) - co.clock())
        else:
            delay = backoff(agent, max(1, failures)) if result == "retry" and effect == "failure" else 0
        co.update(recovery, result=result, summary=summary, attempt_effect=effect,
                  retry_after=iso(co.clock() + delay) if delay else None,
                  unreported=outcome is None or source.get("unreported") or None)
        co.report(recovery, result, f"Recovered {source['run']}: {summary}")
        if result == "success":
            decision.result = "recovered completion (accepted)"
        elif outcome and not persisted:
            decision.result = "recovered outcome rejected (blocked or parked)" if result == "blocked" else "interrupted work released for normal retry"
        else:
            decision.result = "interrupted work released for normal retry" if result == "retry" else "recovered outcome rejected (blocked or parked)"
        next_step = ("; max-attempts exhausted, parked" if failures >= agent.max_attempts and result != "success"
                     else f"; next eligibility {iso(co.clock() + delay)}" if result == "retry" else "")
        decision.reason = summary + next_step
        co.update(recovery, recovery=claim["recovery"] | {"result": decision.result, "summary": decision.reason})
        # Keep all comments, branches, logs and worktrees. Notice minimization is
        # the existing advisory behavior, not artifact deletion.
        co.release(recovery, result, f"Recovered {source['run']}: {summary}", delay,
                   attempt_effect=effect, parking_outcome=outcome, max_attempts=agent.max_attempts)
        decision.applied = True
        return decision
    except (AgentError, OSError) as exc:
        decision.result, decision.reason = "refused or waiting", str(exc)
        return decision
    finally:
        loop.github.lease = None
