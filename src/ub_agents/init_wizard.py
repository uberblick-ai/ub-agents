"""Wizard pages shared by the terminal view and the readable line fallback."""

from dataclasses import dataclass
import json

from . import __version__
from .config import LOCAL_CONFIG
from .errors import AgentError
from .init_probes import Probes, PROBE_ERRORS, findings
from .init_setup import (ROLES, apply_plan, guidance_summary, make_plan, next_steps,
                         permission_args, priority_labels, recovery_content)
from .labels import configured_labels


@dataclass
class Field:
    name: str
    label: str
    value: str | bool = ""


@dataclass
class Page:
    title: str
    text: str
    fields: tuple[Field, ...] = ()
    optional: bool = False


class Wizard:
    def __init__(self, plan, github, probes=None):
        self.plan, self.github = plan, github
        self.choices = plan.choices
        self.probes = probes or Probes(plan, github)
        self.current = "welcome"
        self.applied = None
        self.cancelled = False
        self.started = False

    def probe(self):
        self.probes.run()
        self.started = True

    def pages(self):
        pages = ["welcome", "prerequisites", "checkout", "policy", "permissions", "files", "features"]
        if "priority" in self.choices.features:
            pages.append("priority")
            if self.missing(priority_labels(self.choices)):
                pages.append("priority-labels")
        pages.extend(name for name in ("retrospectives", "knowledge") if name in self.choices.features)
        return pages + ["review", "result"]

    def missing(self, labels):
        existing = self.probes.existing_labels
        return [] if existing is None else [label for label in labels if label.name.casefold() not in existing]

    def remote_labels(self):
        selected = []
        if self.choices.workflow_labels:
            selected.extend(self.missing(configured_labels(self.plan.config)))
        if self.choices.create_priority_labels:
            selected.extend(self.missing(priority_labels(self.choices)))
        unique = {}
        for label in selected:
            unique.setdefault(label.name.casefold(), label)
        return list(unique.values())

    def page(self):
        c = self.choices
        if self.current == "welcome":
            return Page("Welcome to ub-agents", "Let's set up your project's GitHub loop.\n\n"
                        "Four roles prepare issues, implement changes, review PRs and integrate approved work. "
                        "Your repository owns the configuration, checks and authority.\n\n"
                        f"Installed version: {__version__}\n\n" +
                        (findings(self.probes.welcome) if self.started else "Checking the latest version and launch prerequisites...") +
                        "\n\nWe'll collect choices, show every change, then ask before applying. "
                        "Nothing is written until Apply. Cancel is safe before then.")
        if self.current == "prerequisites":
            return Page("Prerequisites", f"Target repository: {self.plan.config.repository}\n\n"
                        "These findings describe launch prerequisites. You can finish setup and repair them later.\n\n" +
                        findings(self.probes.prerequisites + self.probes.runtime) +
                        "\n\nNo software is installed, accounts changed or permissions altered by these checks.")
        if self.current == "checkout":
            return Page("Your checkout", findings(self.probes.checkout) + "\n\n"
                        "Launch needs a clean default-branch control checkout without ahead or divergent local commits. "
                        "Before each run it fetches origin and fast-forwards to pick up code, configuration and policy.\n\n"
                        "Agents work in private worktrees and branches. Changes go through PRs; agents never push directly "
                        "to the default branch.\n\n"
                        "If you also code here, use a separate launcher checkout. Setup on a branch is fine. "
                        "Init never switches, resets, stashes or clones a checkout.")
        if self.current == "policy":
            return Page("Runtime and project policy", "Choose one starter runtime for all four roles (cli:model:effort).\n\n" +
                        "\n\n".join(guidance_summary(self.plan)) +
                        "\n\nShared policy: .agents/ub_agents.md. Role files: " + ", ".join(ROLES) + ".\n\n"
                        "Existing check guidance is referenced; otherwise the shared policy contains build/test placeholders. "
                        "Finding guidance does not establish launch readiness.\n\n"
                        "Before launch, the project owners must review build/test commands, merge authority, "
                        "decision-makers and review priorities. Continuing this screen does not approve that policy.\n\n"
                        "Public repositories require maintain/admin approval of issue starts and outside edits by default; "
                        "private repositories default to approvals off. Review docs/approvals before launch.",
                        (Field("runtime", "Starter runtime", c.runtime),))
        if self.current == "permissions":
            cli = c.runtime.split(":", 1)[0]
            return Page("Runtime permissions", findings(self.probes.runtime) + "\n\n" +
                        ("Codex starter permissions grant full access without the sandbox."
                         if cli == "codex" else "Claude starter permissions grant unattended edits plus git, gh and report commands. "
                         "The project's check commands must still be added to --allowedTools before launch.") +
                        "\n\nExact starter arguments:\n" + json.dumps(permission_args(cli), indent=2) +
                        "\n\nThese apply to all four roles. You can defer and edit runtime-args later. "
                        "They are never enabled automatically. Existing local overrides still take precedence.",
                        (Field("permissions", "Enable starter permissions for all four agents", c.permissions),), True)
        if self.current == "files":
            labels = configured_labels(self.plan.config)
            missing = self.missing(labels)
            text = ("Create these starter files:\n" + "\n".join(str(p) for p in self.plan.targets) +
                   "\n\nAdd missing .gitignore entries: .ub-agents/ and ub-agents.local.yaml. "
                   "Keep existing entries and AGENTS.md / CLAUDE.md unchanged. Existing starter paths are never overwritten.\n\n")
            local = self.plan.path.parent / LOCAL_CONFIG
            if local.exists():
                text += f"Existing {local} stays unchanged and overrides starter settings. Review its content:\n{local.read_text()}\n\n"
            if self.probes.existing_labels is None:
                text += "UNAVAILABLE: GitHub labels could not be read. Creation is deferred; result includes commands."
            elif not missing:
                text += "All configured workflow labels exist; existing labels stay unchanged."
            else:
                text += "Missing workflow labels:\n" + "\n".join(f"{label.name}: {label.explanation}" for label in missing)
            fields = (Field("workflow_labels", "Create missing workflow labels (default no)", c.workflow_labels),) if missing else ()
            return Page("Files and workflow labels", text, fields)
        if self.current == "features":
            return Page("Optional features", "Choose only the features you want to look at; every feature can be skipped.\n\n"
                        "Priority: order work by labels. Without configuration all items have equal priority.\n\n"
                        "Retrospectives: chosen roles may read existing discussion boards and publish public reflections.\n\n"
                        "Uberblick shared knowledge: recommended as an optional follow-up. It is never a runtime dependency.",
                        tuple(Field(name, label, name in c.features) for name, label in
                              (("priority", "Set queue priorities"), ("retrospectives", "Configure retrospective boards"),
                               ("knowledge", "Learn about Uberblick shared knowledge"))), True)
        if self.current == "priority":
            return Page("Queue priority", "Enter unique labels, separated by commas, highest priority first. "
                        "The default must be one of those labels. Leave default empty to explicitly choose no default; "
                        "unlabeled items then rank below configured labels.\n\n"
                        "Skip leaves no priority configuration, so all items have equal priority. "
                        "Missing label creation is a separate choice.",
                        (Field("labels", "Priority labels (highest to lowest)", ", ".join(c.priority_labels)),
                         Field("default", "Default label (empty means no default)", c.priority_default or "")), True)
        if self.current == "priority-labels":
            return Page("Missing priority labels", "Create only these missing labels, leaving existing labels unchanged:\n\n" +
                        "\n".join(label.command(self.plan.config.repository) for label in self.missing(priority_labels(c))) +
                        "\n\nDecline or Skip to configure priority without creating labels; result includes the commands.",
                        (Field("create_priority_labels", "Create missing priority labels (default no)", c.create_priority_labels),), True)
        if self.current == "retrospectives":
            return Page("Retrospective boards", f"Choose existing discussions in {self.plan.config.repository}. "
                        "Enter a positive integer per role; leave empty to keep that role's posting off.\n\n"
                        "We verify each exact repository URL and read access. Posting access remains UNVERIFIED: "
                        "no test comments are posted. Ensure the token can read discussions and post discussion comments "
                        "(fine-grained Discussions read/write; classic tokens need repository access). "
                        "Later agents publish publicly as the launcher account. For Claude, separately allow "
                        "Bash({report_command} retrospective *) on selected roles.\n\n"
                        "Create boards yourself: https://docs.github.com/en/discussions/managing-discussions-for-your-community/creating-a-discussion\n\n"
                        "No discussions, accounts or workspaces are created. Invalid or unreadable destinations cannot be enabled: fix and retry, or Skip.",
                        tuple(Field(role, f"{role} discussion number", str(c.retrospectives.get(role, ""))) for role in ROLES), True)
        if self.current == "knowledge":
            return Page("Uberblick shared knowledge", "Uberblick can share project guidance and decisions among agents. "
                        "It is a recommended optional follow-up, never needed to run this standalone framework.\n\n"
                        "Learn more: https://github.com/uberblick-ai/uberblick-2\n\n"
                        "Follow its setup guidance separately. This wizard creates no workspace or account, requests no secrets "
                        "and enables no integration.", optional=True)
        if self.current == "review":
            remote = self.remote_labels()
            text = ("Review the exact changes below. Apply defaults to no. Back edits choices; Cancel leaves everything untouched.\n\n" +
                   self.option_summary() + "\n\n" + self.effective_summary() + "\n\n" +
                   self.plan.preview() + "\n\nOpted-in remote changes:\n" +
                   ("\n".join(label.command(self.plan.config.repository) for label in remote) or "None") +
                   "\n\nNo launch, commit, push, merge, installation, account change or protection change is performed. "
                   "Setup success does not mean launch readiness.")
            return Page("Review and apply", text, (Field("confirm", "Apply these exact local and remote changes (default no)", False),))
        return Page("Setup results", self.result_text())

    def option_summary(self):
        c = self.choices
        return (f"Runtime: {c.runtime}\nPermissions: {'enabled' if c.permissions else 'deferred'}\n"
                f"Workflow label creation: {'selected' if c.workflow_labels else 'deferred'}\n"
                f"Priority: {', '.join(c.priority_labels) or 'skipped; all items equal'}; default: {c.priority_default or 'none'}\n"
                f"Priority label creation: {'selected' if c.create_priority_labels else 'deferred'}\n"
                "Retrospectives: " + (", ".join(f"{role}: #{number} (read verified; posting UNVERIFIED)"
                                               for role, number in c.retrospectives.items()) or "skipped; posting off") +
                f"\nUberblick: {'optional follow-up' if 'knowledge' in c.features else 'skipped'}; no integration enabled")

    def effective_summary(self):
        config = self.plan.config
        lines = ["Effective configuration (including existing local overrides):"]
        for agent in config.agents:
            runtimes = ", ".join(r.name for r in agent.runtimes) or "command: " + json.dumps(agent.command)
            lines.append(f"{agent.name}: {runtimes}")
            for runtime in agent.runtimes:
                lines.append(f"  {runtime.cli} arguments: {json.dumps(agent.runtime_args_for(runtime))}")
            lines.append(f"  retrospective: #{agent.retrospectives} (posting UNVERIFIED)" if agent.retrospectives else
                         "  retrospective: off")
        lines.append(f"Priority: {', '.join(config.queue.priority.labels) or 'off'}; default: {config.queue.priority.default or 'none'}")
        return "\n".join(lines)

    def advance(self, values):
        if self.current != "review":
            self.plan.protect()
        c = self.choices
        if self.current == "welcome" and not self.started:
            self.probe()
        elif self.current == "policy":
            runtime = values["runtime"].strip()
            # Build first, so invalid input cannot corrupt the last valid choices.
            from dataclasses import replace
            proposed = make_plan(self.plan.path, self.plan.repository, replace(c, runtime=runtime, permissions=False))
            if runtime != c.runtime:
                c.runtime, c.permissions = runtime, False
                self.plan = proposed
                self.probes.check_runtime(self.plan.config)
        elif self.current in {"permissions", "files", "priority-labels"}:
            for field in self.page().fields:
                setattr(c, field.name, values[field.name])
        elif self.current == "features":
            c.features = {name for name in ("priority", "retrospectives", "knowledge") if values[name]}
            if "priority" not in c.features:
                c.priority_labels, c.priority_default, c.create_priority_labels = (), None, False
            if "retrospectives" not in c.features:
                c.retrospectives.clear()
        elif self.current == "priority":
            labels = tuple(label.strip() for label in values["labels"].split(","))
            if not labels or any(not label or "\x00" in label for label in labels):
                raise AgentError("Enter at least one nonempty priority label, or Skip")
            if len({label.casefold() for label in labels}) != len(labels):
                raise AgentError("Priority labels must be unique")
            default = values["default"].strip() or None
            if default is not None and default not in labels:
                raise AgentError("Default must be one of the configured priority labels, or empty for no default")
            c.priority_labels, c.priority_default = labels, default
            if self.probes.existing_labels is None:
                c.create_priority_labels = False
        elif self.current == "retrospectives":
            selected = {}
            for role in ROLES:
                value = values[role].strip()
                if value:
                    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                        raise AgentError(f"{role}: enter a positive integer, leave empty, or Skip")
                    try:
                        self.probes.discussion(int(value))
                    except PROBE_ERRORS:
                        raise AgentError(f"{role}: discussion #{value} is invalid or unreadable in {self.plan.config.repository}; fix/retry or Skip") from None
                    selected[role] = int(value)
            c.retrospectives = selected
        elif self.current == "review":
            if not values.get("confirm"):
                raise AgentError("Apply defaults to no; explicitly select Apply to make changes")
            # Destinations must still be readable at the point we enable posting.
            for number in {a.retrospectives for a in self.plan.config.agents if a.retrospectives is not None}:
                try:
                    self.probes.discussion(number)
                except PROBE_ERRORS:
                    raise AgentError(f"Discussion #{number} can no longer be verified; go Back to fix or Skip retrospectives") from None
            self.applied = apply_plan(self.plan, self.github, self.remote_labels())
            self.current = "result"
            return
        self.plan = make_plan(self.plan.path, self.plan.repository, c)
        pages = self.pages()
        self.current = pages[pages.index(self.current) + 1]

    def back(self):
        if self.current not in {"welcome", "result"}:
            pages = self.pages()
            self.current = pages[pages.index(self.current) - 1]

    def skip(self):
        name = self.current
        if not self.page().optional:
            return
        if name == "permissions":
            self.choices.permissions = False
        elif name == "features":
            self.choices.features.clear()
            self.choices.priority_labels, self.choices.priority_default = (), None
            self.choices.create_priority_labels = False
            self.choices.retrospectives.clear()
        elif name == "priority-labels":
            self.choices.create_priority_labels = False
        elif name == "priority":
            self.choices.priority_labels, self.choices.priority_default = (), None
            self.choices.create_priority_labels = False
            self.choices.features.discard(name)
        elif name == "retrospectives":
            self.choices.retrospectives.clear()
            self.choices.features.discard(name)
        elif name == "knowledge":
            self.choices.features.discard(name)
        old = self.current
        # The skipped optional page may disappear; use its successor from the old ordering.
        if old == "priority":
            self.current = next((p for p in ("retrospectives", "knowledge") if p in self.choices.features), "review")
        elif old == "retrospectives":
            self.current = "knowledge" if "knowledge" in self.choices.features else "review"
        elif old == "knowledge":
            self.current = "review"
        else:
            self.current = self.pages()[self.pages().index(old) + 1]
        self.plan = make_plan(self.plan.path, self.plan.repository, self.choices)

    def deferred_commands(self):
        labels = configured_labels(self.plan.config) + priority_labels(self.choices)
        done = {name.casefold() for name in self.applied.label_names} if self.applied else set()
        if self.probes.existing_labels is not None:
            labels = self.missing(labels)
        return [label.command(self.plan.config.repository) for label in labels if label.name.casefold() not in done]

    def result_text(self):
        if self.cancelled or self.applied is None:
            return "Setup cancelled. No files, directories or GitHub labels were changed."
        result = self.applied
        lines = ["Setup completed; launch readiness still requires project review." if result.ok else
                 "Setup partially failed. Completed effects remain; launch readiness has not been established."]
        lines.extend(result.completed)
        if result.error:
            lines.extend([result.error, *result.incomplete, "Remaining work:", *result.remaining,
                          "Inspect completed files (and any partial file from the failed write). Repair access and complete missing files manually from the recovery content below. "
                          "Init refuses existing starter files; do not rerun it expecting overwrites."])
            lines.append(recovery_content(self.plan, result))
        lines.extend([self.option_summary(), self.effective_summary(), *guidance_summary(self.plan), "Before launch:", *next_steps(self.plan)])
        attention = [check for check in (self.probes.welcome + self.probes.prerequisites +
                                         self.probes.checkout + self.probes.runtime) if check.status != "ok"]
        if attention:
            lines.extend(["Pre-apply findings needing attention (run doctor again after committing):", findings(attention)])
        commands = self.deferred_commands()
        if commands:
            lines.extend(["Deferred labels; run these commands yourself (skip any that already exist):", *commands])
        return "\n\n".join(lines)
