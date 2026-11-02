"""MCP configuration as a new agent worktree sees it, without checkout trust."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tomllib

from .config import instruction_text
from .errors import AgentError
from .mcp_probe import probe_stdio


TOOL_NAME = re.compile(r"mcp__(\w[\w.-]*?)__", re.ASCII)
CODEX_ENV = {"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}


def merge(left, right):
    result = deepcopy(left)
    for key, value in right.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def mapping(value):
    return value if isinstance(value, dict) else {}


def strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def flags(arguments, *names):
    """Read repeated option values, including --option=value forms."""
    values = []
    for index, arg in enumerate(arguments):
        if arg in names and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif any(arg.startswith(name + "=") for name in names):
            values.append(arg.split("=", 1)[1])
    return values


def codex_overrides(arguments):
    result = {}
    for value in flags(arguments, "-c", "--config"):
        key, sep, raw = value.partition("=")
        if not sep:
            continue
        try:
            entry = tomllib.loads(value)
        except tomllib.TOMLDecodeError:
            # Codex accepts unquoted strings in CLI overrides too.
            try:
                entry = tomllib.loads(key + "=" + json.dumps(raw))
            except tomllib.TOMLDecodeError:
                continue
        result = merge(result, entry)
    return result


def expand(value):
    """Claude expands environment references before spawning the server."""
    def replace(match):
        if match[1] in os.environ:
            return os.environ[match[1]]
        if match[2] is not None:
            return match[2]
        raise ValueError("unresolved environment reference")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*)(?::-([^}]*))?\}", replace, value)


class MCPChecks:
    def __init__(self, config, *, probe=None):
        self.config = config
        self.probe = probe or probe_stdio
        self.cache = {}
        self.errors = set()
        self.project_claude = self.servers(self.read(config.root / ".mcp.json", runtime_reads=False), "mcpServers")
        self.project_codex = self.servers(self.read(config.root / ".codex/config.toml", runtime_reads=False), "mcp_servers")

    def read(self, path, *, runtime_reads=True):
        try:
            text = path.read_text(encoding="utf-8")
            value = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("expected object")
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            # No parser exceptions: they may quote commands or credentials.
            # Discovery also inspects files the runtime will never load. Their
            # errors must not suppress probes of valid, effective definitions.
            if runtime_reads:
                self.errors.add(path)
            return {}

    @staticmethod
    def servers(value, key):
        return mapping(value.get(key))

    def expected(self, agent):
        names = set(self.project_claude) | set(self.project_codex)
        text = " ".join(arg for runtime in agent.runtimes for arg in agent.runtime_args_for(runtime))
        for path in (agent.instructions, self.config.shared_instructions):
            try:
                text += "\n" + instruction_text(self.config.root, path, "MCP instructions")
            except AgentError:
                pass  # The ordinary instruction check already reports this.
        return names | set(TOOL_NAME.findall(text))

    def codex(self, arguments):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        value = self.read(home / "config.toml")
        overrides = codex_overrides(arguments)
        # A profile may be selected either in the user file or on the command line.
        profiles = flags(arguments, "-p", "--profile")
        profile = profiles[-1] if profiles else overrides.get("profile", value.get("profile"))
        if isinstance(profile, str):
            value = merge(value, mapping(mapping(value.get("profiles")).get(profile)))
        loaded = self.servers(merge(value, overrides), "mcp_servers")
        unread = dict(self.project_codex)
        for parent in self.config.root.parents:
            path = parent / ".codex/config.toml"
            if path != home / "config.toml":
                unread.update(self.servers(self.read(path, runtime_reads=False), "mcp_servers"))
        return loaded, unread, {}, {}

    def claude(self, arguments):
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        user_path = home / ".claude.json" if "CLAUDE_CONFIG_DIR" in os.environ else Path.home() / ".claude.json"
        sources = flags(arguments, "--setting-sources")
        sources = set(sources[-1].split(",")) if sources else {"user", "project", "local"}
        strict = "--strict-mcp-config" in arguments
        user = self.read(user_path, runtime_reads="user" in sources and not strict)
        settings = []
        if "user" in sources:
            settings.append(self.read(home / "settings.json"))
        if "project" in sources:
            settings.append(self.read(self.config.root / ".claude/settings.json"))
        for value in flags(arguments, "--settings"):
            settings.append(self.json_argument(value))
        # Arrays combine; explicit denial always wins over approval.
        approvals = {name for value in settings for name in strings(value.get("enabledMcpjsonServers"))}
        denied = {name for value in settings for name in strings(value.get("disabledMcpjsonServers"))}
        all_approved = any(value.get("enableAllProjectMcpServers") is True for value in settings)
        loaded = dict(self.servers(user, "mcpServers")) if "user" in sources and not strict else {}
        pending = {}
        unread = dict(self.project_claude) if strict or "project" not in sources else {}
        if not strict and "project" in sources:
            project = self.servers(self.read(self.config.root / ".mcp.json"), "mcpServers")
            for name, entry in project.items():
                # Project entries take precedence over user entries even while pending.
                loaded.pop(name, None)
                if name not in denied and (all_approved or name in approvals):
                    loaded[name] = entry
                else:
                    pending[name] = entry
        for value in flags(arguments, "--mcp-config"):
            for name, entry in self.servers(self.json_argument(value), "mcpServers").items():
                loaded[name] = entry
                pending.pop(name, None)
        for parent in self.config.root.parents:
            unread.update(self.servers(self.read(parent / ".mcp.json", runtime_reads=False), "mcpServers"))
        # These entries and approval choices are keyed to the control checkout,
        # and therefore do not apply at a new worktree path.
        local = mapping(mapping(user.get("projects")).get(str(self.config.root)))
        unread.update(self.servers(local, "mcpServers"))
        return loaded, unread, pending, {name: "pending approval" for name in denied if name in pending}

    def json_argument(self, value):
        if value.lstrip().startswith("{"):
            try:
                return mapping(json.loads(value))
            except ValueError:
                self.errors.add(Path("runtime-args"))
                return {}
        return self.read(self.config.root / value)

    def run(self, doctor, *, offline=False):
        for agent in self.config.agents:
            if agent.command:
                continue
            expected = self.expected(agent)
            for runtime in agent.runtimes:
                self.check_runtime(doctor, agent, runtime, expected, offline=offline)

    def check_runtime(self, doctor, agent, runtime, expected, *, offline):
        arguments = agent.runtime_args_for(runtime)
        self.errors = set()
        if offline:
            loaded, unread, pending, denied = {}, {}, {}, {}
            explicit = (self.servers(codex_overrides(arguments), "mcp_servers")
                        if runtime.cli == "codex" else {})
        else:
            loaded, unread, pending, denied = getattr(self, runtime.cli)(arguments)
            explicit = self.servers(codex_overrides(arguments), "mcp_servers")
        names = expected | set(explicit) if runtime.cli == "codex" else set(expected)
        # Explicit Claude --mcp-config entries are relevant too; unrelated
        # user-wide servers do not expand the project's checks.
        if runtime.cli == "claude":
            for value in flags(arguments, "--mcp-config"):
                names.update(self.servers(self.json_argument(value), "mcpServers"))
        if self.errors:
            doctor.add("runtime-mcp-config", "skip" if offline else "warn",
                       "offline: MCP probes are not run" if offline else "MCP configuration could not be read",
                       None if offline else "Fix the MCP JSON/TOML configuration for this runtime",
                       required=False, agent=agent, runtime=runtime)
        for name in sorted(names):
            kwargs = dict(required=False, agent=agent, runtime=runtime, server=name)
            if offline:
                doctor.add("runtime-mcp", "skip", f"{name}: offline: MCP servers are not checked", **kwargs)
                continue
            state, entry = self.server_state(name, loaded, unread, pending)
            self.check_server(doctor, runtime, name, state, entry, denied, kwargs)

    def server_state(self, name, loaded, unread, pending):
        state = ("loaded" if name in loaded else
                 "pending approval" if name in pending else "unread" if name in unread else "missing")
        if self.errors:
            state = "unread"
        entry = mapping(loaded.get(name))
        if state == "loaded" and entry.get("enabled") is False:
            state = "unread"
        if state == "loaded" and not (entry.get("command") or entry.get("url")):
            state = "missing"
        return state, entry

    @staticmethod
    def server_remedy(cli, name, state, entry, denied):
        remedy = (f"Define {name} in runtime-args with -c mcp_servers.{name}.command and other entries"
                  if cli == "codex" else
                  f"Add {name} to enabledMcpjsonServers in the committed .claude/settings.json")
        if cli == "claude" and state == "missing":
            remedy = "Run ub mcp install claude, then " + remedy[0].lower() + remedy[1:]
        elif cli == "claude" and state == "unread":
            remedy = f"Define {name} in the project's .mcp.json and add it to enabledMcpjsonServers"
        if name in denied:
            remedy = f"Remove {name} from disabledMcpjsonServers, then " + remedy[0].lower() + remedy[1:]
        if entry.get("enabled") is False:
            remedy += f" and set mcp_servers.{name}.enabled=true"
        return remedy

    def check_server(self, doctor, runtime, name, state, entry, denied, kwargs):
        if state != "loaded":
            remedy = self.server_remedy(runtime.cli, name, state, entry, denied)
            doctor.add("runtime-mcp", "warn", f"{name}: {state}", remedy, **kwargs)
            return
        if entry.get("url") or entry.get("type", "stdio") != "stdio":
            doctor.add("runtime-mcp", "skip", f"{name}: loaded; non-stdio transport is not probed", **kwargs)
            return
        if entry.get("experimental_environment") == "remote":
            doctor.add("runtime-mcp", "skip", f"{name}: loaded; remote stdio execution is not probed", **kwargs)
            return
        count, error = self.start(runtime.cli, entry)
        doctor.add("runtime-mcp", "warn" if error else "ok",
                   f"{name}: loaded; {error}" if error else f"{name}: loaded; {count} tools",
                   f"Fix MCP server {name}: {error}" if error else None, **kwargs)

    def start(self, cli, entry):
        try:
            command, args, extra = entry.get("command"), entry.get("args", []), entry.get("env", {})
            if (not isinstance(command, str) or not command or not isinstance(args, list)
                    or any(not isinstance(arg, str) for arg in args) or not isinstance(extra, dict)
                    or any(not isinstance(key, str) or not isinstance(value, str) for key, value in extra.items())):
                return None, "handshake failed (invalid stdio configuration)"
            cwd = entry.get("cwd", str(self.config.root))
            if not isinstance(cwd, str):
                return None, "handshake failed (invalid working directory)"
            cwd = str(self.config.root / cwd)
            if cli == "claude":
                command, args = expand(command), [expand(arg) for arg in args]
                env = dict(os.environ) | {key: expand(value) for key, value in extra.items()}
                env["CLAUDE_PROJECT_DIR"] = str(self.config.root)
            else:
                variables = entry.get("env_vars", [])
                if not isinstance(variables, list) or any(not isinstance(key, str) for key in variables):
                    return None, "handshake failed (invalid environment configuration)"
                env = {key: value for key, value in os.environ.items() if key in CODEX_ENV or key in variables} | extra
            key = (command, tuple(args), cwd, tuple(sorted(env.items())))
            if key not in self.cache:
                self.cache[key] = self.probe([command, *args], cwd=cwd, env=env)
            return self.cache[key]
        except (OSError, ValueError, AgentError):
            return None, "handshake failed"
