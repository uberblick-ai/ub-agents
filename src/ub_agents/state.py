"""Per-user state shared by launchers."""

import os
from pathlib import Path


def user_state_directory():
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/state"
    return base / "ub-agents"
