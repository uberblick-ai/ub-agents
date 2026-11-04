"""Prerequisite diagnostics and confirmed label setup. Probe output stays private."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib.resources import files
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from ..config import instruction_text, load_config
from ..approvals import Roles, resolve_policy
from ..errors import AgentError
from ..execution import parse_process_table, repository_checks
from ..github import REPOSITORY, GitHub, repository_visibility
from ..labels import LabelUse, configured_labels, confirm_label_creation
from ..launcher_lock import running_launcher
from .mcp_probe import PROBE_TIMEOUT
from .registry import CHECK_AREAS, areas, check
from ..records import iso
from ..refresh import control_checkout_checks
from ..trust import WRITERS


PERMISSIONS_URL = "https://agents.uberblick.ai/docs/configuration/permissions.html"


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
    uses: list[LabelUse] | None = None
    server: str | None = None

    def record(self):
        # Init reuses the collector for setup-only ids without serializing them.
        if self.id.split(":", 1)[0] not in CHECK_AREAS:
            raise ValueError(f"Undeclared doctor check {self.id!r}")
        value = asdict(self)
        for key in ("uses", "server"):
            if value[key] is None:
                del value[key]
        return value


class Doctor:
    def __init__(self, runner=None, which=None, github=None, access=None):
        self.runner = runner or subprocess.run
        self.which = which or shutil.which
        self.github = github
        self.access = access or os.access
        self.checks = []
        self.config = None
        self.missing_labels = []
        self.approvals = None
        self.launcher = None
        self.progress = None
        self.area = None

    def add(self, id, status, message, remedy=None, *, required=True, agent=None, runtime=None, uses=None, server=None):
        # Scope repeated checks with stable configuration identities.
        scope = [id] + ([agent.name] if agent else []) + ([runtime.name] if runtime else [])
        if server is not None:
            scope.append(server)
        self._record(Check(":".join(scope), status, required,
                           agent.name if agent else None, runtime.name if runtime else None,
                           public(message), public(remedy) if remedy else None, uses, server))

    def _record(self, check):
        self.checks.append(check)
        if self.progress:
            self.notify("check", check.record())

    def notify(self, event, value=None):
        """Send header, area-start, step, check and area-end events, when requested."""
        if self.progress:
            self.progress(event, self.area, value)

    def waiting(self, label):
        self.notify("step", label)

    @contextmanager
    def checking(self, area):
        self.area = area
        start = len(self.checks)
        self.notify("area-start")
        yield
        self.notify("area-end", [check.record() for check in self.checks[start:]])

    def probe(self, command, cwd=None):
        try:
            result = self.runner(list(command), cwd=cwd, capture_output=True, text=True,
                                 timeout=PROBE_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise AgentError(f"probe timed out ({PROBE_TIMEOUT}s)") from None
        except (OSError, UnicodeError):
            raise AgentError("probe could not run") from None
        if result.returncode:
            raise AgentError(f"probe failed (exit {result.returncode})")
        return result.stdout

    @check("machine", "git", "gh")
    def tool(self, name, remedy):
        if self.which(name):
            self.add(name, "ok", f"{name} is on PATH")
            return True
        self.add(name, "fail", f"{name} is not on PATH", remedy)
        return False

    @check("configuration", "policy-placeholder", "policy-starter")
    def shared_policy(self, text, path):
        path = path.relative_to(self.config.root)
        if "@org/maintainers" in text:
            self.add("policy-placeholder:owner", "warn", f"{path} still contains @org/maintainers",
                     f"Replace @org/maintainers in {path} with the repository's person or team", required=False)
        commands = [name for name, placeholder in (("build", "<project build command>"),
                    ("test", "<project test command>"), ("lint", "<project lint or validation command>"))
                    if placeholder in text]
        remaining = []
        remedies = []
        if commands:
            names = " and ".join(commands) if len(commands) < 3 else ", ".join(commands[:-1]) + " and " + commands[-1]
            remaining.append(names + (" command" if len(commands) == 1 else " commands"))
            remedies.append("Replace the command placeholders with the repository's own required commands")

        def merging(content):
            section = re.search(r"^## Merging[ \t]*\r?\n(.*?)(?=^#{1,2} |\Z)", content, re.MULTILINE | re.DOTALL)
            if section is None:
                return None
            # Init substitutes a person or team; that alone does not tailor the rules.
            normalized = re.sub(r"`@[A-Za-z0-9-]+(?:/[A-Za-z0-9._-]+)?`", "`@org/maintainers`", section[1])
            normalized = " ".join(normalized.split())
            normalized = normalized.replace("- is labelled `outside-input` (built from outside text);", "")
            return " ".join(normalized.split())

        starter = files("ub_agents").joinpath("templates/ub_agents.md").read_text(encoding="utf-8")
        if merging(text) == merging(starter):
            remaining.append("the Merging section")
            remedies.append("Merge the Tailor loop policy PR or edit the Merging section yourself "
                            "to set the repository's required CI check and maintainer merge rules")
        if remaining:
            self.add("policy-starter", "hint", f"tailor {path}: " + ", and ".join(remaining),
                     "Starter policy needs the repository's own checks and merge rules. " + ". ".join(remedies),
                     required=False)

    def run(self, path, *, offline=False, progress=None):
        self.launcher = running_launcher(Path(path).resolve().parent)
        # The header needs only local metadata. Replay these checks when their
        # area starts so verbose output still follows the diagnostic order.
        config = self._load_configuration(path)
        self.config = config
        self._launcher_installation(config)
        initial, self.checks = self.checks, []
        self.progress = progress
        try:
            self.notify("header", {**self.result(), "checks": [check.record() for check in initial]})
            with self.checking("machine"):
                git_ready, gh_ready = self._machine(offline)
                self._process_inspection()
            with self.checking("configuration"):
                for check in initial:
                    self._record(check)
                self._configuration(config, git_ready)
            with self.checking("GitHub"):
                if offline:
                    self.offline(config)
                else:
                    self._online(config, git_ready, gh_ready)
            with self.checking("runtimes"):
                self._runtimes(config, offline)
            return self.result()
        finally:
            self.progress = self.area = None

    def _configuration(self, config, git_ready):
        self._instructions(config)
        self._repository(config, git_ready)
        if config:
            self.local(config, git_ready)
        else:
            for id in ("local-state", "local-state-ignored"):
                self.add(id, "skip", "configuration unavailable")

    @check("machine", "python", "platform")
    def _machine(self, offline):
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
        gh_ready = False
        if offline:
            self.add("gh", "skip", "offline: GitHub CLI is not needed")
        else:
            gh_ready = self.tool("gh", "Install GitHub CLI from https://cli.github.com and put gh on PATH")
        return git_ready, gh_ready

    @check("configuration", "config")
    def _load_configuration(self, path):
        config = None
        try:
            config = load_config(path)
            self.add("config", "ok", f"{config.repository}, {len(config.agents)} agents")
        except (AgentError, OSError, UnicodeError) as exc:
            missing = not Path(path).exists()
            self.add("config", "fail", f"no {Path(path).name} here; run ub-agents init" if missing else str(exc),
                     "Run ub-agents init to create the configuration" if missing else
                     "Fix the configuration error in the selected YAML file")
        return config

    @check("configuration", "instructions")
    def _instructions(self, config):
        if config:
            if config.shared_instructions is not None:
                try:
                    text = instruction_text(config.root, config.shared_instructions, "shared-instructions")
                    self.add("instructions", "ok", "shared instruction file is readable")
                    self.shared_policy(text, config.shared_instructions)
                except AgentError as exc:
                    self.add("instructions", "fail", str(exc),
                             f"Restore a readable shared instruction file at {config.shared_instructions}")
            for agent in config.agents:
                if agent.instructions is None:
                    self.add("instructions", "skip", "not configured", agent=agent)
                    continue
                try:
                    instruction_text(config.root, agent.instructions, f"{agent.name} instructions")
                    self.add("instructions", "ok", "instruction file is readable", agent=agent)
                except AgentError:
                    self.add("instructions", "fail", "instruction file is not readable",
                             f"Restore a readable instruction file at {agent.instructions}", agent=agent)
        else:
            self.add("instructions", "skip", "configuration unavailable")

    @check("configuration", "repository-root", "repository-remote")
    def _repository(self, config, git_ready):
        if config and git_ready:
            remedies = {"repository-root": "Place the configuration at the consuming Git repository root",
                        "repository-remote": f"Set origin to https://github.com/{config.repository}.git or "
                                             f"git@github.com:{config.repository}.git "
                                             "(an SSH host alias for github.com also works)"}
            for id, error in repository_checks(config, self._read_git, self.runner):
                self.add(id, "fail" if error else "ok", str(error) if error else
                         ("configuration is at the repository root" if id == "repository-root" else
                          f"origin matches {config.repository}"), remedies[id] if error else None)
        else:
            for id in ("repository-root", "repository-remote"):
                self.add(id, "skip", "configuration unavailable" if not config else "git unavailable")

    @check("configuration", "launcher-installation")
    def _launcher_installation(self, config):
        from ..updates.self_updates import installation
        install, reason = installation()
        policy = (config.runtime_updates.policies.get("ub-agents", "off")
                  if config and config.runtime_updates else "off")
        self.add("launcher-installation", "skip" if reason else "ok",
                 f"ub-agents: {reason}" if reason else
                 f"ub-agents: uberblick-ai/tap Homebrew formula at {install.cellar}; automatic updates: {policy}",
                 required=False)

    def _read_git(self, root, *args):
        return self.probe(["git", "-C", str(root), *args], root).strip()

    def _online(self, config, git_ready, gh_ready):
        github = self.github or GitHub(config.repository if config else "", runner=self.runner)
        self.config, self.github = config, github
        metadata = None
        login = self._github_auth(github, gh_ready)
        if config and gh_ready:
            metadata = self.repository_access(config, github)
            try:
                self.approvals, _ = resolve_policy(config.approvals, lambda: repository_visibility(metadata))
            except AgentError:
                self.approvals = None
            self.labels(config, github)
            self._launcher_roles(config, github, login)
        else:
            self.add("github-repository", "skip", "configuration unavailable" if not config else "gh unavailable")
            self.add("github-permissions", "skip", "repository response unavailable")
            self.add("github-labels", "skip", "configuration unavailable" if not config else "gh unavailable")
        self._control_checkout(config, git_ready, metadata)
        self._approval_policy(config, gh_ready, metadata)
        self._retrospectives(config, github, gh_ready)
        if gh_ready:
            self.rate_limit(github.quota_headers, github.rate_limited)
        else:
            self.add("github-rate-limit", "skip", "gh unavailable", required=False)

    @check("machine", "process-inspection")
    def _process_inspection(self):
        try:
            rows = parse_process_table(self.probe(["ps", "-axo", "pid=,pgid=,stat="]))
            if os.getpid() not in {pid for pid, _, _ in rows}:
                raise AgentError("process table does not include doctor's PID")
            self.add("process-inspection", "ok", "ps output parses and includes doctor's PID")
        except AgentError as exc:
            self.add("process-inspection", "fail", str(exc),
                     "Supervision needs ps; install/allow ps (sandboxes may block process inspection)")

    @check("GitHub", "github-auth")
    def _github_auth(self, github, gh_ready):
        login = None
        if gh_ready:
            try:
                self.waiting("reading authenticated account")
                login = github.actor()
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", login):
                    raise AgentError("no login")
                self.add("github-auth", "ok", f"authenticated as {login}")
            except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                login = None
                self.add("github-auth", "fail", self.github_failure("authentication", exc), "gh auth login")
        else:
            self.add("github-auth", "skip", "gh unavailable")
        return login

    @check("GitHub", "github-launcher-role", "github-launcher-listed", "github-launcher-account")
    def _launcher_roles(self, config, github, login):
        roles = Roles(github)
        if login:
            try:
                self.waiting(f"reading repository role for {login}")
                role = roles({"login": login})
            except AgentError:
                role = None
            elevated = role in {"maintain", "admin"}
            self.add("github-launcher-role", "warn" if elevated or role not in WRITERS else "ok",
                     (f"Launcher account {login} has {role}; agents can start and approve their own work"
                      if elevated else f"Launcher account {login} has {role}"
                      if role else "Launcher repository role could not be read"),
                     "Use a dedicated launcher account with the write repository role"
                     if elevated else "Ensure the token can read collaborator permissions" if not role else None,
                     required=False)
            if config.launchers is not None and login.casefold() not in {v.casefold() for v in config.launchers}:
                self.add("github-launcher-listed", "warn", f"Authenticated account {login} is unlisted; it claims no work",
                         "Use a listed launcher account or add this account to launchers", required=False)
        for account in config.launchers or ():
            try:
                self.waiting(f"reading repository role for {account}")
                role = roles({"login": account})
            except AgentError:
                role = None
            self.add(f"github-launcher-account:{account}", "ok" if role in WRITERS else "warn",
                     f"Listed launcher account {account} has {role}" if role else
                     f"Listed launcher account {account}'s repository role could not be read",
                     "Listed accounts require write or higher and readable collaborator permissions"
                     if role not in WRITERS else None, required=False)

    @check("GitHub", "control-checkout", "control-checkout-branch", "control-checkout-dirty", "control-checkout-origin")
    def _control_checkout(self, config, git_ready, metadata):
        self.waiting("checking the control checkout")
        default = metadata.get("default_branch") if metadata else None
        if config and git_ready and isinstance(default, str) and default:
            for id, error in control_checkout_checks(config, default, self._read_git):
                self.add(id, "warn" if error else "ok", str(error) if error else
                         {"control-checkout-branch": f"control checkout is on {default}",
                          "control-checkout-dirty": "control checkout is clean",
                          "control-checkout-origin": f"control checkout has no commits outside origin/{default}"}[id],
                         required=False)
        else:
            self.add("control-checkout", "skip", "configuration unavailable" if not config else
                     "git unavailable" if not git_ready else "repository default branch unavailable", required=False)

    @check("GitHub", "approvals")
    def _approval_policy(self, config, gh_ready, metadata):
        if config and config.approvals is None and metadata is None:
            self.add("approvals", "skip", "gh unavailable" if not gh_ready else
                     "repository response unavailable")
        elif config:
            try:
                value, source = resolve_policy(config.approvals, lambda: repository_visibility(metadata))
                self.add("approvals", "ok", f"Approvals: {value} ({source})")
            except AgentError:
                self.add("approvals", "fail", "Approvals: repository visibility is unreadable",
                         "Ensure the token can read repository visibility, or configure approvals: on or off")
        else:
            self.add("approvals", "skip", "configuration unavailable")

    @check("GitHub", "github-retrospectives")
    def _retrospectives(self, config, github, gh_ready):
        if config:
            for agent in config.agents:
                if agent.retrospectives is None:
                    continue
                number = agent.retrospectives
                if not gh_ready:
                    self.add("github-retrospectives", "skip", f"Discussion #{number}: gh unavailable", agent=agent)
                    continue
                try:
                    self.waiting(f"reading discussion {number} for {agent.name}")
                    github.discussion(number)
                    self.add("github-retrospectives", "ok",
                             f"{agent.name}: discussion #{number} exists in {config.repository}", agent=agent)
                except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
                    self.add("github-retrospectives", "fail",
                             f"{agent.name}: discussion #{number} in {config.repository} is missing or unreadable "
                             f"({self.github_failure('discussion lookup', exc)})",
                             "Set retrospectives to an existing discussion number in the configured repository "
                             "and ensure the token can read it", agent=agent)

    def offline(self, config):
        reason = "offline: GitHub access is not checked"
        for id in ("github-auth", "github-repository", "github-permissions", "github-labels",
                   "github-launcher-role", "github-launcher-listed", "github-rate-limit"):
            self.add(id, "skip", reason, required=id not in {
                "github-launcher-role", "github-launcher-listed", "github-rate-limit"})
        self.add("control-checkout", "skip", "offline: launcher checkout readiness is not checked", required=False)
        if not config:
            self.add("approvals", "skip", "configuration unavailable")
            return
        for label in configured_labels(config):
            self.add(f"github-label:{label.name}", "skip", reason,
                     required=any(use.required for use in label.uses), uses=list(label.uses))
        for account in config.launchers or ():
            self.add(f"github-launcher-account:{account}", "skip", reason, required=False)
        self.add("approvals", "ok", f"Approvals: {config.approvals} (config)" if config.approvals is not None else
                 "Approvals: from repository visibility (public: on; private/internal: off); offline: visibility not resolved")
        for agent in config.agents:
            if agent.retrospectives is not None:
                self.add("github-retrospectives", "skip", "offline: GitHub discussions are not checked", agent=agent)

    @check("runtimes", "commands")
    def _runtimes(self, config, offline):
        from .doctor_mcp import MCPChecks
        if config:
            if offline:
                self._offline_runtimes(config)
            else:
                self.agents(config)
            MCPChecks(config).run(self, offline=offline)
        else:
            for id in ("commands", "runtimes", "different-runtime-from"):
                self.add(id, "skip", "offline: agent runtimes and commands are not checked"
                         if offline else "configuration unavailable")

    def _offline_runtimes(self, config):
        reason = "offline: agent runtimes and commands are not checked"
        self.add("runtime-permissions", "skip", reason, required=False)
        for agent in config.agents:
            if agent.command:
                self.add("command", "skip", reason, agent=agent)
            else:
                for runtime in agent.runtimes:
                    for id in ("runtime-executable", "runtime-auth"):
                        self.add(id, "skip", reason, agent=agent, runtime=runtime)
                self.add("runtimes", "skip", reason, agent=agent)
            if agent.different_from:
                self.add("different-runtime-from", "skip", reason, agent=agent)

    def result(self):
        return {"version": 2, "ok": not any(c.required and c.status == "fail" for c in self.checks),
                "checks": [check.record() for check in self.checks], "launcher": self.launcher}

    def create_missing_labels(self):
        if not confirm_label_creation(self.config, self.github, self.missing_labels):
            return False
        # Recheck labels rather than treating successful writes as proof they exist.
        self.checks = [check for check in self.checks
                       if check.id.split(":", 1)[0] not in {"github-label", "github-labels", "github-rate-limit"}]
        self.labels(self.config, self.github)
        self.rate_limit(self.github.quota_headers, self.github.rate_limited)
        return True

    @check("GitHub", "github-rate-limit")
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

    @check("GitHub", "github-repository", "github-permissions")
    def repository_access(self, config, github):
        try:
            self.waiting("reading repository")
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
                         f"Ask a maintainer for write access to {config.repository}")
            return raw
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.add("github-repository", "fail", self.github_failure("repository access", exc),
                     f"Authenticate with gh auth login and obtain access to {config.repository}")
            self.add("github-permissions", "skip", "repository response unavailable")

    @check("GitHub", "github-labels", "github-label")
    def labels(self, config, github):
        self.missing_labels = []
        try:
            self.waiting("reading labels")
            existing = {name.casefold() for name in github.labels()}
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            self.add("github-labels", "fail", self.github_failure("labels", exc),
                     f"Authenticate with gh auth login and obtain access to {config.repository}")
            return
        for label in configured_labels(config, approvals=self.approvals):
            present = label.name.casefold() in existing
            required = any(use.required for use in label.uses)
            self.add(f"github-label:{label.name}", "ok" if present else "fail" if required else "warn",
                     f"Label {label.name} {'exists' if present else 'is missing'}: {label.explanation}",
                     None if present else label.command(config.repository),
                     required=required, uses=list(label.uses))
            if not present:
                self.missing_labels.append(label)

    @check("runtimes", "runtime-permissions", "command", "different-runtime-from")
    def agents(self, config):
        usable = {}
        without_permissions = [agent.name for agent in config.agents
                               if any(not agent.runtime_args_for(runtime) for runtime in agent.runtimes)]
        if without_permissions:
            self.add("runtime-permissions", "warn",
                     f"Agents without runtime-args: {', '.join(without_permissions)}; unattended edits, commits or pushes may fail",
                     f"Configure top-level runtime-args or agent overrides covering every listed CLI: {PERMISSIONS_URL}", required=False)
        for agent in config.agents:
            if agent.command:
                executable = agent.command[0]
                installed = self.which(executable)
                self.add("command", "ok" if installed else "fail",
                         f"executable {executable} {'resolves' if installed else 'does not resolve'}",
                         None if installed else f"Install {executable} or correct agent {agent.name}'s command",
                         agent=agent)
                continue
            usable[agent.name] = self.agent_runtimes(agent)
        for agent in config.agents:
            for name in agent.different_from:
                check_id = ("different-runtime-from" if len(agent.different_from) == 1
                            else f"different-runtime-from:{name}")
                source = usable[name]
                covered = [r for r in source if any(candidate.different_from(r) for candidate in usable[agent.name])]
                for runtime in source:
                    if runtime not in covered:
                        self.add(check_id, "warn",
                                 f"{agent.name} has no independent usable alternative for {name} using {runtime.name}",
                                 f"Install/configure a different CLI and model for {agent.name}, or remove this alternative from {name}",
                                 required=False, agent=agent, runtime=runtime)
                if not covered:
                    self.add(check_id, "fail",
                             f"{agent.name} cannot independently run after {name}",
                             f"Install/authenticate independent runtime alternatives for {agent.name} and {name}", agent=agent)
                else:
                    self.add(check_id, "ok", f"independent execution available after {name}", agent=agent)

    @check("runtimes", "runtime-executable", "runtime-auth", "runtimes")
    def agent_runtimes(self, agent):
        alternatives = []
        for runtime in agent.runtimes:
            installed = self.which(runtime.cli)
            auth_error = None
            if installed:
                try:
                    self.waiting(f"{runtime.cli} auth for {agent.name}")
                    self.probe(AUTH_PROBES[runtime.cli])
                except AgentError as exc:
                    auth_error = str(exc)
            alternatives.append((runtime, installed, auth_error))
        usable = [r for r, installed, error in alternatives if installed and not error]
        available = bool(usable)
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
                 f"{len(usable)} usable runtime alternative(s)",
                 None if available else f"Install and authenticate at least one runtime for agent {agent.name}", agent=agent)
        return usable

    @check("configuration", "local-state", "local-state-ignored")
    def local(self, config, git_ready):
        local = config.root / ".ub-agents"
        try:
            if local.is_symlink() or (local.exists() and not (local.is_dir() and self.access(local, os.W_OK | os.X_OK))):
                raise AgentError(".ub-agents/ must be a real, writable directory")
            if not local.exists() and not self.access(config.root, os.W_OK):
                raise AgentError("project root is not writable")
            self.add("local-state", "ok", ".ub-agents/ is writable" if local.exists() else ".ub-agents/ created at launch")
        except (OSError, AgentError) as exc:
            self.add("local-state", "fail", str(exc) if isinstance(exc, AgentError) else ".ub-agents/ could not be inspected",
                     "Choose a writable project root and restore .ub-agents/ as a real directory owned by the current user")
        if git_ready:
            try:
                self.probe(["git", "-C", str(config.root), "check-ignore", "-q", ".ub-agents/"], config.root)
                self.add("local-state-ignored", "ok", ".ub-agents/ is ignored by Git")
            except AgentError as exc:
                self.add("local-state-ignored", "fail", f".ub-agents/ ignore check: {exc}", "Add .ub-agents/ to .gitignore (as ub-agents init does)")
        else:
            self.add("local-state-ignored", "skip", "git unavailable")


def diagnose(path, *, offline=False, **kwargs):
    return Doctor(**kwargs).run(path, offline=offline)



def render(result, json_output=False, verbose=False, *, elapsed=0.0):
    from .doctor_output import render as render_output
    render_output(result, areas(), json_output, verbose, elapsed=elapsed)
