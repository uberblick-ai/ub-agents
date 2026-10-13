"""Advisory wizard probes: bounded reads, no setup mutations or test posts."""

import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from .approvals import Roles, resolve_policy
from .doctor import AUTH_PROBES, Doctor
from .errors import AgentError
from .execution import parse_process_table, repository_checks
from .github import repository_visibility
from .refresh import control_checkout_checks

ACCOUNT_GUIDANCE = "https://agents.uberblick.ai/docs/best-practices/launcher-account.html"
PROBE_ERRORS = (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired)


def findings(checks):
    names = {"ok": "PASS", "fail": "FAIL", "warn": "WARNING", "skip": "UNAVAILABLE / SKIPPED"}
    lines = []
    for check in checks:
        suffix = " (blocks launch; setup may continue)" if check.required and check.status in {"fail", "skip"} else ""
        lines.append(f"{names[check.status]}: {check.message}{suffix}")
        if check.remedy:
            lines.append(f"  Next: {check.remedy}")
    return "\n\n".join(lines)


class Probes:
    def __init__(self, plan, github, doctor=None):
        self.plan, self.github = plan, github
        self.doctor = doctor or Doctor(github=github)
        self.welcome = []
        self.prerequisites = []
        self.checkout = []
        self.runtime = []
        self.existing_labels = None

    def run(self):
        d, config = self.doctor, self.plan.config
        self.existing_labels = None
        d.add("version", "ok", f"Installed ub-agents {__version__}", required=False)
        git_ready = bool(d.which("git"))
        gh_ready = bool(d.which("gh"))
        if gh_ready:
            try:
                release = self.github.request("repos/uberblick-ai/ub-agents/releases/latest")
                tag = release.get("tag_name")
                if not isinstance(tag, str) or not tag.strip():
                    raise AgentError("invalid release")
                d.add("latest-version", "ok", f"Latest published version: {tag}; no upgrade is performed", required=False)
            except PROBE_ERRORS:
                d.add("latest-version", "skip", "Latest version lookup unavailable; you can continue",
                      "Check connectivity and https://github.com/uberblick-ai/ub-agents/releases later", required=False)
        else:
            d.add("latest-version", "skip", "Latest version lookup skipped: gh unavailable",
                  "Install gh, then check https://github.com/uberblick-ai/ub-agents/releases", required=False)
        self.welcome = d.checks[:]
        d.checks.clear()
        d.add("python", "ok", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        d.add("platform", "ok" if sys.platform in {"darwin", "linux"} else "fail", sys.platform,
              "Use macOS or Linux" if sys.platform not in {"darwin", "linux"} else None)
        d.tool("git", "Install Git and put git on PATH")
        d.tool("gh", "Install GitHub CLI from https://cli.github.com")
        actor = None
        if gh_ready:
            try:
                actor = self.github.actor()
                d.add("github-auth", "ok", f"gh authenticated as {actor}")
            except PROBE_ERRORS:
                d.add("github-auth", "fail", "gh authentication failed", "Run gh auth login")
        else:
            d.add("github-auth", "skip", "gh authentication not checked", "Install gh and run gh auth login")
        metadata = d.repository_access(config, self.github) if gh_ready else None
        if not gh_ready:
            d.add("github-repository", "skip", f"Access to {config.repository} not checked", "Install and authenticate gh")
        if actor:
            try:
                role = Roles(self.github)({"login": actor})
                elevated = role in {"maintain", "admin"}
                d.add("account", "warn" if elevated else "ok" if role == "write" else "warn",
                      f"Launcher account {actor}: repository role {role or 'unavailable'}",
                      f"Use a separate write-access launcher account: {ACCOUNT_GUIDANCE}", required=False)
            except PROBE_ERRORS:
                d.add("account", "skip", "Launcher repository role unavailable",
                      f"Review account access: {ACCOUNT_GUIDANCE}", required=False)
            if config.launchers is not None and actor.casefold() not in {v.casefold() for v in config.launchers}:
                d.add("launcher-listed", "warn", f"{actor} is not listed in local launchers; it will claim no work",
                      "Review the existing local override file", required=False)
        try:
            value, source = resolve_policy(config.approvals, lambda: repository_visibility(metadata))
            d.add("approvals", "ok", f"Approvals: {value} ({source}); review the project's authority policy")
        except PROBE_ERRORS:
            d.add("approvals", "skip", "Repository approval defaults could not be determined",
                  "Run ub-agents doctor after repairing repository access")
        try:
            rows = parse_process_table(d.probe(["ps", "-axo", "pid=,pgid=,stat="]))
            if os.getpid() not in {pid for pid, _, _ in rows}:
                raise AgentError("missing process")
            d.add("process-inspection", "ok", "ps supports process supervision")
        except PROBE_ERRORS:
            d.add("process-inspection", "fail", "ps process supervision unavailable", "Install or allow ps")
        parent = config.root
        while not parent.exists():
            parent = parent.parent
        d.add("local-state", "ok" if os.access(parent, os.W_OK | os.X_OK) else "fail",
              "Setup directory is writable" if os.access(parent, os.W_OK | os.X_OK) else "Setup directory is not writable",
              "Choose a writable checkout" if not os.access(parent, os.W_OK | os.X_OK) else None)
        self.prerequisites = d.checks[:]
        d.checks.clear()
        read_git = lambda root, *args: d.probe(["git", "-C", str(root), *args]).strip()
        if git_ready:
            for id, error in repository_checks(config, read_git):
                d.add(id, "fail" if error else "ok", str(error) if error else
                      "Configuration is at the repository root" if id == "repository-root" else
                      f"origin points to {config.repository}", "Repair the checkout or origin before launch" if error else None)
            try:
                branch = read_git(config.root, "rev-parse", "--abbrev-ref", "HEAD")
                d.add("branch", "ok", f"Current branch: {branch}", required=False)
            except PROBE_ERRORS:
                d.add("branch", "skip", "Current branch unavailable", "Run init inside a Git checkout")
        default = metadata.get("default_branch") if metadata else None
        if isinstance(default, str) and default:
            d.add("default-branch", "ok", f"Actual default branch: {default}", required=False)
            if git_ready:
                for id, error in control_checkout_checks(config, default, read_git):
                    d.add(id, "warn" if error else "ok", str(error) if error else
                          {"control-checkout-branch": f"Launch branch matches {default}",
                           "control-checkout-dirty": "Checkout is clean",
                           "control-checkout-origin": f"No local commits ahead of origin/{default} (local ref)"}[id],
                          "Resolve this condition before launch; setup on this branch is allowed" if error else None,
                          required=False)
        else:
            d.add("default-branch", "skip", "Default branch and comparison unavailable",
                  "Repair GitHub access; run git fetch origin and ub-agents doctor before launch")
        self.checkout = d.checks[:]
        d.checks.clear()
        if gh_ready:
            try:
                self.existing_labels = {name.casefold() for name in self.github.labels()}
            except PROBE_ERRORS:
                pass
        self.check_runtime(config)
        return self

    def check_runtime(self, config):
        d = self.doctor
        d.checks.clear()
        for runtime in sorted({r.name: r for a in config.agents for r in a.runtimes}.values(), key=lambda r: r.name):
            installed = d.which(runtime.cli)
            d.add("runtime-executable", "ok" if installed else "fail",
                  f"{runtime.name}: executable {installed or 'unavailable'}",
                  f"Install {runtime.cli}" if not installed else None)
            if installed:
                try:
                    d.probe(AUTH_PROBES[runtime.cli])
                    d.add("runtime-login", "ok", f"{runtime.cli} login probe passed")
                except PROBE_ERRORS:
                    d.add("runtime-login", "fail", f"{runtime.cli} login probe failed",
                          "Run codex login" if runtime.cli == "codex" else "Run claude auth login")
            else:
                d.add("runtime-login", "skip", f"{runtime.cli} login not checked", f"Install and log in to {runtime.cli}")
        self.runtime = d.checks[:]
        d.checks.clear()

    def discussion(self, number):
        # The GitHub client enforces the exact target repository URL too.
        result = self.github.discussion(number)
        expected = f"https://github.com/{self.plan.config.repository}/discussions/{number}"
        if result.get("url") != expected:
            raise AgentError(f"Discussion URL must be exactly {expected}")
        return expected
