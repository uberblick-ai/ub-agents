"""Run artifacts and the launcher's pinned context for in-run commands."""

import json
import os
from pathlib import Path
import re

from .config import bot_logins
from .errors import AgentError
from .github import REPOSITORY
from .read_input import read_policy
from .records import positive_int


def run_directory(root, run):
    return root / ".ub-agents" / "runs" / run


def run_config(config, agent, lease):
    return read_policy(config) | {"run": lease["run"], "assignment": lease["assignment"],
                                 "lease_id": lease["id"], "agent": agent.name,
                                 "retrospectives": agent.retrospectives}


def supervised_run():
    """Fail closed before any input read or write when launcher context is invalid."""
    path = os.environ.get("UB_AGENTS_RUN_CONFIG")
    try:
        if not path or not Path(path).is_absolute():
            raise ValueError("missing launcher context")
        context = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(context, dict)
                or set(context) != {"repository", "run", "assignment", "lease_id", "agent",
                                    "approvals", "trusted-bots", "triggers", "retrospectives"}
                or not isinstance(context["repository"], str)
                or not re.fullmatch(REPOSITORY, context["repository"])
                or context["repository"] != os.environ.get("UB_AGENTS_REPOSITORY")
                or not isinstance(context["run"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", context["run"])
                or context["run"] != os.environ.get("UB_AGENTS_RUN")
                or not positive_int(context["assignment"])
                or not positive_int(context["lease_id"])
                or not isinstance(context["agent"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]*", context["agent"])
                or context["approvals"] not in (None, "on", "off")
                or (context["retrospectives"] is not None and not positive_int(context["retrospectives"]))
                or not isinstance(context["triggers"], dict)
                or set(context["triggers"]) != {"issue", "pr"}):
            raise ValueError("invalid launcher context")
        bot_logins(context["trusted-bots"])
        for labels in context["triggers"].values():
            if not isinstance(labels, list) or any(not isinstance(s, str) or not s.strip() for s in labels):
                raise ValueError("invalid launcher triggers")
        return context
    except (OSError, ValueError, TypeError, KeyError, AgentError) as exc:
        raise AgentError("Command requires a supervised ub-agents assignment and the launcher's readable "
                         "run.json (UB_AGENTS_RUN_CONFIG); no input shown or record posted") from exc
