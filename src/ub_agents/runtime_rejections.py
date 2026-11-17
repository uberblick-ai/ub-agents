"""Recognize startup argv rejections and hold command-specific launcher blocks."""

import re
import threading

from .execution import command_template
from .usage.usage_output import JsonLines


DIAGNOSTICS = {
    ("claude", 1): (
        r"error: unknown option '(?P<setting>[^'\r\n]+)'(?:\s|$)",
        r"error: option '(?P<option>[^'\r\n]+)' argument '(?P<setting>[^'\r\n]+)' is invalid\.(?:\s|$)",
    ),
    ("codex", 2): (
        r"error: unexpected argument '(?P<setting>[^'\r\n]+)' found(?:\s|$)",
        r"error: invalid value '(?P<setting>[^'\r\n]+)' for '(?P<option>[^'\r\n]+)'(?:\s|$)",
    ),
    ("codex", 1): (
        r"Error loading config\.toml: unknown configuration field `(?P<setting>[^`\r\n]+)` in -c/--config override(?:\s|$)",
    ),
}


def rejected_setting(cli, code, run_dir):
    """Match only stderr's beginning, and never classify a run emitting JSON."""
    patterns = DIAGNOSTICS.get((cli, code), ())
    if not patterns:
        return None
    try:
        with (run_dir / "stderr.log").open("rb") as stream:
            diagnostic = stream.read(4096).decode("utf-8", errors="replace")
        # Missing captures cannot establish that stdout was free of JSON.
        with (run_dir / "stdout.log").open("rb"):
            pass
    except OSError:
        return None
    if next(JsonLines(run_dir / "stdout.log").read(final=True), None) is not None:
        return None
    for pattern in patterns:
        match = re.match(pattern, diagnostic)
        if match:
            setting = match["setting"]
            option = match.groupdict().get("option")
            return f"{option}: {setting}" if option else setting
    return None


def block_reason(block):
    return (f"{block['agent']} {block['runtime']}: rejected setting {block['setting']}; "
            f"{block['remedy']}")


class RuntimeBlocks:
    def __init__(self, on_change=None):
        self.blocks = {}
        self.on_change = on_change
        self.lock = threading.RLock()

    def changed(self):
        if self.on_change is not None:
            self.on_change([dict(value['info']) for value in self.blocks.values()])

    def reset(self):
        with self.lock:
            self.blocks.clear()
            self.changed()

    def reject(self, agent, runtime, setting, version):
        info = {"agent": agent.name, "runtime": runtime.name, "setting": setting,
                "remedy": ("Change this alternative's runtime-args, model or effort, update the CLI version "
                           "or launcher flags, or restart this launcher")}
        with self.lock:
            self.blocks[agent.name, runtime.name] = {"command": tuple(command_template(agent, runtime)),
                                                    "version": version, "info": info}
            self.changed()
        return block_reason(info)

    def blocked(self, agent, runtime):
        with self.lock:
            key = agent.name, runtime.name
            value = self.blocks.get(key)
            if value is not None and value['command'] != tuple(command_template(agent, runtime)):
                del self.blocks[key]
                self.changed()
                return None
            return dict(value['info']) if value is not None else None

    def reconcile(self, config, version):
        """Recheck versions once per boundary, only for CLIs with held blocks."""
        with self.lock:
            current = {(agent.name, runtime.name): (agent, runtime)
                       for agent in config.agents if not agent.command for runtime in agent.runtimes}
            for key in list(self.blocks):
                if key not in current:
                    del self.blocks[key]
                    self.changed()
                else:
                    self.blocked(*current[key])
            clis = {current[key][1].cli for key in self.blocks}
        versions = {cli: version(cli) for cli in clis}
        with self.lock:
            for key, value in list(self.blocks.items()):
                after = versions[current[key][1].cli]
                if value['version'] is not None and after is not None and value['version'] != after:
                    del self.blocks[key]
                    self.changed()
