"""Install refreshed control-checkout dependencies before any assignment writes."""

import hashlib
import json
import os
import re
import shlex
import uuid
from contextlib import contextmanager

from .errors import (AgentError, CheckoutRefreshError, CheckoutSetupInterrupted, CleanupError,
                     RetryableExecutionError)
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
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / "setup.json")
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def baseline_record(root):
    directory = None
    try:
        directory = setup_directory(root)
        directory.mkdir(parents=True, exist_ok=True)
        with lock(directory / "setup.lock") as guard:
            if guard is None:
                raise AgentError("another checkout setup is still running; wait for it to finish")
            yield directory, read_record(directory)
    except (AgentError, OSError, ValueError) as exc:
        raise CheckoutRefreshError(f"checkout setup baseline failed: {exc} (state: {directory}). "
                                   "Repair the setup state and launch again.") from exc


def preserve_baseline(root, previous, head):
    """Save the first baseline before a merge can advance HEAD or reload can fail."""
    if previous == head:
        return
    with baseline_record(root) as (directory, record):
        if record is None:
            write_record(directory, {"baseline": previous, "succeeded": False, "pending": None})


def discard_unused_baseline(root):
    """A valid reload without setup consumes an unevaluated refresh baseline."""
    if not (setup_directory(root) / "setup.json").exists():
        return
    with baseline_record(root) as (directory, record):
        if record is not None and not record["succeeded"] and record["pending"] is None:
            (directory / "setup.json").unlink()


def confirm_stopped(directory):
    """A crashed launch must not overlap an install still running in its group."""
    attempts = directory / "attempts"
    if not attempts.exists():
        return
    for attempt in attempts.iterdir():
        stopped = attempt / "stopped"
        if stopped.exists():
            if stopped.read_text() != "confirmed\n":
                raise AgentError(f"repair the checkout setup stop record in {attempt}")
            continue
        pid = attempt / "pid"
        if not pid.exists():
            raise AgentError(f"confirm the checkout setup process has exited and remove {attempt}")
        group = int(pid.read_text())
        if group < 1 or group_members(group):
            raise AgentError(f"confirm checkout setup process group {group} has exited")
        (attempt / "stopped").write_text("confirmed\n")


def run_setup(config, interrupt, output, activity, previous=None):
    root = config.root
    if config.checkout_setup is None:
        return
    directory = None
    trigger = None
    log = None
    try:
        directory = setup_directory(root)
        directory.mkdir(parents=True, exist_ok=True)
        with lock(directory / "setup.lock") as guard:
            if guard is None:
                raise AgentError("another checkout setup is still running; wait for it to finish")
            confirm_stopped(directory)
            head = git(root, "rev-parse", "HEAD")
            record = read_record(directory) or {"baseline": previous or head,
                                                "succeeded": False, "pending": None}
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
            confirmed = False
            try:
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith("UB_AGENTS_")}
                code = supervise(list(setting.command), root, env, attempt,
                                 setting.timeout_seconds, interrupt, pass_fds=(guard.fileno(),))
                confirmed = True
                if code:
                    raise AgentError(f"{command} exited {code}")
            except (KeyboardInterrupt, RetryableExecutionError):
                # These supervisor exits have either never spawned or confirmed
                # termination. A cleanup error masks them and remains uncertain.
                confirmed = True
                raise
            finally:
                if confirmed:
                    (attempt / "stopped").write_text("confirmed\n")
            write_record(directory, {"baseline": head, "succeeded": True, "pending": None})
    except (AgentError, OSError, ValueError, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        detail = f"{command} interrupted" if interrupted and log is not None else " ".join(str(exc).split())
        if log is not None:
            detail = f"checkout setup failed after {trigger} changed: {detail} (log: {log})."
        else:
            detail = f"checkout setup failed: {detail} (state: {directory})."
        if isinstance(exc, CleanupError):
            detail += f" {exc.next_step};"
        message = f"{detail} Fix the install in {root} and launch again."
        activity(message)
        error = CheckoutSetupInterrupted if interrupted else CheckoutRefreshError
        raise error(message) from exc
