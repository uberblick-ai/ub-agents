"""One launcher per resolved control checkout; owner records alone confer nothing."""

from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
import socket
from typing import BinaryIO

from .errors import AgentError
from .records import iso, timestamp


_streams: set[BinaryIO] = set()


def _close_in_child():
    # CLOEXEC and subprocess close_fds cover helpers and agents. Also release
    # copies in a fork that does not exec, without unlocking the parent's copy.
    for stream in _streams:
        stream.close()
    _streams.clear()


os.register_at_fork(after_in_child=_close_in_child)


@contextmanager
def _lock(path, *, create=True, blocking=False):
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    with os.fdopen(descriptor, "r+b" if create else "rb") as stream:
        _streams.add(stream)
        try:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield None
            else:
                yield stream
        finally:
            _streams.discard(stream)


def launcher_summary(owner):
    if owner is None:
        return "No launcher running on this checkout"
    summary = (f"Launcher running on this checkout: pid {owner['pid']} on {owner['host']}, "
               f"started {owner['started_at']}")
    if owner.get("configuration_pause"):
        summary += "\n" + configuration_pause_summary(owner["configuration_pause"])
    return summary


def configuration_pause_summary(pause):
    return (f"Claims paused at {pause['commit']}: {pause['error']}; "
            f"{pause['next_step']}")


def publish_configuration_pause(root, pause):
    """Update only this process's live owner record under the metadata guard."""
    directory = root.resolve() / ".ub-agents"
    with _lock(directory / "launcher.guard", create=False, blocking=True) as guard:
        if guard is None:
            return  # Embedded loops need not hold a launcher lock.
        owner = _owner(directory)
        if owner["pid"] != os.getpid():
            return
        if pause is None:
            owner.pop("configuration_pause", None)
        else:
            owner["configuration_pause"] = pause
        descriptor = os.open(directory / "launcher.lock", os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(owner))
            stream.truncate()


def _owner(directory):
    # Called only after the kernel refused the lifetime lock, with the metadata
    # guard held. A new holder cannot replace these details during this read.
    return json.loads((directory / "launcher.lock").read_text(encoding="utf-8"))


@contextmanager
def launcher_lock(root):
    directory = root.resolve() / ".ub-agents"
    if directory.is_symlink():
        raise AgentError(".ub-agents/ must be a real directory for the launcher lock")
    directory.mkdir(mode=0o700, exist_ok=True)
    # The short guard makes acquisition and owner publication one transition.
    # Never unlink either file: waiters must always use the same inode.
    with ExitStack() as lifetime:
        with _lock(directory / "launcher.guard", blocking=True):
            stream = lifetime.enter_context(_lock(directory / "launcher.lock"))
            if stream is None:
                owner = _owner(directory)
                raise AgentError(f"{launcher_summary(owner)}. Stop that launcher first, "
                                 "or use ub-agents retry to hand it an item.")
            owner = {"pid": os.getpid(), "host": socket.gethostname(), "started_at": iso(timestamp())}
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(owner).encode("utf-8"))
            stream.flush()
        yield owner


def running_launcher(root):
    directory = root.resolve() / ".ub-agents"
    if directory.is_symlink() or not directory.is_dir():
        return None  # Doctor's local-state check diagnoses redirected state.
    # Status and doctor need not create state to report an idle checkout.
    with _lock(directory / "launcher.guard", create=False, blocking=True) as guard:
        if guard is None:
            return None
        with _lock(directory / "launcher.lock", create=False) as stream:
            if stream is not None or not (directory / "launcher.lock").exists():
                return None
            return _owner(directory)
