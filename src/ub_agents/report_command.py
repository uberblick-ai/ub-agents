"""A shell command and entry point pinned to the running launcher's installation."""

import os
from pathlib import Path
import shlex
import sys


def launcher_report_command():
    # Keep a virtualenv's interpreter symlink: resolving it can lose that environment.
    # -I ignores PYTHONPATH and the agent's cwd; the entry point pins the package too.
    return shlex.join([os.path.abspath(sys.executable), "-I", str(Path(__file__).resolve())])


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ub_agents.cli import main

    raise SystemExit(main())
