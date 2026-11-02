#!/usr/bin/env python3
"""A quick command card, shown by mise when entering an interactive checkout."""
import os
import shutil
import sys


def main():
    on_enter = "--on-enter" in sys.argv[1:]
    stream = sys.stderr if on_enter else sys.stdout
    if on_enter and (not stream.isatty() or os.environ.get("CI")
                     or os.environ.get("UB_AGENTS_WELCOME") == "0"):
        return
    color = stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    violet, dim, reset = ("\033[38;5;141m", "\033[2m", "\033[0m") if color else ("", "", "")
    width = min(70, shutil.get_terminal_size().columns)
    host = os.environ.get("UB_AGENTS_REMOTE_HOST", "")
    example = os.environ.get("UB_AGENTS_REMOTE_LOOPS", "").split()
    loop = example[0] if example else "<loop>"
    lines = ["", f"{violet}  ▸ ub-agents{reset}   {dim}your loop control room{reset}",
             f"{violet}  {'─' * max(1, width - 4)}{reset}",
             f"  {dim}Prepare → implement → review → integrate{reset}", "", "  HERE",
             "    ub-agents launch                  Run this checkout's loop",
             "    ub-agents status                  Inspect the queue",
             "    ub-agents doctor                  Check setup and logins",
             "    mise run ci                       Verify a commit", ""]
    if host:
        lines.extend([f"  REMOTE · {host}",
                      f"    mise remote-loop {loop:<16} Attach · Ctrl-a d detaches",
                      "    mise remote-loop <loop> status    Service state",
                      "    mise remote-loop <loop> logs      Follow service logs",
                      "    mise remote-loop <loop> drain     Finish the run, then stop",
                      "    mise remote-loop <loop> start     Start immediately",
                      "    mise remote-loop <loop> stop      Interrupt and release claim"])
        if example:
            lines.extend(["", f"  {dim}Loops: {' · '.join(example)}{reset}"])
    else:
        lines.extend(["  REMOTE", "    Set UB_AGENTS_REMOTE_HOST in mise.local.toml to connect.",
                      "    Setup: docs/development/checks.md#remote-loop-shortcuts"])
    lines.append("")
    print("\n".join(lines), file=stream)


if __name__ == "__main__":
    main()
