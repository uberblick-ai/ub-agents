"""Operator-attested, local recovery of a stopped launcher's reported outcome."""

import socket

from .coordination import Plan
from .errors import AgentError, CleanupError
from .execution import group_members
from .loop import Loop
from .records import latest_leases, live_leases, records


def recover_run(config, github, actor, number, agent_name, reason, output=print):
    agent = next((a for a in config.agents if a.name == agent_name), None)
    if agent is None:
        raise AgentError("Unknown configured agent")
    if number < 1 or not reason.strip():
        raise AgentError("recover requires a positive item number and a reason")
    loop = Loop(config, github, actor, output=output)
    observed = None

    def refuse(check):
        expiry = observed["expires"] if observed else "unknown (no lease recorded)"
        raise AgentError(f"#{number} {agent_name}: refused — {check}; lease expires {expiry}")

    def check(_history=None):
        nonlocal observed
        # Include other comment authors so a different actor's latest lease is
        # refused explicitly rather than mistaking an older owned lease for it.
        history = records(loop.github.comments(number))
        observed = latest_leases(history).get((number, agent_name))
        if observed is None:
            refuse("no latest lease")
        if observed["actor"].casefold() != actor.casefold():
            refuse("latest lease belongs to another actor")
        if observed.get("host") != socket.gethostname():
            refuse("latest lease belongs to another host or has no recorded host")
        if observed["state"] not in {"claiming", "running"}:
            refuse("latest lease is already finished")
        if observed.get("cleanup") == "unconfirmed":
            refuse("run cleanup is unconfirmed")
        group = observed.get("process_group")
        if type(group) is not int or group < 1:
            refuse("no recorded process group")
        try:
            members = group_members(group)
        except CleanupError as exc:
            refuse(f"process group check cannot be confirmed: {exc}")
        if members:
            refuse("process group has live members")
        if observed.get("result") and observed.get("attempt_effect") in {"failure", "unchanged"}:
            refuse("supervisor verdict already superseded the report")
        try:
            outcome = loop.coordinator.pending_completion(history, agent_name, loop.coordinator.clock(), allow_live=True)
        except AgentError as exc:
            refuse(str(exc))
        if outcome is None:
            refuse("no reported outcome within the lease validity window")
        if any(r["id"] != observed["id"] for r in live_leases(history, loop.coordinator.clock())):
            refuse("another live lease owns the item")
        return outcome

    check()
    item = loop.github.item(number)
    plan = Plan(item, agent, None, "recover", reason, observed["attempt"])
    if not loop.recover(plan, recovery_check=check, recovery_reason=reason):
        refuse("lease changed or another claimant won the recovery election")
