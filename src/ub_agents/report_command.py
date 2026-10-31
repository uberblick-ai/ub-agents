"""A shell command and entry point pinned to the launcher's startup code copy."""

from pathlib import Path
import shlex
import sys


def launcher_report_command():
    from .launcher_code import interpreter, package_directory

    # Homebrew helpers use the saved dependencies and base interpreter.
    # -I ignores PYTHONPATH and the agent's cwd; the entry point pins the package too.
    return shlex.join([interpreter(), "-I", str(package_directory() / "report_command.py")])


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ub_agents.launcher_code import use_copy

    with use_copy():
        from ub_agents.cli import main

        raise SystemExit(main())
