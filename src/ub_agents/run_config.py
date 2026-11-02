"""Run artifacts and the launcher's pinned context for in-run commands."""

import json
import os
from pathlib import Path
import re

from .config import argv, bot_logins
from .errors import AgentError, CleanupError
from .github import REPOSITORY
from .read_input import read_policy
from .records import outcome_declarations, positive_int, validate_labels, validate_transition


def run_directory(root, run):
    return root / ".ub-agents" / "runs" / run


def run_config(config, agent, lease):
    return read_policy(config) | {"run": lease["run"], "assignment": lease["assignment"],
                                 "lease_id": lease["id"], "agent": agent.name,
                                 "retrospectives": agent.retrospectives,
                                 "outcomes": outcome_declarations(agent), "stop-labels": list(config.stop_labels)}


def supervised_run():  # noqa: C901
    """Fail closed before any input read or write when launcher context is invalid."""
    path = os.environ.get("UB_AGENTS_RUN_CONFIG")
    try:
        if not path or not Path(path).is_absolute():
            raise ValueError("missing launcher context")
        context = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(context, dict)
                or set(context) - {"launchers", "sub-issue-labels"} != {"repository", "run", "assignment", "lease_id", "agent",
                                    "approvals", "trusted-bots", "trusted-pr-authors", "triggers", "retrospectives", "outcomes", "stop-labels"}
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
        validate_labels(context["stop-labels"])
        if not isinstance(context["outcomes"], dict):
            raise ValueError("invalid outcome declarations")
        for name, transition in context["outcomes"].items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid outcome name")
            validate_transition(transition)
        bot_logins(context["trusted-bots"])
        bot_logins(context["trusted-pr-authors"], "trusted-pr-authors")
        if "launchers" in context:
            launchers = argv(context["launchers"], "launchers")
            if (any(login != login.strip() for login in launchers)
                    or len({login.casefold() for login in launchers}) != len(launchers)):
                raise ValueError("invalid launcher accounts")
        for labels in [*context["triggers"].values(), context.get("sub-issue-labels", [])]:
            if not isinstance(labels, list) or any(not isinstance(s, str) or not s.strip() for s in labels):
                raise ValueError("invalid launcher triggers")
        if not set(context.get("sub-issue-labels", ())).issubset(context["triggers"]["issue"]):
            raise ValueError("invalid sub-issue labels")
        return context
    except (OSError, ValueError, TypeError, KeyError, AgentError) as exc:
        raise AgentError("Command requires a supervised ub-agents assignment and the launcher's readable "
                         "run.json (UB_AGENTS_RUN_CONFIG); no input shown or record posted") from exc


def process_group(root, run):
    """The latest supervised group is local machine state, never a public field."""
    directory = run_directory(root.resolve(), run)
    path = directory / "process-group"
    try:
        if Path(run).name != run or run in {".", ".."} or path.resolve() != path:
            raise ValueError("redirected process record")
        try:
            group = int(path.read_text())
        except FileNotFoundError:
            return None
        if group < 1:
            raise ValueError("invalid process group")
        return group
    except (OSError, ValueError) as exc:
        raise CleanupError("Run process record is unreadable", next_step="repair the local run process record") from exc


def record_process_group(root, run, group):
    path = run_directory(root.resolve(), run) / "process-group"
    if path.resolve() != path:
        raise CleanupError("Run process record redirects", next_step="repair the local run process record")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    if temporary.resolve() != temporary:
        raise CleanupError("Run process record redirects", next_step="repair the local run process record")
    temporary.write_text(f"{group}\n")
    temporary.replace(path)
