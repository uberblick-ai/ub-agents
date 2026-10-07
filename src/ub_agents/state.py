"""Per-user state shared by launchers."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


def user_state_directory():
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/state"
    return base / "ub-agents"


@contextmanager
def lock(path, shared=False, create=True, blocking=False):
    """Kernel-owned locks have no stale PID markers or PID-reuse ambiguity."""
    if not create and not path.exists():
        yield None
        return
    stream = path.open("a+b" if create else "rb")
    try:
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(stream, operation | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            stream.close()
            stream = None
        yield stream
    finally:
        if stream is not None:
            # Close instead of LOCK_UN: an inherited run descriptor must retain
            # the lock if the launcher dies before its child finishes.
            stream.close()
