"""Project cleanup commands, isolated from the candidate's configuration."""

import json
import os
import threading
import uuid

from .errors import AgentError, CleanupError
from .execution import supervise
from .records import iso, timestamp


def diagnostic(config, run, event, **details):
    directory = config.root / ".ub-agent" / "runs" / run
    # Never follow a redirected run-log directory when doing maintenance.
    if directory.resolve() != directory:
        raise CleanupError("Run diagnostics path redirects; preserve artifacts")
    directory.mkdir(parents=True, exist_ok=True)
    events = directory / "events.jsonl"
    if events.resolve() != events:
        raise CleanupError("Run diagnostics file redirects; preserve artifacts")
    with events.open("a") as stream:
        stream.write(json.dumps({"time": iso(timestamp()), "event": event, **details}) + "\n")


def run_hook(config, lease, worktree, outcome=None):
    """Return a confirmed hook failure, or None. Unconfirmed stop raises."""
    if config.cleanup is None:
        return None
    directory = config.root / ".ub-agent" / "runs" / lease["run"] / "cleanup" / uuid.uuid4().hex
    if directory.resolve() != directory:
        raise CleanupError("Hook diagnostics path redirects; preserve artifacts")
    directory.mkdir(parents=True, exist_ok=True)
    context = {
        "repository": config.repository, "run": lease["run"], "agent": lease["agent"],
        "assignment": lease["assignment"],
        "kind": "pr" if lease.get("assignment_sha") else "issue",
        "handoff": outcome.get("handoff") if outcome else None,
        "resume_pr": lease.get("resume_pr"),
        "status": lease.get("result") if lease["state"] == "released" else
                  outcome.get("status") if outcome else None,
        "outcome": outcome.get("outcome") if outcome else None,
        "worktree": str(worktree), "branch": lease.get("branch"),
    }
    path = directory / "context.json"
    path.write_text(json.dumps(context, indent=2))
    env = {key: value for key, value in os.environ.items() if not key.startswith("UB_AGENT_")}
    env.update({"UB_AGENT_CLEANUP_CONTEXT": str(path),
                "UB_AGENT_REPOSITORY": config.repository,
                "UB_AGENT_RUN": lease["run"], "UB_AGENT_AGENT": lease["agent"],
                "UB_AGENT_ASSIGNMENT": str(lease["assignment"]),
                "UB_AGENT_WORKTREE": str(worktree), "UB_AGENT_BRANCH": lease.get("branch") or ""})
    try:
        code = supervise(list(config.cleanup.command), config.root, env, directory,
                         config.cleanup.timeout_seconds, lambda: None, threading.Event())
        failure = f"Cleanup hook exited {code}" if code else None
    except CleanupError:
        raise
    except (AgentError, OSError) as exc:
        failure = str(exc)
    if failure:
        diagnostic(config, lease["run"], "cleanup-hook-failed", error=failure)
    else:
        diagnostic(config, lease["run"], "cleanup-hook-succeeded")
    return failure
