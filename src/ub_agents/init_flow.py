"""`ub-agents init`: a short Clack-style flow over the read-only plan and one explicit write."""

import json
import re
import subprocess

from rich.text import Text

from . import __version__
from .doctor import public
from .errors import AgentError
from .execution import git
from .github import REPOSITORY
from .init_boards import Boards
from .init_mcp import preview as mcp_preview
from .init_probes import ACCOUNT_GUIDANCE, PROBE_ERRORS, Probes
from .init_prompts import DIM, GREEN, RED, YELLOW, Cancelled, Option
from .init_setup import (PERMISSIONS_GUIDE, ROLES, Choices, apply_plan, guidance_summary, infer_repository, make_plan, next_steps,
                         priority_labels, recovery_content)
from .labels import configured_labels

RUNTIMES = {"claude": ("Claude Code", "claude:claude-opus-5-5:high"), "codex": ("Codex", "codex:gpt-6.1-sol:high")}
PRIORITIES = "prio:urgent, prio:high, prio:medium, prio:low"
WORKFLOW_GUIDE = "https://agents.uberblick.ai/docs/configuration/"
UPDATES_GUIDE = "https://agents.uberblick.ai/docs/configuration/runtime-updates"
RUNTIMES_GUIDE = "https://agents.uberblick.ai/docs/configuration/runtimes"
APPROVALS_GUIDE = "https://agents.uberblick.ai/docs/configuration/approvals"
SETUP_BRANCH = "setup-ub-agents"
PR_TITLE = "Set up ub-agents"
PR_BODY = ("Starter configuration, agent instructions and .gitignore entries written by `ub-agents init`.\n\n"
           "Review the agent permissions (runtime-args) in ub-agents.yaml and the checks and merge authority in "
           ".agents/ub_agents.md, then merge. Agents always work in worktrees and branches; they never commit to main.")
SAFETY = "Agents always work in worktrees and branches; they never commit to main."
UBERBLICK = "https://uberblick.ai"
UB_INSTALL = "brew install uberblick-ai/tap/uberblick"
WORKSPACE_FILE = ".uberblick.json"
MARKS = {"ok": ("✓", GREEN), "warn": ("!", YELLOW), "fail": ("✗", RED)}


def run(root, *command):
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120,
                                check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError(f"{command[0]} failed: {exc}") from exc
    if result.returncode:
        raise AgentError(result.stderr.strip() or f"{command[0]} failed")
    return result.stdout.strip()


def workspace_binding(root):
    """The nearest .uberblick.json, where ub looks for it."""
    for directory in (root, *root.parents):
        path = directory / WORKSPACE_FILE
        if path.exists() or path.is_symlink():
            return path
    return None


def board_title(role):
    return f"{role.capitalize()} retrospectives"


def check_repository(value):
    return None if re.fullmatch(REPOSITORY, value) else "Enter owner/name"


def check_owner(value):
    return None if re.fullmatch(r"@[A-Za-z0-9-]+(/[A-Za-z0-9._-]+)?", value) else "Enter @user or @org/team"


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
        self.on_default = False
        self.workspace = None
        self.mcp = None

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
            self.t.step("Checks skipped", [Text("Unattended setup", style=DIM)])
        elif self.check() is False:
            return 1
        choices = Choices(self.choose_runtime())
        choices.permissions = self.choose_permissions(choices.runtime)
        choices.updates = self.choose_updates(choices.runtime)
        choices.owner = self.choose_owner()
        extras = self.choose_extras()
        if "priority" in extras:
            value = self.ask.text("Priority labels, highest first", PRIORITIES, check_priorities)
            self.t.answered("Priority labels, highest first", value)
            choices.priority_labels = tuple(label.strip() for label in value.split(","))
        if "uberblick" in extras:
            self.choose_workspace()
            self.mcp = mcp_preview(self.path.parent, choices.runtime.split(":", 1)[0], for_pr=self.on_default)
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
        extra = self.create_workspace() if self.workspace == "create" else []
        if self.mcp:
            with self.t.spinner("Registering the Uberblick MCP server"):
                extra += self.mcp.apply()
        url = self.open_pr(plan, extra) if self.on_default else None
        self.finish(plan, labels, extras, url)
        return 0

    def check(self):
        """Return False when the branch choice stops setup before files are written."""
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
        return self.choose_branch()

    def choose_branch(self):
        branch, default = self.probes.branch, self.probes.default_branch
        self.on_default = bool(default) and branch == default
        if not branch or not default or branch == default:
            return
        message = f"Switch this checkout to {default}?"
        note = [f"ub-agents only uses {default} to start fresh branches and worktrees for agents.",
                SAFETY.replace("main", default), f"See {PERMISSIONS_GUIDE}"]
        if self.ask.confirm(message, True, note):
            try:
                git(self.probes.plan.config.root, "switch", default)
            except AgentError as exc:
                reason = str(exc).removeprefix("Git operation failed: ")
                lines = [Text(line, style=RED) for line in f"Not switched: {reason}".splitlines()]
            else:
                self.on_default = True
                self.t.step(message, [Text(f"Yes · now on {default}", style=DIM),
                                      Text(SAFETY.replace("main", default), style=DIM)])
                return
        else:
            lines = [Text(f"No · init needs {default} to open the setup PR", style=DIM)]
        self.t.step(message, [*lines, Text(f"Switch to {default} and run ub-agents init again", style=DIM)], "▲", YELLOW)
        return False

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

    def choose_updates(self, runtime):
        cli = runtime.split(":", 1)[0]
        message = f"Keep {cli} and gh up to date?"
        note = ["Once a day between runs, each with its own updater (Homebrew, npm or the native installer).",
                f"See {UPDATES_GUIDE}"]
        # Unattended runs leave updates off, like permissions.
        enabled = self.ask.confirm(message, not self.unattended, note)
        self.t.answered(message, f"Yes · {cli} and gh update once a day between runs" if enabled else "No")
        return enabled

    def choose_owner(self):
        if self.unattended:
            return self.choose_unattended_owner()
        if not self.probes:
            return ""
        approvals = f"{self.probes.approvals}; see {APPROVALS_GUIDE}"
        if not self.probes.actor:
            self.t.step("Policy", [Text(approvals, style=DIM)])
            return ""
        message = "Who answers the agents' questions?"
        handles = [*self.probes.teams, f"@{self.probes.actor}"]
        options = [Option(handle, handle, "you" if handle == handles[-1] else "maintains this repository")
                   for handle in handles] + [Option("", "Someone else")]
        note = ["They decide scope and policy questions and own workflow changes; see .agents/ub_agents.md.", approvals]
        owner = self.ask.select(message, options, 0, note)
        if not owner:
            owner = self.ask.text("Which @user or @org/team?", handles[0], check_owner)
        self.t.step(message, [Text(owner, style=DIM), Text(approvals, style=DIM)])
        return owner

    def choose_unattended_owner(self):
        try:
            teams = self.github.teams()
        except PROBE_ERRORS:
            teams = []
        if teams:
            owner, source = teams[0], "first maintaining team"
        else:
            try:
                actor = self.github.actor()
            except PROBE_ERRORS:
                actor = None
            owner, source = f"@{actor}" if actor else "", "current gh user"
        line = (f"{owner} · {source}; edit .agents/ub_agents.md to change it" if owner else
                "Replace @org/maintainers in .agents/ub_agents.md before launch")
        self.t.step("Who answers the agents' questions?", [Text(line, style=DIM)])
        return owner

    def choose_extras(self):
        available = self.probes and self.probes.discussions
        note = self.probes.discussions_note if self.probes else "unattended"
        options = [Option("priority", "Priorities", "order the queue by labels"),
                   Option("retrospectives", "Retrospectives", "agents post learnings per run; used to optimize delivery",
                          "" if available else note),
                   Option("uberblick", "Uberblick", "shared knowledge for agents and humans",
                          "" if self.probes and self.probes.ub else f"needs ub: {UB_INSTALL}")]
        extras = self.ask.multiselect("Extras", options)
        self.t.answered("Extras", ", ".join(o.label for o in options if o.value in extras) or "None")
        return extras

    def choose_workspace(self):
        root = self.path.parent
        binding = workspace_binding(root)
        if binding:
            where = binding.relative_to(root) if binding.parent == root else binding
            try:
                hub = json.loads(binding.read_text()).get("hubUrl") or "local"
                answer = f"Found {where} · {hub}"
            except (OSError, ValueError, AttributeError):
                answer = f"Found {where}; check it with ub workspace status"
            self.workspace = "found"
            self.t.answered("Uberblick workspace", answer)
            return
        message = "Set up an Uberblick workspace?"
        options = [Option("create", "Create a local workspace",
                          "ub workspace create; " + (f"{WORKSPACE_FILE} goes in the setup PR" if self.on_default else
                                                     f"writes {WORKSPACE_FILE}")),
                   Option("hub", "Use a hub workspace", "sign in, then ub workspace use <link>"),
                   Option("later", "Later")]
        self.workspace = self.ask.select(message, options, 0, f"No {WORKSPACE_FILE} found for this checkout.")
        self.t.answered(message, next(o.label for o in options if o.value == self.workspace))

    def create_workspace(self):
        root = self.path.parent
        try:
            with self.t.spinner("Creating the Uberblick workspace"):
                run(root, "ub", "workspace", "create", self.repository.split("/")[-1])
        except AgentError as exc:
            self.workspace = "failed"
            self.t.step("Uberblick workspace not created", [Text(str(exc).splitlines()[-1], style=RED)], "▲", YELLOW)
            return []
        self.workspace = "created"
        self.t.step("Uberblick workspace created", [Text(WORKSPACE_FILE, style=DIM)])
        return [WORKSPACE_FILE] if (root / WORKSPACE_FILE).exists() else []

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
        if self.workspace == "create":
            lines.append(Text.assemble(("+ ", GREEN), WORKSPACE_FILE, (" local Uberblick workspace", DIM)))
        if self.mcp:
            lines.append(Text(self.mcp.preview))
            if self.mcp.action not in {"add", "reused"}:
                lines.append(Text(f"Manual command: {self.mcp.command}", style=DIM))
        files = "the files go to a new PR." if self.on_default else "the files stay local."
        if labels or plan.choices.board_requests:
            files = "Labels and boards are created on GitHub right away; " + files
        lines += [Text(""), Text(files[0].upper() + files[1:], style=DIM)]
        self.t.note("Changes", lines)
        if not self.ask.confirm("Write setup?", True):
            raise Cancelled("Write setup?")
        self.t.answered("Write setup?", "Yes")

    def open_pr(self, plan, extra=(), *, unique=False, title=PR_TITLE, body=PR_BODY):
        """Commit only init's files on a setup branch, push it and open a PR; the checkout returns to the default branch."""
        root, default = plan.config.root, self.probes.default_branch
        files = [str(path.relative_to(root)) for path in plan.targets]
        if plan.ignore_after != (plan.ignore_before or b""):
            files.append(".gitignore")
        files += extra
        done = []
        try:
            with self.t.spinner("Opening the setup PR"):
                branch = SETUP_BRANCH
                if unique:
                    # Old setup branches are often retained after their PR merges.
                    # Inspect local and remote names without changing either one.
                    local = set(git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/", "refs/remotes/origin/").splitlines())
                    remote = {line.split()[-1].removeprefix("refs/heads/")
                              for line in git(root, "ls-remote", "--heads", "origin").splitlines()}
                    taken = local | {name.removeprefix("origin/") for name in local} | remote
                    suffix = 2
                    while branch in taken:
                        branch = f"{SETUP_BRANCH}-{suffix}"
                        suffix += 1
                git(root, "switch", "--create", branch)
                done.append(f"created {branch}")
                git(root, "add", "--", *files)
                git(root, "commit", "--quiet", "--message", title, "--", *files)
                done.append("committed the files")
                git(root, "push", "--quiet", "--set-upstream", "origin", branch)
                done.append("pushed")
                url = run(root, "gh", "pr", "create", "--repo", self.repository, "--base", default, "--head", branch,
                          "--title", title, "--body", body).splitlines()[-1]
                git(root, "switch", "--quiet", default)
        except AgentError as exc:
            reason = (str(exc).removeprefix("Git operation failed: ").splitlines() or [""])[-1]
            state = f"Init {', '.join(done)}; " if done else ""
            self.t.step("Setup PR not opened", [Text(reason, style=RED),
                                               Text(f"{state}open the PR yourself", style=DIM)], "▲", YELLOW)
            return None
        self.t.step("Setup PR opened", [Text(url, style=DIM), Text(f"Back on {default}", style=DIM)])
        return url

    def finish(self, plan, labels, extras, url=None):
        steps = next_steps(plan) + guidance_summary(plan)
        if url:
            steps[steps.index("Open a PR with these files and merge it")] = f"Review and merge the setup PR: {url}"
            steps[0] = steps[0].replace(f"in {plan.path.name}", f"in {plan.path.name} on the PR")
        account = next((g for g in self.probes.groups if g.name == "Account"), None) if self.probes else None
        if account and any(problem.id == "account" for problem in account.problems):
            steps.append(f"Use a dedicated agent account with write access; see {ACCOUNT_GUIDANCE}")
        if account and any(problem.id == "launcher-listed" for problem in account.problems):
            steps.append("Review ub-agents.local.yaml: list this launcher account before launch")
        if "uberblick" in extras:
            if self.workspace == "hub":
                steps.append(f"Join your team's Uberblick workspace: ub auth login, then ub workspace use <link>; see {UBERBLICK}")
            elif self.workspace in {"later", "failed"}:
                steps.append(f"Bind an Uberblick workspace: ub workspace create <name> or ub workspace use <link>; see {UBERBLICK}")
            outcome = f"{self.mcp.description} · {self.mcp.action}"
            if self.mcp.reason:
                outcome += f" ({self.mcp.reason})"
            steps.append(outcome)
            if self.mcp.action not in {"registered", "reused"} or self.mcp.reason:
                steps.append(f"Manual command: {self.mcp.command}; see {UBERBLICK}")
            steps.append("Restart running agent sessions to pick up the Uberblick MCP server")
        if "retrospectives" not in extras:
            steps.append("Want retrospectives later? Turn on Discussions if needed, then run ub-agents init --create-boards")
        self.t.note("Next", [f"{index}. {step}" for index, step in enumerate(steps, 1)])
        created = {label.name.casefold() for label in labels}
        pending = [label for label in configured_labels(plan.config) + priority_labels(plan.choices)
                   if label.name.casefold() not in created and
                   (self.probes is None or self.probes.existing_labels is None or
                    label.name.casefold() not in self.probes.existing_labels)]
        if pending:
            unique = {label.name.casefold(): label for label in pending}.values()
            self.t.step("Create the labels yourself", [label.command(self.repository) for label in unique])
        self.t.outro(f"After the merge, pull {self.probes.default_branch} and run " if url else "Then run ",
                     ("ub-agents launch", "cyan"))

    def failed(self, plan, result, labels):
        created = {name.casefold() for name in result.label_names}
        lines = [*result.completed, Text(public(result.error), style=RED), *result.incomplete,
                 *(Text(f"Not done: {step}", style=DIM) for step in result.remaining),
                 *(label.command(self.repository) for label in labels if label.name.casefold() not in created)]
        self.t.step("Setup partly written", lines, "▲", YELLOW)
        content = recovery_content(plan, result)
        if content:
            self.t.console.print(content, markup=False, highlight=False)
        self.t.outro("Finish the rest by hand; plain init preserves existing starter files. "
                     "--create-boards and --create-priorities only add missing config entries.")
        return 1
