"""Read-only prerequisite diagnostics. Probe output stays private."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .config import load_config
from .errors import AgentError
from .execution import parse_process_table, repository_checks
from .github import REPOSITORY, GitHub
from .labels import configured_labels
from .records import iso


PERMISSIONS_URL = "https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#runtime-permissions"


AUTH_PROBES = {"codex": ("codex", "login", "status"),
               "claude": ("claude", "auth", "status")}


# Configuration errors can quote YAML. Keep diagnostics on one line and redact
# recognizable credential forms.
def public(value):
    value = re.sub(r"(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+", "[redacted]", str(value))
    return " ".join(value.split())


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    required: bool
    agent: str | None
    runtime: str | None
    message: str
    remedy: str | None


class Doctor:
    def __init__(self, runner=None, which=None, github=None, access=None):
        self.runner = runner or subprocess.run
        self.which = which or shutil.which
        self.github = github
        self.access = access or os.access
        self.checks = []

    def add(self, id, status, message, remedy=None, *, required=True, agent=None, runtime=None):
        # Scope repeated checks with stable configuration identities.
        scope = [id] + ([agent.name] if agent else []) + ([runtime.name] if runtime else [])
        self.checks.append(Check(":".join(scope), status, required,
                                 agent.name if agent else None, runtime.name if runtime else None,
                                 public(message), public(remedy) if remedy else None))

    def probe(self, command, cwd=None):
        try:
            result = self.runner(list(command), cwd=cwd, capture_output=True, text=True,
                                 timeout=20, check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise AgentError("probe timed out (20s)") from None
        except (OSError, UnicodeError):
            raise AgentError("probe could not run") from None
        if result.returncode:
            raise AgentError(f"probe failed (exit {result.returncode})")
        return result.stdout

    def tool(self, name, remedy):
        if self.which(name):
            self.add(name, "ok", f"{name} is on PATH")
            return True
        self.add(name, "fail", f"{name} is not on PATH", remedy)
        return False

    def run(self, path):
        self.add("python", "ok" if sys.version_info >= (3, 11) else "fail",
                 f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                 "Install Python 3.11 or newer" if sys.version_info < (3, 11) else None)
        if os.name != "posix":
            self.add("platform", "fail", "POSIX process supervision is required", "Run on macOS or Linux")
        elif sys.platform not in {"darwin", "linux"}:
            self.add("platform", "warn", f"Untested POSIX platform: {sys.platform}",
                     "Use macOS or Linux for supported supervision", required=False)
        else:
            self.add("platform", "ok", sys.platform)
        git_ready = self.tool("git", "Install Git and put git on PATH")
        gh_ready = self.tool("gh", "Install GitHub CLI from https://cli.github.com and put gh on PATH")
        config = None
        try:
            config = load_config(path)
            self.add("config", "ok", f"{config.repository}, {len(config.agents)} agents")
        except (AgentError, OSError, UnicodeError) as exc:
            missing = not Path(path).exists()
            self.add("config", "fail", str(exc),
                     "Run ub-agent init to create the configuration" if missing else
                     "Fix the configuration error in the selected YAML file")
        if config:
            for agent in config.agents:
                if agent.instructions is None:
                    self.add("instructions", "skip", "not configured", agent=agent)
                    continue
                try:
                    agent.instructions.read_text()
                    self.add("instructions", "ok", "instruction file is readable", agent=agent)
                except (OSError, UnicodeError):
                    self.add("instructions", "fail", "instruction file is not readable",
                             f"Restore a readable instruction file at {agent.instructions}", agent=agent)
        else:
            self.add("instructions", "skip", "configuration unavailable")
        if config and git_ready:
            def read_git(root, *args):
                return self.probe(["git", "-C", str(root), *args], root).strip()
            remedies = {"repository-root": "Place the configuration at the consuming Git repository root",
                        "repository-remote": f"Set origin to https://github.com/{config.repository}.git"}
            for id, error in repository_checks(config, read_git):
                self.add(id, "fail" if error else "ok", str(error) if error else
                         ("configuration is at the repository root" if id == "repository-root" else
                          f"origin matches {config.repository}"), remedies[id] if error else None)
        else:
            for id in ("repository-root", "repository-remote"):
                self.add(id, "skip", "configuration unavailable" if not config else "git unavailable")
        github = self.github or GitHub(config.repository if config else "", runner=self.runner)
        login = None
        if gh_ready:
            try:
                login = github.actor()
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", login):
                    raise AgentError("no login")
                self.add("github-auth", "ok", f"authenticated as {login}; every launcher for this project must use this account")
            except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                login = None
                self.add("github-auth", "fail", self.github_failure("authentication", exc), "gh auth login")
        else:
            self.add("github-auth", "skip", "gh unavailable")
        if config and gh_ready:
            self.repository_access(config, github)
            self.labels(config, github)
            if login:
                try:
                    role = github.role(login)
                except AgentError:
                    role = None
                elevated = role in {"maintain", "admin"}
                self.add("github-launcher-role", "warn" if elevated else "ok" if role else "warn",
                         (f"Launcher account {login} has {role}; agents can start and approve their own work"
                          if elevated else f"Launcher account {login} has {role}"
                          if role else "Launcher repository role could not be read"),
                         "Use a dedicated launcher account with the write repository role"
                         if elevated else "Ensure the token can read collaborator permissions" if not role else None,
                         required=False)
        else:
            self.add("github-repository", "skip", "configuration unavailable" if not config else "gh unavailable")
            self.add("github-permissions", "skip", "repository response unavailable")
            self.add("github-labels", "skip", "configuration unavailable" if not config else "gh unavailable")
        if gh_ready:
            self.rate_limit(github.quota_headers, github.rate_limited)
        else:
            self.add("github-rate-limit", "skip", "gh unavailable", required=False)
        if config:
            self.agents(config)
            self.local(config, git_ready)
        else:
            for id in ("commands", "runtimes", "different-runtime-from", "local-state", "local-state-ignored"):
                self.add(id, "skip", "configuration unavailable")
        try:
            rows = parse_process_table(self.probe(["ps", "-axo", "pid=,pgid=,stat="]))
            if os.getpid() not in {pid for pid, _, _ in rows}:
                raise AgentError("process table does not include doctor's PID")
            self.add("process-inspection", "ok", "ps output parses and includes doctor's PID")
        except AgentError as exc:
            self.add("process-inspection", "fail", str(exc),
                     "Supervision needs ps; install/allow ps (sandboxes may block process inspection)")
        return {"version": 1, "ok": not any(c.required and c.status == "fail" for c in self.checks),
                "checks": [asdict(c) for c in self.checks]}

    def rate_limit(self, headers, limited):
        try:
            remaining = int(headers["x-ratelimit-remaining"])
            limit = int(headers["x-ratelimit-limit"])
            reset = iso(int(headers["x-ratelimit-reset"]))
            if limit <= 0 or not 0 <= remaining <= limit:
                raise ValueError("invalid quota")
            low = remaining < limit / 10
            message = f"{remaining} of {limit} requests remaining; reset {reset} (UTC)"
            status = "warn" if low or limited else "ok"
        except (KeyError, ValueError, OverflowError, OSError):
            message, status = "Request quota headers unavailable", "warn"
        if limited:
            message += "; doctor was rate limited by GitHub"
        self.add("github-rate-limit", status, message,
                 "Wait for the GitHub quota reset before launching more work" if status == "warn" else None,
                 required=False)

    @staticmethod
    def github_failure(subject, error):
        # GitHub exceptions may carry raw stderr. Only safe probe metadata escapes.
        reason = getattr(error, "probe_reason", "invalid or unavailable response")
        if isinstance(error, subprocess.TimeoutExpired):
            reason = "timed out (20s)"
        return f"GitHub {subject} failed ({reason})"

    def repository_access(self, config, github):
        try:
            raw = github.request(f"repos/{config.repository}")
            name = raw.get("full_name")
            if not isinstance(name, str) or not re.fullmatch(REPOSITORY, name):
                raise AgentError("no repository name")
            if name.casefold() != config.repository.casefold():
                self.add("github-repository", "fail", f"configured {config.repository}, GitHub returned {name}",
                         "Update the configured repository and origin to the intended repository")
            else:
                self.add("github-repository", "ok", f"access to {name}")
            # The launcher applies label transitions after the agent finishes; a token that
            # cannot change labels wastes the whole session before failing.
            permissions = raw.get("permissions") or {}
            if isinstance(permissions, dict) and any(permissions.get(key) is True
                                                     for key in ("triage", "push", "maintain", "admin")):
                self.add("github-permissions", "ok", "token can change labels for outcome transitions")
            else:
                self.add("github-permissions", "fail", "token cannot change labels, so outcome transitions would fail",
                         f"Ask a maintainer for triage or higher access to {config.repository}")
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.add("github-repository", "fail", self.github_failure("repository access", exc),
                     f"Authenticate with gh auth login and obtain access to {config.repository}")
            self.add("github-permissions", "skip", "repository response unavailable")

    def labels(self, config, github):
        try:
            existing = {name.casefold() for name in github.labels()}
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.add("github-labels", "fail", self.github_failure("labels", exc),
                     f"Authenticate with gh auth login and obtain access to {config.repository}")
            return
        agents = {agent.name: agent for agent in config.agents}
        for label in configured_labels(config):
            # One check per label and consuming agent; a stop use stays a warning
            # even when the same label is required by an outcome transition.
            for name in dict.fromkeys(use.agent for use in label.uses):
                uses = [use for use in label.uses if use.agent == name]
                present = label.name.casefold() in existing
                required = any(use.required for use in uses)
                self.add(f"github-label:{label.name}", "ok" if present else "fail" if required else "warn",
                         f"Label {label.name} {'exists' if present else 'is missing'}: "
                         + "; ".join(use.meaning for use in uses),
                         None if present else label.command(config.repository),
                         required=required, agent=agents.get(name))

    def agents(self, config):
        usable = {}
        for agent in config.agents:
            if agent.command:
                executable = agent.command[0]
                installed = self.which(executable)
                self.add("command", "ok" if installed else "fail",
                         f"executable {executable} {'resolves' if installed else 'does not resolve'}",
                         None if installed else f"Install {executable} or correct agent {agent.name}'s command",
                         agent=agent)
                continue
            if not agent.runtime_args:
                self.add("runtime-permissions", "warn", f"Agent {agent.name} has no runtime-args; unattended edits, commits or pushes may fail",
                         f"Configure this agent's runtime-args: {PERMISSIONS_URL}", required=False, agent=agent)
            alternatives = []
            for runtime in agent.runtimes:
                installed = self.which(runtime.cli)
                auth_error = None
                if installed:
                    try:
                        self.probe(AUTH_PROBES[runtime.cli])
                    except AgentError as exc:
                        auth_error = str(exc)
                alternatives.append((runtime, installed, auth_error))
            usable[agent.name] = [r for r, installed, error in alternatives if installed and not error]
            available = bool(usable[agent.name])
            unavailable_status = "warn" if available else "fail"
            for runtime, installed, auth_error in alternatives:
                kwargs = dict(agent=agent, runtime=runtime)
                self.add("runtime-executable", "ok" if installed else unavailable_status,
                         f"executable {runtime.cli} {'resolves' if installed else 'does not resolve'}",
                         None if installed else f"Install {runtime.cli} or remove {runtime.name} from agent {agent.name}'s runtime",
                         required=not available, **kwargs)
                remedy = None
                if auth_error:
                    remedy = (("Run codex login" if runtime.cli == "codex" else "Run claude auth login")
                              + f" or remove {runtime.name} from agent {agent.name}'s runtime")
                self.add("runtime-auth", "skip" if not installed else unavailable_status if auth_error else "ok",
                         "executable unavailable" if not installed else auth_error or "auth probe passed",
                         remedy, required=not available, **kwargs)
            self.add("runtimes", "ok" if available else "fail",
                     f"{len(usable[agent.name])} usable runtime alternative(s)",
                     None if available else f"Install and authenticate at least one runtime for agent {agent.name}", agent=agent)
        for agent in config.agents:
            if not agent.different_from:
                continue
            source = usable[agent.different_from]
            covered = [r for r in source if any(candidate.different_from(r) for candidate in usable[agent.name])]
            for runtime in source:
                if runtime not in covered:
                    self.add("different-runtime-from", "warn",
                             f"{agent.name} has no independent usable alternative for {agent.different_from} using {runtime.name}",
                             f"Install/configure a different CLI and model for {agent.name}, or remove this alternative from {agent.different_from}",
                             required=False, agent=agent, runtime=runtime)
            if not covered:
                self.add("different-runtime-from", "fail",
                         f"{agent.name} cannot independently run after {agent.different_from}",
                         f"Install/authenticate independent runtime alternatives for {agent.name} and {agent.different_from}", agent=agent)
            else:
                self.add("different-runtime-from", "ok", f"independent execution available after {agent.different_from}", agent=agent)

    def local(self, config, git_ready):
        local = config.root / ".ub-agent"
        try:
            if local.is_symlink() or (local.exists() and not (local.is_dir() and self.access(local, os.W_OK | os.X_OK))):
                raise AgentError(".ub-agent/ must be a real, writable directory")
            if not local.exists() and not self.access(config.root, os.W_OK):
                raise AgentError("project root is not writable")
            self.add("local-state", "ok", ".ub-agent/ is writable" if local.exists() else ".ub-agent/ created at launch")
        except (OSError, AgentError) as exc:
            self.add("local-state", "fail", str(exc) if isinstance(exc, AgentError) else ".ub-agent/ could not be inspected",
                     "Choose a writable project root and restore .ub-agent/ as a real directory owned by the current user")
        if git_ready:
            try:
                self.probe(["git", "-C", str(config.root), "check-ignore", "-q", ".ub-agent/"], config.root)
                self.add("local-state-ignored", "ok", ".ub-agent/ is ignored by Git")
            except AgentError as exc:
                self.add("local-state-ignored", "fail", f".ub-agent/ ignore check: {exc}", "Add .ub-agent/ to .gitignore (as ub-agent init does)")
        else:
            self.add("local-state-ignored", "skip", "git unavailable")


def diagnose(path, **kwargs):
    return Doctor(**kwargs).run(path)


def render(result, json_output=False):
    if json_output:
        print(json.dumps(result, indent=2))
        return
    for check in result["checks"]:
        scope = "/".join(value for value in (check["agent"], check["runtime"]) if value)
        print(f"{check['status']} {check['id']} {scope or '-'} {check['message']}")
        if check["status"] in {"warn", "fail"}:
            print(f"  remedy: {check['remedy']}")
    failures = sum(c["required"] and c["status"] == "fail" for c in result["checks"])
    warnings = sum(c["status"] == "warn" for c in result["checks"])
    print(f"{failures} required failures, {warnings} warnings")
