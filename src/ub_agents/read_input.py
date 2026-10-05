"""Read-only filtered items, using a launcher's pinned policy inside a run."""

import json
import os
from pathlib import Path
import re

from . import approvals
from .config import bot_logins
from .errors import AgentError
from .github import REPOSITORY


def read_policy(config):
    return {"repository": config.repository, "approvals": config.approvals,
            "trusted-bots": list(config.trusted_bots),
            "triggers": {kind: sorted({label for agent in config.agents
                                      if agent.kind in {kind, "either"} for label in agent.triggers})
                         for kind in ("issue", "pr")}}


def supervised_policy():
    """Never fall back to worktree configuration when supervised context is missing."""
    path = os.environ.get("UB_AGENTS_READ_CONFIG")
    try:
        if not path or not Path(path).is_absolute():
            raise ValueError("missing launcher policy")
        policy = json.loads(Path(path).read_text())
        if (set(policy) != {"repository", "approvals", "trusted-bots", "triggers"}
                or not isinstance(policy["repository"], str)
                or not re.fullmatch(REPOSITORY, policy["repository"])
                or policy["repository"] != os.environ.get("UB_AGENTS_REPOSITORY")
                or policy["approvals"] not in (None, "on", "off")
                or set(policy["triggers"]) != {"issue", "pr"}):
            raise ValueError("invalid launcher policy")
        bot_logins(policy["trusted-bots"])
        for labels in policy["triggers"].values():
            if not isinstance(labels, list) or any(not isinstance(s, str) or not s.strip() for s in labels):
                raise ValueError("invalid launcher triggers")
        return policy
    except (OSError, ValueError, TypeError, KeyError, AgentError) as exc:
        raise AgentError("Supervised read requires the launcher's readable input policy; no input shown") from exc


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
