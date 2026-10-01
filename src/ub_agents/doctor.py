"""Read-only prerequisite diagnostics. Probe output is private unless it is a version."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from .config import load_config
from .errors import AgentError
from .execution import parse_process_table, repository_checks
from .github import GitHub
from .labels import configured_labels


PERMISSIONS_URL = "https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#runtime-permissions"


AUTH_PROBES = {"codex": ("codex", "login", "status"),
               "claude": ("claude", "auth", "status")}


# Configuration errors can quote YAML. Keep diagnostics on one line and redact
# recognizable credential forms even there and in otherwise-valid version lines.
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

    def version(self, command, cwd=None):
        output = self.probe(command, cwd)
        first = output.splitlines()[0] if output.splitlines() else ""
        return public(first) if re.search(r"\d+\.\d+", first) else "version probe passed"

    def tool(self, name, remedy):
        try:
            if not self.which(name):
                raise AgentError(f"{name} is not on PATH")
            message = self.version([name, "--version"])
            self.add(name, "ok", message)
            return True
        except AgentError as exc:
            self.add(name, "fail", f"{name}: {exc}", remedy)
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
        if gh_ready:
            try:
                login = github.actor()
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", login):
                    raise AgentError("no login")
                self.add("github-auth", "ok", f"authenticated as {login}")
            except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                self.add("github-auth", "fail", self.github_failure("authentication", exc), "gh auth login")
        else:
            self.add("github-auth", "skip", "gh unavailable")
        if config and gh_ready:
            self.repository_access(config, github)
            self.labels(config, github)
        else:
            self.add("github-repository", "skip", "configuration unavailable" if not config else "gh unavailable")
            self.add("github-permissions", "skip", "repository response unavailable", required=False)
            self.add("github-labels", "skip", "configuration unavailable" if not config else "gh unavailable")
        if config:
            self.agents(config)
            self.local(config, git_ready)
        else:
            for id in ("commands", "runtimes", "different-runtime-from", "local-state",
                       "local-state-ignored", "worktrees"):
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
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
                raise AgentError("no repository name")
            if name.casefold() != config.repository.casefold():
                self.add("github-repository", "fail", f"configured {config.repository}, GitHub returned {name}",
                         "Update the configured repository and origin to the intended repository")
            else:
                self.add("github-repository", "ok", f"access to {name}")
            permissions = raw.get("permissions") or {}
            if not isinstance(permissions, dict) or not any(permissions.get(key) is True for key in
                                                           ("triage", "push", "maintain", "admin")):
                self.add("github-permissions", "warn", "label handoffs in project instructions will fail",
                         f"Ask a maintainer for triage or higher access to {config.repository}", required=False)
            else:
                self.add("github-permissions", "ok", "label handoff permission available", required=False)
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.add("github-repository", "fail", self.github_failure("repository access", exc),
                     f"Authenticate with gh auth login and obtain access to {config.repository}")
            self.add("github-permissions", "skip", "repository response unavailable", required=False)

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
                version, version_error, auth_error = None, None, None
                if installed:
                    try:
                        version = self.version([runtime.cli, "--version"])
                    except AgentError as exc:
                        version_error = str(exc)
                    try:
                        self.probe(AUTH_PROBES[runtime.cli])
                    except AgentError as exc:
                        auth_error = str(exc)
                alternatives.append((runtime, installed, version, version_error, auth_error))
            usable[agent.name] = [r for r, installed, _, _, error in alternatives if installed and not error]
            available = bool(usable[agent.name])
            unavailable_status = "warn" if available else "fail"
            for runtime, installed, version, version_error, auth_error in alternatives:
                kwargs = dict(agent=agent, runtime=runtime)
                self.add("runtime-executable", "ok" if installed else unavailable_status,
                         f"executable {runtime.cli} {'resolves' if installed else 'does not resolve'}",
                         None if installed else f"Install {runtime.cli} or remove {runtime.name} from agent {agent.name}'s runtime",
                         required=not available, **kwargs)
                self.add("runtime-version", "skip" if not installed else "warn" if version_error else "ok",
                         "executable unavailable" if not installed else version_error or version,
                         f"Repair/reinstall {runtime.cli}" if version_error else None, required=False, **kwargs)
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
            if local.is_symlink():
                raise AgentError(".ub-agent/ must not be a symlink")
            if local.exists():
                if not local.is_dir() or not self.access(local, os.W_OK | os.X_OK):
                    raise AgentError(".ub-agent/ must be a writable, searchable directory")
                if stat.S_IMODE(local.stat().st_mode) & 0o077:
                    self.add("local-state", "warn", ".ub-agent/ has group or world permission bits",
                             "Run chmod 700 .ub-agent/ to restrict access to the current user", required=False)
                else:
                    self.add("local-state", "ok", ".ub-agent/ is a writable, searchable private directory")
            elif not self.access(config.root, os.W_OK):
                raise AgentError("project root is not writable")
            else:
                self.add("local-state", "ok", ".ub-agent/ created at launch")
        except (OSError, AgentError) as exc:
            self.add("local-state", "fail", str(exc) if isinstance(exc, AgentError) else ".ub-agent/ could not be inspected",
                     "Choose a writable project root; restore .ub-agent/ as a real directory owned by the current user")
        if git_ready:
            try:
                self.probe(["git", "-C", str(config.root), "check-ignore", "-q", ".ub-agent/"], config.root)
                self.add("local-state-ignored", "ok", ".ub-agent/ is ignored by Git")
            except AgentError as exc:
                self.add("local-state-ignored", "fail", f".ub-agent/ ignore check: {exc}", "Add .ub-agent/ to .gitignore (as ub-agent init does)")
        else:
            self.add("local-state-ignored", "skip", "git unavailable")
        names = ", ".join(a.name for a in config.agents if a.worktree)
        if not names:
            self.add("worktrees", "skip", "not configured")
        else:
            errors = []
            if git_ready:
                try:
                    self.probe(["git", "-C", str(config.root), "worktree", "list", "--porcelain"], config.root)
                except AgentError as exc:
                    errors.append(str(exc))
            if (local / "worktrees").is_symlink():
                errors.append(".ub-agent/worktrees must not be a symlink")
            self.add("worktrees", "fail" if errors else "ok" if git_ready else "skip",
                     f"agents {names}: " + ("; ".join(errors) if errors else "private worktrees available" if git_ready else "git unavailable"),
                     "Restore Git worktree support and a real .ub-agent/worktrees directory" if errors else None)


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
