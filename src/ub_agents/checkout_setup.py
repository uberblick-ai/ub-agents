"""Install refreshed control-checkout dependencies before any assignment writes."""

import hashlib
import json
import os
import re
import shlex
import uuid

from .errors import AgentError, CheckoutRefreshError, CleanupError
from .execution import git, group_members, supervise
from .state import lock, user_state_directory


def setup_directory(root):
    identity = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    directory = user_state_directory() / "checkouts" / identity
    if directory.resolve().is_relative_to(root.resolve()):
        raise AgentError("checkout setup state must be outside the control checkout")
    return directory


def read_record(directory):
    path = directory / "setup.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or set(record) != {"baseline", "succeeded", "pending"}
            or not isinstance(record["baseline"], str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", record["baseline"])
            or not isinstance(record["succeeded"], bool)
            or (record["pending"] is not None and not isinstance(record["pending"], str))):
        raise ValueError("invalid checkout setup record")
    return record


def write_record(directory, record):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
        temporary.replace(directory / "setup.json")
    finally:
        temporary.unlink(missing_ok=True)


def remember_refresh(root):
    """Keep the pre-fast-forward HEAD even if config reload or setup fails."""
    directory = setup_directory(root)
    try:
        if read_record(directory) is None:
            write_record(directory, {"baseline": git(root, "rev-parse", "HEAD"),
                                     "succeeded": False, "pending": None})
    except (OSError, ValueError) as exc:
        raise AgentError(f"cannot record checkout setup baseline in {directory}: {exc}") from exc


def confirm_stopped(directory):
    """A crashed launch must not overlap an install still running in its group."""
    attempts = directory / "attempts"
    if not attempts.exists():
        return
    for attempt in attempts.iterdir():
        if (attempt / "stopped").exists():
            continue
        pid = attempt / "pid"
        if not pid.exists():
            raise AgentError(f"confirm the checkout setup process has exited and remove {attempt}")
        group = int(pid.read_text())
        if group < 1 or group_members(group):
            raise AgentError(f"confirm checkout setup process group {group} has exited")
        (attempt / "stopped").write_text("confirmed\n")


def run_setup(config, interrupt, output, activity):
    root = config.root
    directory = setup_directory(root)
    path = directory / "setup.json"
    if config.checkout_setup is None:
        path.unlink(missing_ok=True)
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with lock(directory / "setup.lock") as guard:
            if guard is None:
                raise AgentError("another checkout setup is still running; wait for it to finish")
            confirm_stopped(directory)
            head = git(root, "rev-parse", "HEAD")
            record = read_record(directory) or {"baseline": head, "succeeded": False, "pending": None}
            setting = config.checkout_setup
            trigger = record["pending"]
            if trigger is None:
                trigger = next((name for name in setting.when_changed
                                if git(root, "diff", "--name-only", record["baseline"], head,
                                       "--", f":(literal){name}")), None)
            if trigger is None:
                if not record["succeeded"]:
                    write_record(directory, record | {"baseline": head})
                return
            # Persist pending before spawn: even a later reversion must retry a
            # failed/partially completed install, rather than trust its packages.
            write_record(directory, record | {"pending": trigger})
            attempt = directory / "attempts" / uuid.uuid4().hex
            attempt.mkdir(parents=True)
            log = attempt / "process.log"
            log.touch()
            command = shlex.join(setting.command)
            message = f"checkout setup running after {trigger} changed: {command} (log: {log})"
            output(message)
            activity(f"checkout setup running: {trigger} changed")
            confirmed = True
            try:
                code = supervise(list(setting.command), root, os.environ.copy(), attempt,
                                 setting.timeout_seconds, interrupt, pass_fds=(guard.fileno(),))
                if code:
                    raise AgentError(f"{command} exited {code}")
            except KeyboardInterrupt as exc:
                raise AgentError(f"{command} interrupted") from exc
            except CleanupError:
                confirmed = False
                raise
            finally:
                if confirmed:
                    (attempt / "stopped").write_text("confirmed\n")
            write_record(directory, {"baseline": head, "succeeded": True, "pending": None})
    except (AgentError, OSError, ValueError) as exc:
        detail = " ".join(str(exc).split())
        if "log" in locals():
            detail = f"checkout setup failed after {trigger} changed: {detail} (log: {log})."
        else:
            detail = f"checkout setup failed: {detail} (state: {directory})."
        if isinstance(exc, CleanupError):
            detail += f" {exc.next_step};"
        message = f"{detail} Fix the install in {root} and launch again."
        activity(message)
        raise CheckoutRefreshError(message) from exc
