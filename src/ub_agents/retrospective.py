"""Post telemetry only to a supervised agent's launcher-pinned discussion."""

import json
import os
from pathlib import Path
import re

from .errors import AgentError
from .github import REPOSITORY
from .records import positive_int


def retrospective_policy(config, agent, run):
    return {"repository": config.repository, "agent": agent.name, "run": run,
            "retrospectives": agent.retrospectives}


def supervised_policy():
    path = os.environ.get("UB_AGENTS_RETROSPECTIVE_CONFIG")
    try:
        if not os.environ.get("UB_AGENTS_RUN") or not path or not Path(path).is_absolute():
            raise ValueError("missing launcher policy")
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(policy, dict)
                or set(policy) != {"repository", "agent", "run", "retrospectives"}
                or not isinstance(policy["repository"], str)
                or not re.fullmatch(REPOSITORY, policy["repository"])
                or policy["repository"] != os.environ.get("UB_AGENTS_REPOSITORY")
                or policy["run"] != os.environ["UB_AGENTS_RUN"]
                or not isinstance(policy["agent"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]*", policy["agent"])
                or (policy["retrospectives"] is not None and not positive_int(policy["retrospectives"]))):
            raise ValueError("invalid launcher policy")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise AgentError("retrospective requires a supervised ub-agents assignment and the launcher's "
                         "readable retrospective policy; nothing posted") from exc
    if policy["retrospectives"] is None:
        raise AgentError(f"No retrospective board configured for agent {policy['agent']}; nothing posted")
    return policy


def read_body(path):
    try:
        body = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentError(f"Retrospective body file is missing or unreadable: {path}; nothing posted") from exc
    if not body.strip():
        raise AgentError("Retrospective body must not be empty or whitespace-only; nothing posted")
    return body
