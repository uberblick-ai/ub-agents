"""Read-only filtered items, using a launcher's pinned policy inside a run."""

from . import approvals
from .errors import AgentError


def read_policy(config):
    return {"repository": config.repository, "approvals": config.approvals,
            "trusted-bots": list(config.trusted_bots),
            "triggers": {kind: sorted({label for agent in config.agents
                                      if agent.kind in {kind, "either"} for label in agent.triggers})
                         for kind in ("issue", "pr")}}


def read_item(github, number, policy):
    effective, _ = approvals.resolve_policy(policy["approvals"], github.visibility)
    try:
        item = github.item(number)
        if item.number != number or item.kind not in {"issue", "pr"}:
            raise ValueError("not an issue or PR")
        filtered = approvals.filter_input(github, item, effective, policy["triggers"][item.kind],
                                          trusted_bots=policy["trusted-bots"], read_only=True)
        if not filtered.allowed:
            raise AgentError(filtered.reason)
        return {"number": item.number, "kind": item.kind, "state": item.state} | filtered.snapshot
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AgentError("Configured repository item is unreadable; no input shown") from exc
