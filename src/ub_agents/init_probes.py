"""Read-only setup checks, grouped into one line per area."""

from dataclasses import dataclass, field
import os
import subprocess
import sys

from .approvals import Roles
from .doctor import AUTH_PROBES, Doctor
from .errors import AgentError
from .execution import parse_process_table, repository_checks
from .refresh import control_checkout_checks

ACCOUNT_GUIDANCE = "https://agents.uberblick.ai/docs/best-practices/launcher-account"
PROBE_ERRORS = (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired)
CLIS = ("claude", "codex")


@dataclass
class Group:
    name: str
    summary: list[str] = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def status(self):
        statuses = {check.status for check in self.problems}
        return "fail" if statuses & {"fail", "skip"} else "warn" if statuses else "ok"


class Probes:
    def __init__(self, plan, github, doctor=None):
        self.plan, self.github = plan, github
        self.doctor = doctor or Doctor(github=github)
        self.groups = []
        self.clis = {}
        self.existing_labels = None
        self.discussions = None
        self.discussions_note = ""
        self.branch = self.default_branch = self.clean = None

    def collect(self, name, summary=()):
        group = Group(name, [part for part in summary if part])
        group.problems = [check for check in self.doctor.checks if check.status != "ok"]
        self.doctor.checks.clear()
        self.groups.append(group)
        return group

    def run(self):
        d, config = self.doctor, self.plan.config
        d.checks.clear()
        self.groups = []
        python = f"Python {sys.version_info.major}.{sys.version_info.minor}"
        d.add("platform", "ok" if sys.platform in {"darwin", "linux"} else "fail", sys.platform, "Use macOS or Linux")
        git_ready = d.tool("git", "Install Git and put git on PATH")
        gh_ready = d.tool("gh", "Install GitHub CLI from https://cli.github.com")
        try:
            rows = parse_process_table(d.probe(["ps", "-axo", "pid=,pgid=,stat="]))
            if os.getpid() not in {pid for pid, _, _ in rows}:
                raise AgentError("missing process")
        except PROBE_ERRORS:
            d.add("process-inspection", "fail", "ps can't inspect processes", "Install or allow ps")
        parent = config.root
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            d.add("local-state", "fail", "The setup directory is not writable", "Run init in a writable checkout")
        self.collect("Machine", [python, "git" if git_ready else "", "gh" if gh_ready else "", "ps"])

        actor = metadata = None
        if gh_ready:
            try:
                actor = self.github.actor()
            except PROBE_ERRORS:
                d.add("github-auth", "fail", "gh is not logged in", "Run gh auth login")
            if actor:
                metadata = d.repository_access(config, self.github)
        github = self.collect("GitHub", [actor, config.repository])
        if metadata and not github.problems:
            github.summary.append("labels writable")

        if git_ready:
            read_git = lambda root, *args: d.probe(["git", "-C", str(root), *args]).strip()
            branch, clean = None, None
            for id, error in repository_checks(config, read_git):
                if error:
                    d.add(id, "fail", str(error), "Run init at the root of a checkout whose origin is the repository")
            try:
                branch = read_git(config.root, "rev-parse", "--abbrev-ref", "HEAD")
                clean = not read_git(config.root, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all")
            except PROBE_ERRORS:
                d.add("branch", "fail", "This is not a Git checkout", "Run init inside a clone of the repository")
            default = metadata.get("default_branch") if metadata else None
            self.branch, self.default_branch = branch, default if isinstance(default, str) and default else None
            synced = False
            if isinstance(default, str) and default and branch:
                # init asks to switch branches itself; a dirty tree is expected during setup.
                problems = [error for id, error in control_checkout_checks(config, default, read_git)
                            if error and id not in {"control-checkout-branch", "control-checkout-dirty"}]
                for error in problems:
                    d.add("control-checkout", "warn", str(error), "Fine for setup; launch from a clean default-branch checkout",
                          required=False)
                synced = not problems
            self.clean = clean
            self.collect("Checkout", [branch, "clean" if clean else "local changes" if clean is False else "",
                                      "in sync with origin" if synced else ""])

        for cli in CLIS:
            if not d.which(cli):
                self.clis[cli] = "not installed"
                continue
            try:
                d.probe(AUTH_PROBES[cli])
                self.clis[cli] = "logged in"
            except PROBE_ERRORS:
                self.clis[cli] = "not logged in"
        if all(state == "not installed" for state in self.clis.values()):
            d.add("runtime", "fail", "Neither claude nor codex is installed", "Install Claude Code or Codex before launch")
        elif "logged in" not in self.clis.values():
            d.add("runtime-login", "warn", "No agent CLI is logged in", "Run claude auth login or codex login before launch",
                  required=False)
        self.collect("Agents", [f"{cli} {state}" for cli, state in self.clis.items()])

        if actor:
            try:
                role = Roles(self.github)({"login": actor})
            except PROBE_ERRORS:
                role = None
            if role in {"maintain", "admin"}:
                d.add("account", "warn", f"{actor} is {role} on GitHub; use a dedicated agent account with write access",
                      f"See {ACCOUNT_GUIDANCE}", required=False)
            elif role is None:
                d.add("account", "warn", f"{actor}'s repository role can't be read", f"See {ACCOUNT_GUIDANCE}", required=False)
            elif role != "write":
                d.add("account", "fail", f"{actor} can't push to {config.repository}", f"See {ACCOUNT_GUIDANCE}")
            if config.launchers is not None and actor.casefold() not in {v.casefold() for v in config.launchers}:
                d.add("launcher-listed", "warn", f"{actor} is not in local launchers; it will claim no work",
                      "Review ub-agents.local.yaml", required=False)
            account = self.collect("Account")
            if not account.problems:
                self.groups.remove(account)

        if metadata:
            try:
                self.existing_labels = {name.casefold() for name in self.github.labels()}
            except PROBE_ERRORS:
                pass
            self.check_discussions()
        else:
            self.discussions_note = "GitHub is unavailable"
        return self

    def check_discussions(self):
        from .init_boards import Boards
        try:
            repo = Boards(self.github, self.plan.config.repository).discussions()
        except PROBE_ERRORS:
            self.discussions_note = "Discussions can't be checked"
            return
        if repo["hasDiscussionsEnabled"] is not True:
            self.discussions_note = f"Discussions are off in {self.plan.config.repository}"
        elif repo["viewerPermission"] not in {"WRITE", "MAINTAIN", "ADMIN"}:
            self.discussions_note = "this account can't create discussions"
        elif not repo["discussionCategories"]["nodes"]:
            self.discussions_note = "the repository has no discussion categories"
        else:
            self.discussions = repo

    def runtime(self, preferred=None):
        """The CLI to preselect: an explicit choice, else the logged-in one (Codex when both)."""
        if preferred:
            return preferred
        ready = [cli for cli in ("codex", "claude") if self.clis.get(cli) == "logged in"]
        installed = [cli for cli in ("codex", "claude") if self.clis.get(cli, "not installed") != "not installed"]
        return (ready or installed or ["codex"])[0]
