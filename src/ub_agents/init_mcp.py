"""Read-only MCP preview and optional project registration through Uberblick's CLI."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import tomllib

from .errors import AgentError

CLIENTS = {"claude": ("Claude Code", ".mcp.json"), "codex": ("Codex", ".codex/config.toml")}


def invoke(root, *args):
    try:
        return subprocess.run(["ub", *args], cwd=root, capture_output=True, text=True, timeout=30,
                              check=False, stdin=subprocess.DEVNULL)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        # Client diagnostics can quote config values. Never repeat them in the transcript.
        raise AgentError("ub could not be run") from exc


def decode(content, cli):
    data = json.loads(content) if cli == "claude" else tomllib.loads(content)
    key = "mcpServers" if cli == "claude" else "mcp_servers"
    if not isinstance(data, dict) or not isinstance(data.get(key, {}), dict):
        raise ValueError("invalid MCP config")
    return data, key


def matching(entry, cli):
    allowed = {"command", "args", "env"} | ({"type"} if cli == "claude" else set())
    return (isinstance(entry, dict) and not set(entry).difference(allowed) and
            entry.get("command") == "ub" and entry.get("args") == ["mcp", "serve"] and
            entry.get("env", {}) == {} and entry.get("type", "stdio") == "stdio")


@dataclass
class Registration:
    root: Path
    cli: str
    file: str
    action: str = "not registered"
    reason: str = ""
    before: bytes | None = None
    for_pr: bool = False

    @property
    def command(self):
        return f"ub mcp install {self.cli}"

    @property
    def description(self):
        client = CLIENTS.get(self.cli, (self.cli,))[0]
        return f"Uberblick MCP · {client} · project scope · {self.file}"

    @property
    def preview(self):
        action = {"add": "add entry", "reused": "reuse matching entry", "kept existing": "keep existing entry"}
        detail = action.get(self.action, self.action)
        return f"{self.description} · {detail}" + (f" ({self.reason})" if self.reason else "")

    def read(self):
        path = self.root / self.file
        # ub delegates writes to the client; do not let it follow a config symlink.
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("config path is a symlink")
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None, {}, "mcpServers" if self.cli == "claude" else "mcp_servers"
        data, key = decode(content.decode("utf-8"), self.cli)
        return content, data, key

    def capability(self):
        # Grounded in the v0.4.3 release, not a guessed --scope/--project option:
        # https://github.com/uberblick-ai/homebrew-tap/releases/tag/v0.4.3
        # Project is the default; --print runs nothing and names the target on stderr.
        # A binding above this checkout can redirect ub there, so verify the exact path.
        version = invoke(self.root, "--version")
        match = re.fullmatch(r"(?:ub )?(\d+)\.(\d+)\.(\d+)", version.stdout.strip())
        if version.returncode or not match or tuple(map(int, match.groups())) < (0, 4, 3):
            raise AgentError("requires ub 0.4.3 or newer with project-scope installation")
        result = invoke(self.root, "mcp", "install", self.cli, "--print")
        if result.returncode:
            raise AgentError("ub cannot preview project-scope installation for this client")
        destination = f"--print runs nothing — paste this into {self.root / self.file}"
        if f"ub mcp install: {destination}" not in result.stderr.splitlines():
            raise AgentError("ub did not confirm this checkout's project config path")
        try:
            data, key = decode(result.stdout, self.cli)
        except (ValueError, UnicodeError):
            raise AgentError("ub did not preview a plain ub mcp serve entry") from None
        if data != {key: {"uberblick": data.get(key, {}).get("uberblick")}} or not matching(data[key]["uberblick"], self.cli):
            raise AgentError("ub did not preview a plain ub mcp serve entry")

    def inspect(self):
        try:
            self.before, data, key = self.read()
        except (OSError, ValueError, UnicodeError):
            self.action, self.reason = "kept existing", "config cannot be safely read; left unchanged"
            return self
        entries = data.get(key, {})
        if "uberblick" in entries:
            if matching(entries["uberblick"], self.cli):
                self.action = "reused"
            else:
                self.action, self.reason = "kept existing", "conflicting uberblick entry; left unchanged"
            return self
        try:
            self.capability()
            if self.for_pr:
                ignored = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", "--", self.file],
                                         capture_output=True, text=True, timeout=30, check=False,
                                         stdin=subprocess.DEVNULL)
                if ignored.returncode != 1:
                    raise AgentError("project config is ignored or its Git status could not be checked; left unchanged")
        except AgentError as exc:
            self.reason = str(exc)
            return self
        except (OSError, subprocess.TimeoutExpired):
            self.reason = "project config's Git status could not be checked; left unchanged"
            return self
        self.action = "add"
        return self

    def apply(self):
        """Return only a verified changed config for the setup-only commit."""
        if self.action not in {"add", "reused"}:
            return []
        try:
            before, data, key = self.read()
            if before != self.before:
                self.action, self.reason = "kept existing", "config changed after preview; left unchanged"
                return []
            if self.action == "reused":
                return []
            # Workspace creation happens between preview and registration. Check that ub
            # still targets this checkout before allowing the client's write.
            self.capability()
            result = invoke(self.root, "mcp", "install", self.cli)
            after, updated, _ = self.read()
            entries = updated.get(key, {})
            remaining = {**updated, key: {name: entry for name, entry in entries.items() if name != "uberblick"}}
            original = {**data, key: data.get(key, {})}
            if not matching(entries.get("uberblick"), self.cli) or remaining != original:
                status = f"ub exited {result.returncode}; " if result.returncode else ""
                raise AgentError(status + "client did not write a verified entry preserving the other settings")
            self.action = "registered"
            if result.returncode:
                self.reason = f"ub exited {result.returncode}, but the project entry was verified"
            return [self.file] if after != before else []
        except (AgentError, OSError, ValueError, UnicodeError) as exc:
            self.action = "not registered"
            self.reason = str(exc) if isinstance(exc, AgentError) else "project config could not be verified"
            return []


def preview(root, cli, *, for_pr=False):
    registration = Registration(root.resolve(), cli, CLIENTS.get(cli, ("", "no supported config path"))[1], for_pr=for_pr)
    if cli not in CLIENTS:
        registration.reason = "ub supports project registration for Claude Code and Codex"
        return registration
    return registration.inspect()
