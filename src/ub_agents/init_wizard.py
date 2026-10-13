"""Wizard pages shared by the terminal view and the readable line fallback."""

from dataclasses import dataclass
import json
import yaml

from . import __version__
from .config import LOCAL_CONFIG
from .errors import AgentError, GitHubError
from .init_probes import Probes, PROBE_ERRORS, findings
from .init_setup import (ROLES, apply_plan, guidance_summary, make_plan, next_steps,
                         permission_args, priority_labels, recovery_content)
from .labels import configured_labels
from .init_boards import Boards, MANUAL_BOARDS


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
        self.option_reviews = {}
        self.board_values = {role: "" for role in ROLES} | {"category": ""}
        self.board_info = "Discussions availability and creation/posting access will be checked when you choose destinations."
        self.board_waiting = False
        self.ready = False
        self.readiness_text = "BLOCKED: launch readiness has not been checked. Fresh starters need review, a PR and merge before launch. Retry readiness after external preparation."
        self.start_loop = False

    def probe(self):
        self.probes.run()
        self.started = True

    def pages(self):
        pages = ["welcome", "prerequisites", "checkout", "policy", "permissions", "files", "features"]
        for name in ("knowledge", "priority", "retrospectives"):
            if name in self.choices.features:
                pages.append(name)
                if name == "priority" and self.missing(priority_labels(self.choices)):
                    pages.append("priority-labels")
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
                        "They are never enabled automatically. Existing local overrides still take precedence.\n\n"
                        "Accept and enable permissions (y) enables these exact arguments. "
                        "Continue without permissions (n) is the default; Cancel (esc) exits without changes.")
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
            return Page("Optional components", "Choose only the components whose setup pages you want to open. "
                        "All are unchecked by default. Selecting a component does not enable it; "
                        "each selected page still defaults to Skip. You can enable this later.\n\n"
                        "Uberblick is a shared knowledge base for agents and humans alike. Learn more at https://uberblick.ai\n\n"
                        "Priorities order eligible work within the queue rules.\n\n"
                        "Retrospectives retain lessons from completed runs through later public agent posts.\n\n"
                        "Continue opens only selected pages, in the order Uberblick, priorities, retrospectives. "
                        "Skip keeps all optional components off; nothing is written until Apply.",
                        (Field("knowledge", "Uberblick shared knowledge", "knowledge" in c.features),
                         Field("priority", "Queue priorities", "priority" in c.features),
                         Field("retrospectives", "Retrospectives", "retrospectives" in c.features)), True)
        if self.current == "priority":
            return Page("Queue priority", "Priorities order eligible work within the queue rules. You can enable this later.\n\n"
                        "Enter unique labels, separated by commas, highest priority first. "
                        "The default must be one of those labels. Leave default empty to explicitly choose no default; "
                        "unlabeled items then rank below configured labels.\n\n"
                        "Skip leaves no priority configuration, so all items have equal priority. "
                        "Missing label creation is a separate choice.\n\n" + self.addition_preview() +
                        "\nContinue checks your entries and shows the proposed YAML; Continue again confirms it. Skip is the default.",
                        (Field("labels", "Priority labels (highest to lowest)", ", ".join(c.priority_labels)),
                         Field("default", "Default label (empty means no default)", c.priority_default or "")), True)
        if self.current == "priority-labels":
            return Page("Missing priority labels", "Labels make the chosen priority ordering usable. You can enable this later.\n\n"
                        "Create only these missing labels, leaving existing labels unchanged:\n\n" +
                        "\n".join(label.command(self.plan.config.repository) for label in self.missing(priority_labels(c))) +
                        "\n\nDecline or Skip to configure priority without creating labels; result includes the commands.",
                        (Field("create_priority_labels", "Create missing priority labels (default no)", c.create_priority_labels),), True)
        if self.current == "retrospectives":
            return Page("Retrospective boards", "Retrospectives retain lessons from completed runs. You can enable this later.\n\n"
                        f"Target repository: {self.plan.config.repository}. Enter an existing positive discussion number, "
                        "or create: <chosen board title> per role. Leave empty to keep that role's posting off. "
                        "For creation, explicitly choose an available category by name. Exact chosen titles are discovered "
                        "and reused to avoid duplicates.\n\n" + self.board_info + "\n\n"
                        "No writes until Apply. Requested public boards are created and verified before configuration is written. "
                        "If manual creation or permission repair is needed, pause here, then Retry, Skip or Cancel. "
                        "No test comments are posted. Existing destinations must report write capability and be open/unlocked. "
                        "GitHub exposes update capability rather than a comment-permission probe; we require that conservative evidence. "
                        "Creation itself verifies token access at Apply; account role alone does not prove token access.\n\n"
                        "Later agents publish publicly as the launcher account. For Claude, separately allow "
                        "Bash({report_command} retrospective *) on selected roles.\n\n"
                        f"Manual instructions: {MANUAL_BOARDS}\n\n" + self.addition_preview() +
                        "\nContinue verifies your choices and previews additions; Continue again confirms them. Skip is the default.",
                        tuple(Field(role, f"{role}: number or create: title", self.board_values[role]) for role in ROLES) +
                        (Field("category", "Category name for requested creation", self.board_values["category"]),), True)
        if self.current == "knowledge":
            return Page("Uberblick shared knowledge", "Uberblick is a shared knowledge base for agents and humans alike. Learn more at https://uberblick.ai\n\n"
                        "You can enable this later. It is a recommended optional follow-up, never needed to run this standalone framework.\n\n"
                        "Follow its setup guidance separately. This wizard creates no workspace or account, requests no secrets "
                        "and enables no integration. Continue keeps the follow-up link; Skip is the default.", optional=True)
        if self.current == "review":
            remote = self.remote_labels()
            text = ("Review the exact changes below. Apply defaults to no. Back edits choices; Cancel leaves everything untouched.\n\n" +
                   self.option_summary() + "\n\n" + self.effective_summary() + "\n\n" +
                   self.plan.preview() + "\n\nOpted-in remote changes:\n" +
                   ("\n".join([*(label.command(self.plan.config.repository) for label in remote),
                                *(request.preview(self.plan.config.repository) for request in c.board_requests.values())]) or "None") +
                   "\n\nBoard numbers are obtained and verified at Apply, before any retrospective config is written. "
                   "No launch, commit, push, merge, installation, account change or protection change is performed by Apply. "
                   "Setup success does not mean launch readiness.")
            return Page("Review and apply", text, (Field("confirm", "Apply these exact local and remote changes (default no)", False),))
        return Page("Setup results", self.result_text() + "\n\nFinish exits. Finish and start loop explicitly checks readiness again, "
                    "then launches only if all gates pass. Retry readiness after preparing the checkout externally. "
                    "Review project checks, merge authority and decision-makers before choosing Start.")

    def addition_preview(self):
        c = self.choices
        additions = {}
        pending = []
        if self.current == "priority" and c.priority_labels:
            priority = {"labels": list(c.priority_labels)}
            if c.priority_default is not None:
                priority["default"] = c.priority_default
            additions = {"queue": {"priority": priority}}
        if self.current == "retrospectives":
            if c.retrospectives:
                additions = {"agents": {role: {"retrospectives": number} for role, number in c.retrospectives.items()}}
            pending = [f"{role}: {request.preview(self.plan.config.repository)}\n"
                       "No destination key is added until Apply returns a verified discussion number."
                       for role, request in c.board_requests.items()]
        return (f"This adds to {self.plan.path}:\n" +
                (yaml.safe_dump(additions, sort_keys=False).rstrip() if additions else "No added keys yet.") +
                ("\n\n" + "\n\n".join(pending) if pending else ""))

    def option_summary(self):
        c = self.choices
        return (f"Runtime: {c.runtime}\nPermissions: {'enabled' if c.permissions else 'deferred'}\n"
                f"Workflow label creation: {'selected' if c.workflow_labels else 'deferred'}\n"
                f"Priority: {', '.join(c.priority_labels) or 'skipped; all items equal'}; default: {c.priority_default or 'none'}\n"
                f"Priority label creation: {'selected' if c.create_priority_labels else 'deferred'}\n"
                "Retrospectives: " + (", ".join([*(f"{role}: #{number} (read and write capability verified)"
                                               for role, number in c.retrospectives.items()),
                                               *(f"{role}: requested board {request.title!r}; number/write access verified at Apply"
                                                 for role, request in c.board_requests.items())]) or "skipped; posting off") +
                f"\nUberblick: {'optional follow-up' if 'knowledge' in c.features else 'skipped'}; no integration enabled")

    def effective_summary(self):
        config = self.plan.config
        lines = ["Effective configuration (including existing local overrides):"]
        for agent in config.agents:
            runtimes = ", ".join(r.name for r in agent.runtimes) or "command: " + json.dumps(agent.command)
            lines.append(f"{agent.name}: {runtimes}")
            for runtime in agent.runtimes:
                lines.append(f"  {runtime.cli} arguments: {json.dumps(agent.runtime_args_for(runtime))}")
            lines.append(f"  retrospective: #{agent.retrospectives} (requires verified write capability)" if agent.retrospectives else
                         "  retrospective: off")
        lines.append(f"Priority: {', '.join(config.queue.priority.labels) or 'off'}; default: {config.queue.priority.default or 'none'}")
        return "\n".join(lines)

    def advance(self, values):
        if self.current not in {"review", "result"}:
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
        elif self.current == "permissions":
            c.permissions = values.get("permissions", False)
        elif self.current in {"files", "priority-labels"}:
            for field in self.page().fields:
                setattr(c, field.name, values[field.name])
        elif self.current == "features":
            selected = {field.name for field in self.page().fields if values.get(field.name, False)}
            for name in ("knowledge", "priority", "retrospectives"):
                if name not in selected:
                    self.clear_feature(name)
            c.features = selected
        elif self.current == "knowledge":
            c.features.add("knowledge")
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
            self.prepare_boards(values)
        elif self.current == "review":
            if not values.get("confirm"):
                raise AgentError("Apply defaults to no; explicitly select Apply to make changes")
            # Destinations must still be readable at the point we enable posting.
            for number in {a.retrospectives for a in self.plan.config.agents if a.retrospectives is not None}:
                try:
                    Boards(self.github, self.plan.config.repository).verify(number)
                except PROBE_ERRORS:
                    raise AgentError(f"Discussion #{number} can no longer be verified; go Back to fix selected destinations. "
                                     f"Check {self.plan.path.parent / LOCAL_CONFIG} for protected local overrides; "
                                     "Cancel and edit those outside init before restarting.") from None
            self.applied = apply_plan(self.plan, self.github, self.remote_labels(), verify_boards=True)
            self.current = "result"
            return
        self.plan = make_plan(self.plan.path, self.plan.repository, c)
        if self.current in {"priority", "retrospectives"}:
            review = ((values.copy(), c.priority_labels, c.priority_default) if self.current == "priority" else
                      (values.copy(), c.retrospectives.copy(), c.board_requests.copy()))
            if self.option_reviews.get(self.current) != review:
                self.option_reviews[self.current] = review
                return
        pages = self.pages()
        self.current = pages[pages.index(self.current) + 1]

    def prepare_boards(self, values):
        self.board_values = {role: values.get(role, "").strip() for role in ROLES} | {"category": values.get("category", "").strip()}
        boards = Boards(self.github, self.plan.config.repository)
        selected, requests = {}, {}
        try:
            repo = boards.setup()
            self.board_info = (f"PASS: Discussions enabled in {self.plan.config.repository}; launcher account role: {repo['viewerPermission']}. "
                               "Available categories: " + ", ".join(c["name"] for c in repo["discussionCategories"]["nodes"]))
            for role in ROLES:
                value = self.board_values[role]
                if not value:
                    continue
                if value.startswith("create:"):
                    title = value[7:].strip()
                    if not title or "\n" in title or "\x00" in title:
                        raise AgentError(f"{role}: enter a nonempty, single-line board title")
                    board = boards.find(title)
                    if board:
                        selected[role] = board["number"]
                    else:
                        requests[role] = boards.request(title, self.board_values["category"], repo)
                elif value.isascii() and value.isdecimal() and int(value) > 0:
                    selected[role] = boards.verify(int(value))["number"]
                else:
                    raise AgentError(f"{role}: enter a positive integer or create: title, leave empty, or Skip")
        except PROBE_ERRORS as exc:
            from .doctor import Doctor
            self.board_waiting = True
            from .doctor import public
            detail = public(exc) if isinstance(exc, AgentError) and not isinstance(exc, GitHubError) else Doctor.github_failure("board verification", exc)
            self.board_info = f"PAUSED: {detail}. Repair access or create the requested boards manually, then Retry; Skip keeps posting off."
            raise AgentError(self.board_info) from None
        self.board_waiting = False
        self.choices.retrospectives, self.choices.board_requests = selected, requests

    def retry_readiness(self):
        if self.applied is None or not self.applied.ok:
            self.ready = False
            self.readiness_text = "BLOCKED: finish setup recovery before starting the loop."
            return False
        from .doctor import Doctor
        from .init_readiness import readiness
        doctor = Doctor(github=self.github, runner=self.probes.doctor.runner, which=self.probes.doctor.which)
        try:
            self.ready, details = readiness(self.plan.path, self.github, doctor)
            self.readiness_text = ("PASS: launch preflight gates passed. Project policy still needs owner review.\n" if self.ready else
                                   "BLOCKED: launch preflight gates failed. Prepare externally, then Retry readiness.\n") + details
        except PROBE_ERRORS:
            self.ready = False
            self.readiness_text = "BLOCKED: readiness checks unavailable; repair access and Retry readiness."
        return self.ready

    def request_start(self):
        self.start_loop = self.retry_readiness()
        return self.start_loop

    def back(self):
        if self.current not in {"welcome", "result"}:
            pages = self.pages()
            self.current = pages[pages.index(self.current) - 1]

    def clear_feature(self, name):
        if name == "priority":
            self.choices.priority_labels, self.choices.priority_default = (), None
            self.choices.create_priority_labels = False
        elif name == "retrospectives":
            self.choices.retrospectives.clear()
            self.choices.board_requests.clear()
            self.board_values = {role: "" for role in ROLES} | {"category": ""}
            self.board_waiting = False
        self.choices.features.discard(name)
        self.option_reviews.pop(name, None)

    def skip(self):
        name = self.current
        if not self.page().optional:
            return
        pages = self.pages()
        if name == "features":
            for feature in ("knowledge", "priority", "retrospectives"):
                self.clear_feature(feature)
        elif name == "priority-labels":
            self.choices.create_priority_labels = False
        else:
            self.clear_feature(name)
        self.plan = make_plan(self.plan.path, self.plan.repository, self.choices)
        self.current = next(page for page in pages[pages.index(name) + 1:] if page in self.pages())

    def deferred_commands(self):
        labels = configured_labels(self.plan.config) + priority_labels(self.choices)
        done = {name.casefold() for name in self.applied.label_names} if self.applied else set()
        if self.probes.existing_labels is not None:
            labels = self.missing(labels)
        return [label.command(self.plan.config.repository) for label in labels if label.name.casefold() not in done]

    def result_text(self):
        if self.cancelled or self.applied is None:
            return "Setup cancelled. No files, directories or GitHub labels or boards were changed."
        result = self.applied
        lines = ["Setup completed; launch readiness still requires project review." if result.ok else
                 "Setup partially failed. Completed effects remain; launch readiness has not been established."]
        lines.extend(result.completed)
        if result.board_numbers:
            lines.append("Verified board destinations: " + ", ".join(f"{role}: #{number}" for role, number in result.board_numbers.items()))
        if result.error:
            lines.extend([result.error, *result.incomplete, "Remaining work:", *result.remaining,
                          "Inspect completed files (and any partial file from the failed write). Repair access and complete missing files manually from the recovery content below. "
                          "Init refuses existing starter files; do not rerun it expecting overwrites."])
            lines.append(recovery_content(self.plan, result))
            if self.choices.board_requests:
                lines.append(f"Board recovery: inspect exact requested titles in {self.plan.config.repository}, including any creation with an unavailable response. "
                             f"Manual instructions: {MANUAL_BOARDS}. "
                             "If no starter file was written, rerun init and explicitly choose those same boards; verified existing titles are reused. "
                             "If starter files were written, finish missing files manually and configure only verified destinations.")
        lines.extend([self.option_summary(), self.effective_summary(), *guidance_summary(self.plan), "Launch readiness:",
                      self.readiness_text, "Before launch:", *next_steps(self.plan)])
        if "knowledge" in self.choices.features:
            lines.append("Uberblick optional follow-up: https://uberblick.ai")
        attention = [check for check in (self.probes.welcome + self.probes.prerequisites +
                                         self.probes.checkout + self.probes.runtime) if check.status != "ok"]
        if attention:
            lines.extend(["Pre-apply findings needing attention (run doctor again after committing):", findings(attention)])
        commands = self.deferred_commands()
        if commands:
            lines.extend(["Deferred labels; run these commands yourself (skip any that already exist):", *commands])
        return "\n\n".join(lines)
