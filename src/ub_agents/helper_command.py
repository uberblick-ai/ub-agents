"""Isolated Python helper entry point for the launcher's package copy."""

from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ub_agents.launcher_code import use_copy

    descriptor, module = int(sys.argv[1]), sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    with use_copy(descriptor):
        runpy.run_module(module, run_name="__main__", alter_sys=True)
