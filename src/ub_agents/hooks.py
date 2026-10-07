"""Project cleanup commands, isolated from the candidate's configuration."""

import json
import os
import threading
import uuid

from .errors import AgentError, CleanupError, LostOwnership
from .execution import group_members, supervise
from .records import iso, timestamp
from .run_config import run_directory


def diagnostic(config, run, event, **details):
    directory = run_directory(config.root, run)
    # Never follow a redirected run-log directory when doing maintenance.
    if directory.resolve() != directory:
        raise CleanupError(f"Run diagnostics path {directory} redirects",
                           next_step="restore the run diagnostics path without redirects")
    directory.mkdir(parents=True, exist_ok=True)
    events = directory / "events.jsonl"
    if events.resolve() != events:
        raise CleanupError(f"Run diagnostics file {events} redirects",
                           next_step="restore the run diagnostics file without redirects")
    with events.open("a") as stream:
        stream.write(json.dumps({"time": iso(timestamp()), "event": event, **details}) + "\n")


def confirm_hook_groups_stopped(config, run):
    """A crashed supervisor's hook must not overlap retries or artifact removal."""
    parent = run_directory(config.root, run) / "cleanup"
    try:
        if parent.resolve() != parent:
            raise ValueError("redirected hook diagnostics")
        if not parent.exists():
            return
        for directory in parent.iterdir():
            if directory.resolve() != directory or not directory.is_dir():
                raise ValueError("uncertain hook diagnostics directory")
            stopped, pid = directory / "stopped", directory / "pid"
            if stopped.resolve() != stopped or pid.resolve() != pid:
                raise ValueError("redirected hook process record")
            if stopped.exists():
                if stopped.read_text() != "confirmed\n":
                    raise ValueError("unreadable hook stop record")
                continue
            # The directory is created before spawning. Missing pid means the
            # launcher could have crashed between Popen and recording its group.
            if not pid.exists():
                raise ValueError("hook has no recorded process group or confirmed stop")
            group = int(pid.read_text())
            if group < 1:
                raise ValueError("invalid hook process group")
            if group_members(group):
                raise CleanupError(f"Cleanup hook process group {group} is still present",
                                   next_step=f"confirm process group {group} has exited")
    except (OSError, ValueError) as exc:
        raise CleanupError(f"Cleanup hook process check cannot be confirmed: {exc}",
                           next_step=f"confirm the hook has exited and repair its process records in {parent}") from exc


def run_hook(config, lease, worktree, outcome=None, expires=None):
    """Return a confirmed hook failure, or None. Unconfirmed stop raises."""
    if config.cleanup is None:
        return None
    confirm_hook_groups_stopped(config, lease["run"])
    directory = run_directory(config.root, lease["run"]) / "cleanup" / uuid.uuid4().hex
    if directory.resolve() != directory:
        raise CleanupError(f"Hook diagnostics path {directory} redirects",
                           next_step="restore the hook diagnostics path without redirects")
    directory.mkdir(parents=True, exist_ok=True)
    context = {
        "repository": config.repository, "run": lease["run"], "agent": lease["agent"],
        "assignment": lease["assignment"],
        "kind": "pr" if lease.get("assignment_sha") else "issue",
        "handoff": outcome.get("handoff") if outcome else None,
        "status": lease.get("result") if lease["state"] == "released" else
                  outcome.get("status") if outcome else None,
        "outcome": outcome.get("outcome") if outcome else None,
        "worktree": str(worktree), "branch": lease.get("branch"),
    }
    path = directory / "context.json"
    path.write_text(json.dumps(context, indent=2))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("UB_AGENTS_")}
    env.update({"UB_AGENTS_CLEANUP_CONTEXT": str(path),
                "UB_AGENTS_REPOSITORY": config.repository,
                "UB_AGENTS_RUN": lease["run"], "UB_AGENTS_AGENT": lease["agent"],
                "UB_AGENTS_ASSIGNMENT": str(lease["assignment"]),
                "UB_AGENTS_WORKTREE": str(worktree), "UB_AGENTS_BRANCH": lease.get("branch") or ""})
    confirmed = True
    try:
        code = supervise(list(config.cleanup.command), config.root, env, directory,
                         config.cleanup.timeout_seconds, threading.Event(), expires=expires)
        failure = f"Cleanup hook exited {code}" if code else None
    except CleanupError:
        confirmed = False
        raise
    except LostOwnership:
        # supervise stopped the hook, but ownership loss forbids removal and
        # cannot be downgraded to an ordinary, retryable hook failure.
        raise
    except (AgentError, OSError) as exc:
        failure = str(exc)
    finally:
        if confirmed:
            (directory / "stopped").write_text("confirmed\n")
    if failure:
        diagnostic(config, lease["run"], "cleanup-hook-failed", error=failure)
    else:
        diagnostic(config, lease["run"], "cleanup-hook-succeeded")
    return failure
