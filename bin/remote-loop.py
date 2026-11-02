#!/usr/bin/env python3
"""Developer shortcut for a remote systemd/tmux loop; not an installed command."""
import argparse
import os
import re
import shlex
import sys


ACTIONS = ("attach", "status", "logs", "start", "stop", "drain")


def ssh_command(loop, action, host):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", loop):
        raise ValueError("loop names use lowercase letters, numbers and hyphens")
    if not host:
        raise ValueError("set UB_AGENTS_REMOTE_HOST in mise.local.toml first; see docs/development.md")
    if host.startswith("-") or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError("UB_AGENTS_REMOTE_HOST must be an SSH destination such as user@host")
    unit = f"ub-agents@{loop}.service"
    commands = {
        "attach": ["ub-agents-tmux", "attach", loop],
        "drain": ["ub-agents-tmux", "drain", loop],
        "status": ["systemctl", "--user", "--no-pager", "--full", "status", unit],
        "logs": ["journalctl", "--user", "--no-pager", "-u", unit, "-n", "80", "-f"],
        "start": ["systemctl", "--user", "start", unit],
        "stop": ["systemctl", "--user", "stop", unit],
    }
    return ["ssh", *(["-t"] if action == "attach" else []), "--", host, shlex.join(commands[action])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("loop")
    parser.add_argument("action", nargs="?", default="attach", choices=ACTIONS)
    args = parser.parse_args()
    try:
        command = ssh_command(args.loop, args.action, os.environ.get("UB_AGENTS_REMOTE_HOST", ""))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"remote-loop: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
