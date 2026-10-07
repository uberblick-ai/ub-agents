"""A shell command and entry point pinned to the launcher's startup code copy."""

import os
from pathlib import Path
import shlex
import sys


def launcher_report_command():
    from .launcher_code import package_directory

    # Keep a virtualenv's interpreter symlink: resolving it can lose that environment.
    # -I ignores PYTHONPATH and the agent's cwd; the entry point pins the package too.
    return shlex.join([os.path.abspath(sys.executable), "-I", str(package_directory() / "report_command.py")])


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ub_agents.launcher_code import use_copy

    with use_copy():
        from ub_agents.cli import main

        raise SystemExit(main())
