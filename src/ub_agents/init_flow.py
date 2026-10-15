"""`ub-agents init`: a short Clack-style flow over the read-only plan and one explicit write."""

import re

from rich.text import Text

from . import __version__
from .doctor import public
from .errors import AgentError
from .execution import git
from .github import REPOSITORY
from .init_boards import Boards
from .init_probes import ACCOUNT_GUIDANCE, PROBE_ERRORS, Probes
from .init_prompts import DIM, GREEN, RED, YELLOW, Cancelled, Option
from .init_setup import (PERMISSIONS_GUIDE, ROLES, Choices, apply_plan, guidance_summary, infer_repository, make_plan, next_steps,
                         priority_labels, recovery_content)
from .labels import configured_labels

RUNTIMES = {"claude": ("Claude Code", "claude:claude-opus-5-5:high"), "codex": ("Codex", "codex:gpt-6.1-sol:high")}
PRIORITIES = "prio:urgent, prio:high, prio:medium, prio:low"
WORKFLOW_GUIDE = "https://agents.uberblick.ai/docs/configuration/"
RUNTIMES_GUIDE = "https://agents.uberblick.ai/docs/configuration/runtimes"
RETROSPECTIVES_GUIDE = "https://agents.uberblick.ai/docs/best-practices/retrospectives"
SAFETY = "Agents always work in worktrees and branches; they never commit to main."
UBERBLICK = "https://uberblick.ai"
MARKS = {"ok": ("✓", GREEN), "warn": ("!", YELLOW), "fail": ("✗", RED)}


def board_title(role):
    return f"{role.capitalize()} retrospectives"


def check_repository(value):
    return None if re.fullmatch(REPOSITORY, value) else "Enter owner/name"


def check_priorities(value):
    labels = [label.strip() for label in value.split(",")]
    if not value.strip() or any(not label for label in labels):
        return "Enter labels separated by commas"
    if len({label.casefold() for label in labels}) != len(labels):
        return "Labels must be unique"
    return None


class Flow:
    def __init__(self, path, repository, runtime, github_factory, prompter, transcript, *, unattended):
        self.path, self.repository, self.runtime = path, repository, runtime
        self.github_factory, self.ask, self.t = github_factory, prompter, transcript
        self.unattended = unattended
        self.github = self.probes = self.doctor = None

    def run(self):
        self.t.intro("ub-agents init", f"v{__version__}")
        # Refuse existing starter files before any network read.
        make_plan(self.path, self.repository or "owner/name", Choices(self.runtime or RUNTIMES["codex"][1]))
        try:
            return self.steps()
        except (Cancelled, KeyboardInterrupt) as exc:
            self.t.cancelled(str(exc) or "Setup")
            return 1

    def steps(self):
        if self.unattended:
            self.repository = self.repository or infer_repository(self.path.parent)
            self.github = self.github_factory(self.repository)
            self.t.step("Checks skipped", [Text("Unattended setup; run ub-agents doctor before launch", style=DIM)])
        else:
            self.check()
        choices = Choices(self.choose_runtime())
        choices.permissions = self.choose_permissions(choices.runtime)
        extras = self.choose_extras()
        if "priority" in extras:
            value = self.ask.text("Priority labels, highest first", PRIORITIES, check_priorities)
            self.t.answered("Priority labels, highest first", value)
            choices.priority_labels = tuple(label.strip() for label in value.split(","))
        if "retrospectives" in extras:
            boards, repo = Boards(self.github, self.repository), self.probes.discussions
            names = [c["name"] for c in repo["discussionCategories"]["nodes"]]
            category = "General" if "General" in names else names[0]
            choices.board_requests = {role: boards.request(board_title(role), category, repo) for role in ROLES}
        plan = make_plan(self.path, self.repository, choices)
        labels = self.choose_labels(plan)
        self.confirm_write(plan, labels)
        with self.t.spinner("Writing setup"):
            result = apply_plan(plan, self.github, labels)
        if not result.ok:
            return self.failed(plan, result, labels)
        boards = ", ".join(f"#{n}" for n in result.board_numbers.values())
        self.t.step("Setup written" + (f" · boards {boards}" if boards else ""))
        self.finish(plan, labels, extras)
        return 0

    def check(self):
        with self.t.spinner("Checking machine, GitHub, checkout and agents"):
            if not self.repository:
                try:
                    self.repository = infer_repository(self.path.parent)
                except AgentError:
                    pass
            if self.repository:
                self.probe()
        if not self.repository:
            self.repository = self.ask.text("Which repository?", "", check_repository)
            self.t.answered("Which repository?", self.repository)
            with self.t.spinner("Checking machine, GitHub, checkout and agents"):
                self.probe()
        lines = []
        for group in self.probes.groups:
            mark, color = MARKS[group.status]
            if not group.problems:
                lines.append(Text.assemble((f"{mark} ", color), f"{group.name:<11}", (" · ".join(group.summary), DIM)))
                continue
            for index, problem in enumerate(group.problems):
                lines.append(Text.assemble((f"{mark} ", color) if not index else "  ",
                                           f"{group.name if not index else '':<11}", problem.message))
                if problem.remedy and problem.remedy not in {p.remedy for p in group.problems[index + 1:]}:
                    lines.append(Text.assemble(" " * 13, (problem.remedy, DIM)))
        if any(group.status == "fail" for group in self.probes.groups):
            self.t.step("Checks · fix these before launch; setup can continue", lines, "▲", YELLOW)
        else:
            self.t.step("Checks", lines)
        if self.probes.clean is False:
            raise Cancelled("This checkout has local changes; commit or stash them, then run ub-agents init again")
        self.choose_branch()

    def choose_branch(self):
        branch, default = self.probes.branch, self.probes.default_branch
        if not branch or not default or branch == default:
            return
        message = f"Switch this checkout to {default}?"
        note = [f"ub-agents only uses {default} to start fresh branches and worktrees for agents.",
                SAFETY.replace("main", default), f"See {PERMISSIONS_GUIDE}"]
        if not self.ask.confirm(message, True, note):
            self.t.answered(message, f"No · switch to {default} before you launch")
            return
        try:
            git(self.probes.plan.config.root, "switch", default)
        except AgentError as exc:
            reason = str(exc).removeprefix("Git operation failed: ").splitlines()[-1:] or [""]
            self.t.step(message, [Text(f"Not switched: {reason[0]}", style=RED),
                                  Text(f"Switch to {default} before you launch", style=DIM)], "▲", YELLOW)
            return
        self.t.step(message, [Text(f"Yes · now on {default}", style=DIM), Text(SAFETY.replace("main", default), style=DIM)])

    def probe(self):
        self.github = self.github_factory(self.repository)
        plan = make_plan(self.path, self.repository, Choices(self.runtime or RUNTIMES["codex"][1]))
        self.probes = Probes(plan, self.github, self.doctor).run()

    def choose_runtime(self):
        preferred = self.runtime.split(":", 1)[0] if self.runtime else None
        cli = self.probes.runtime(preferred if preferred in RUNTIMES else None) if self.probes else preferred or "codex"
        clis = list(RUNTIMES)
        if preferred and preferred not in RUNTIMES:
            clis.append(preferred)
        values = {name: self.runtime if self.runtime and name == preferred else RUNTIMES[name][1] for name in clis}
        options = []
        for name in clis:
            _, model, effort = (values[name].split(":") + ["", ""])[:3]
            state = self.probes.clis.get(name, "not installed") if self.probes else ""
            hint = " · ".join(part for part in (model, effort, state if state == "not installed" else "") if part)
            options.append(Option(name, RUNTIMES.get(name, (name,))[0], hint))
        message = "Which agent runs the loop?"
        cli = self.ask.select(message, options, clis.index(cli), "Model and effort can be changed later in ub-agents.yaml.")
        option = next(o for o in options if o.value == cli)
        self.t.step(message, [Text(f"{option.label} · {option.hint}" if option.hint else option.label, style=DIM),
                              Text(f"Great, {option.label} is the default for all agents.", style=DIM),
                              Text(f"Review later; see {RUNTIMES_GUIDE}", style=DIM)])
        return values[cli]

    def choose_permissions(self, runtime):
        cli = runtime.split(":", 1)[0]
        grant = ("full access, no sandbox" if cli == "codex" else
                 "edits, git, gh and report commands without asking")
        message = "Let agents work without asking?"
        options = [Option(True, "Yes", grant), Option(False, "No", "set runtime-args later")]
        # Unattended runs keep permissions off: nobody saw what they grant.
        enabled = self.ask.select(message, options, 1 if self.unattended else 0)
        answer = f"Yes · review runtime-args in {self.path.name} before you launch" if enabled else "No"
        self.t.answered(message, answer)
        return enabled

    def choose_extras(self):
        available = self.probes and self.probes.discussions
        note = self.probes.discussions_note if self.probes else "unattended"
        options = [Option("priority", "Priorities", "order the queue by labels"),
                   Option("retrospectives", "Retrospectives", "agents post learnings per run; used to optimize delivery",
                          "" if available else note),
                   Option("uberblick", "Uberblick", "shared knowledge for agents and humans")]
        extras = self.ask.multiselect("Extras", options)
        self.t.answered("Extras", ", ".join(o.label for o in options if o.value in extras) or "None")
        return extras

    def choose_labels(self, plan):
        existing = self.probes.existing_labels if self.probes else None
        if existing is None:
            return []
        missing = {}
        groups = {"workflow": configured_labels(plan.config), "priority": priority_labels(plan.choices)}
        for group, labels in groups.items():
            for label in labels:
                if label.name.casefold() not in existing:
                    missing.setdefault(label.name.casefold(), (group, label))
        if not missing:
            return []
        names = {group: ", ".join(label.name for g, label in missing.values() if g == group) for group in groups}
        note = []
        if names["workflow"]:
            note += [f"Workflow:   {names['workflow']}", f"            trigger the loop; see {WORKFLOW_GUIDE}"]
        if names["priority"]:
            note.append(f"Priorities: {names['priority']}")
        message = f"Create {len(missing)} missing label{'s' if len(missing) > 1 else ''}?"
        create = self.ask.confirm(message, True, note)
        self.t.answered(message, "Yes" if create else "No")
        return [label for _, label in missing.values()] if create else []

    def confirm_write(self, plan, labels):
        root = plan.path.parent
        lines = [Text.assemble(("+ ", GREEN), str(plan.path.relative_to(root))),
                 Text.assemble(("+ ", GREEN), ".agents/ ", (f"ub_agents.md and {len(ROLES)} role files", DIM))]
        if plan.ignore_after != (plan.ignore_before or b""):
            added = len(plan.ignore_after.splitlines()) - len((plan.ignore_before or b"").splitlines())
            lines.append(Text.assemble(("~ ", YELLOW), ".gitignore ", (f"+{added} lines", DIM)))
        if labels:
            lines.append(Text.assemble(("+ ", GREEN), f"{len(labels)} labels on {self.repository}"))
        if plan.choices.board_requests:
            lines.append(Text.assemble(("+ ", GREEN), f"{len(plan.choices.board_requests)} retrospective boards",
                                       (" (reused when the title exists)", DIM)))
        self.t.note("Changes", lines)
        if not self.ask.confirm("Write setup?", True):
            raise Cancelled("Write setup?")
        self.t.answered("Write setup?", "Yes")

    def finish(self, plan, labels, extras):
        steps = next_steps(plan) + guidance_summary(plan)
        account = next((g for g in self.probes.groups if g.name == "Account"), None) if self.probes else None
        if account:
            steps.append(f"Use a dedicated agent account with write access; see {ACCOUNT_GUIDANCE}")
        if "uberblick" in extras:
            steps.append(f"Uberblick, shared knowledge for agents and humans: {UBERBLICK}")
        if "retrospectives" not in extras:
            steps.append(f"Want retrospectives later? Turn on Discussions if needed, then follow {RETROSPECTIVES_GUIDE}")
        self.t.note("Next", [f"{index}. {step}" for index, step in enumerate(steps, 1)])
        created = {label.name.casefold() for label in labels}
        pending = [label for label in configured_labels(plan.config) + priority_labels(plan.choices)
                   if label.name.casefold() not in created and
                   (self.probes is None or self.probes.existing_labels is None or
                    label.name.casefold() not in self.probes.existing_labels)]
        if pending:
            unique = {label.name.casefold(): label for label in pending}.values()
            self.t.step("Create the labels yourself", [label.command(self.repository) for label in unique])
        self.t.outro("Then run ", ("ub-agents launch", "cyan"))

    def failed(self, plan, result, labels):
        created = {name.casefold() for name in result.label_names}
        lines = [*result.completed, Text(public(result.error), style=RED), *result.incomplete,
                 *(Text(f"Not done: {step}", style=DIM) for step in result.remaining),
                 *(label.command(self.repository) for label in labels if label.name.casefold() not in created)]
        self.t.step("Setup partly written", lines, "▲", YELLOW)
        content = recovery_content(plan, result)
        if content:
            self.t.console.print(content, markup=False, highlight=False)
        self.t.outro("Finish the rest by hand; init never overwrites existing files.")
        return 1
