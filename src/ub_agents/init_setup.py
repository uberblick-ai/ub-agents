"""Read-only starter plans and explicit, recoverable application."""

from dataclasses import dataclass, field
from importlib.resources import files
import json
from pathlib import Path
import subprocess

import yaml

from .config import LOCAL_CONFIG, preview_config, load_config
from .errors import AgentError
from .labels import Label, LabelUse, configured_labels

ROLES = ("issue-preparer", "implementer", "reviewer", "integrator")


def runtime_guidance(root, cli, *, fallback=False):
    names = ("AGENTS.md",) if cli == "codex" else ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")
    if fallback and cli == "codex":
        names += ("CLAUDE.md", ".claude/CLAUDE.md")
    return next((root / name for name in names if (root / name).is_file()), None)


def infer_repository(root):
    # Resolve from the nearest existing parent without creating the config directory.
    cwd = root
    while not cwd.exists():
        cwd = cwd.parent
    try:
        result = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=cwd,
                                capture_output=True, text=True, timeout=20, check=False,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise AgentError("Cannot infer repository; pass --repository owner/name") from None
    if result.returncode:
        raise AgentError("Cannot infer repository; pass --repository owner/name")
    try:
        return json.loads(result.stdout)["nameWithOwner"]
    except (ValueError, KeyError, TypeError):
        raise AgentError("Unreadable gh repository response") from None


def permission_args(cli):
    if cli == "claude":
        return ["--permission-mode", "acceptEdits", "--permission-prompts", "none", "--allowedTools",
                "Bash(git *)", "Bash(gh *)", "Bash({report_command} report *)",
                "Bash({report_command} read *)", "--add-dir", "{scratch}"]
    return ["--sandbox", "danger-full-access"]


@dataclass
class Choices:
    runtime: str
    permissions: bool = False
    workflow_labels: bool = False
    features: set[str] = field(default_factory=set)
    priority_labels: tuple[str, ...] = ()
    priority_default: str | None = None
    create_priority_labels: bool = False
    retrospectives: dict[str, int] = field(default_factory=dict)
    board_requests: dict = field(default_factory=dict)


@dataclass
class Plan:
    path: Path
    repository: str
    targets: dict[Path, str]
    ignore_before: bytes | None
    ignore_after: bytes
    local_before: bytes | None
    config: object
    choices: Choices

    def protect(self):
        existing = [str(p) for p in self.targets if p.exists() or p.is_symlink()]
        if existing:
            raise AgentError(f"Starter files already exist; nothing overwritten: {', '.join(existing)}")
        root = self.path.parent
        for path in self.targets:
            parent = path.parent
            while parent != root.parent:
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise AgentError(f"Starter directory must be a real directory: {parent}")
                if parent == root:
                    break
                parent = parent.parent
        ignore = root / ".gitignore"
        if ignore.is_symlink():
            raise AgentError(".gitignore is a symlink; update it manually before init")
        current = ignore.read_bytes() if ignore.exists() else None
        if current != self.ignore_before:
            raise AgentError(".gitignore changed during setup; cancel and run init again to review it")
        self.protect_local()

    def protect_local(self):
        local = self.path.parent / LOCAL_CONFIG
        current = local.read_bytes() if local.exists() else None
        if current != self.local_before:
            raise AgentError(f"{LOCAL_CONFIG} changed during setup; cancel and run init again to review its effective settings")

    def preview(self):
        lines = []
        for path, content in self.targets.items():
            lines.extend([f"Create {path}", content])
        ignore = self.path.parent / ".gitignore"
        if self.ignore_after != (self.ignore_before or b""):
            lines.extend([f"Update {ignore} (existing entries preserved)", self.ignore_after.decode("utf-8")])
        else:
            lines.append(f"Keep {ignore} unchanged")
        return "\n\n".join(lines)


def make_plan(path, repository, choices):
    path = Path(path).absolute()
    if path.is_symlink():
        raise AgentError(f"Starter files already exist; nothing overwritten: {path}")
    path = path.parent.resolve() / path.name
    root = path.parent
    templates = files("ub_agents").joinpath("templates")
    # Quote values to prevent runtime/model or repository input becoming YAML structure.
    content = templates.joinpath("ub-agents.yaml").read_text().replace(
        "your-org/your-project", json.dumps(repository)).replace(
        '"codex:gpt-6.1-sol:high"', json.dumps(choices.runtime))
    cli = choices.runtime.split(":", 1)[0]
    if cli == "claude":
        content = content.replace("#   codex: [--sandbox, danger-full-access]",
                                  "#   claude: " + json.dumps(permission_args(cli))).replace(
            "Grants full access without the Codex sandbox", "Grants unattended edits and git/gh/report commands")
        content = content.replace("# runtime-args:",
                                  '# Add the project\'s check commands to --allowedTools: "Bash(<project check command>)".\n'
                                  '# runtime-args:')
    if choices.permissions:
        content = content.replace("# runtime-args:", "runtime-args:").replace(f"#   {cli}:", f"  {cli}:")
    if choices.priority_labels:
        priority = {"labels": list(choices.priority_labels)}
        if choices.priority_default is not None:
            priority["default"] = choices.priority_default
        content = content.replace("  dependencies: wait", "  dependencies: wait\n" +
                                  "\n".join("  " + line for line in
                                            yaml.safe_dump({"priority": priority}, sort_keys=False).splitlines()))
    for role, number in choices.retrospectives.items():
        if role not in ROLES or type(number) is not int or number <= 0:
            raise AgentError("Retrospectives require a starter role and a positive integer discussion number")
        content = content.replace(f"  {role}:\n", f"  {role}:\n    retrospectives: {number}\n")
    if any(role not in ROLES for role in choices.board_requests):
        raise AgentError("Board creation requires an explicitly selected starter role")
    targets = {path: content}
    for role in ROLES:
        targets[root / ".agents" / f"{role}.md"] = templates.joinpath(f"{role}.md").read_text()
    guidance = runtime_guidance(root, cli, fallback=True)
    checks = (f"Project checks are documented in `{guidance.relative_to(root)}`." if guidance else
              "Replace these placeholders with the project's required commands before launch:\n\n"
              "- Build: `<project build command>`\n- Tests: `<project test command>`\n"
              "- Other checks: `<project lint or validation command>`")
    targets[root / ".agents" / "ub_agents.md"] = templates.joinpath("ub_agents.md").read_text().replace(
        "{checks}", checks)
    ignore = root / ".gitignore"
    before = ignore.read_bytes() if ignore.exists() else None
    entries = (before or b"").decode("utf-8").splitlines()
    missing = [entry for entry in (".ub-agents/", LOCAL_CONFIG) if entry not in entries]
    after = (before or b"") + ("\n# Local ub-agents configuration and execution artifacts\n" +
                               "\n".join(missing) + "\n").encode() if missing else (before or b"")
    # Protection runs before parsing overrides, reads from GitHub, or any mutations.
    local = root / LOCAL_CONFIG
    local_before = local.read_bytes() if local.exists() else None
    plan = Plan(path, repository, targets, before, after, local_before, None, choices)
    plan.protect()
    plan.config = preview_config(content, path, {p.resolve() for p in targets})
    return plan


def priority_labels(choices):
    return [Label(name, (LabelUse(None, "queue priority (highest to lowest)"),))
            for name in choices.priority_labels]


@dataclass
class Applied:
    completed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    error: str | None = None
    incomplete: list[str] = field(default_factory=list)
    label_names: list[str] = field(default_factory=list)
    board_numbers: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self):
        return self.error is None


def apply_plan(plan, github, remote_labels=(), *, verify_boards=False):
    """Stop on partial failure; retain and describe successful effects, never roll back."""
    from .doctor import Doctor, public
    result = Applied()
    pending = [f"Create {p}" for p in plan.targets]
    change_ignore = plan.ignore_after != (plan.ignore_before or b"")
    if change_ignore:
        pending.append(f"Update {plan.path.parent / '.gitignore'}")
    pending.append("Validate the effective configuration")
    pending.extend(f"Create label {label.name} on {plan.config.repository}" for label in remote_labels)
    board_steps = [f"Create or reuse board {request.title!r} for {role} on {plan.config.repository}"
                   for role, request in plan.choices.board_requests.items()]
    pending[:0] = board_steps
    phase = "local"
    try:
        plan.protect()
        if (plan.choices.board_requests or plan.choices.retrospectives or
                verify_boards and any(a.retrospectives for a in plan.config.agents)):
            from .init_boards import Boards
            boards = Boards(github, plan.config.repository)
            phase = "boards"
            repo = boards.setup()
            for number in {a.retrospectives for a in plan.config.agents if a.retrospectives}:
                boards.verify(number)
            for role, request in plan.choices.board_requests.items():
                # Re-discover the explicitly requested title immediately before creating.
                # This also makes manual recovery after a partial failure avoid duplicates.
                board = boards.find(request.title)
                if board is None:
                    current = boards.request(request.title, request.category, repo)
                    if current != request:
                        raise AgentError("Discussion category or repository changed; review the request again before creating")
                    result.incomplete.append(f"Board creation may have completed: {request.title!r}; inspect GitHub before retrying")
                    board = boards.create(request)
                    result.incomplete.pop()
                    result.completed.append(f"Created public board {board['url']} for {role}")
                else:
                    result.completed.append(f"Reused public board {board['url']} for {role}")
                verified = boards.verify(board["number"])
                result.board_numbers[role] = verified["number"]
                pending.pop(0)
            # No unverified or placeholder destination is ever written, even if only
            # some public creations succeeded. Keep their actual effects in the result.
            plan.choices.retrospectives.update(result.board_numbers)
            plan.choices.board_requests.clear()
            rebuilt = make_plan(plan.path, plan.repository, plan.choices)
            for number in {a.retrospectives for a in rebuilt.config.agents if a.retrospectives}:
                boards.verify(number)
            plan.targets, plan.config = rebuilt.targets, rebuilt.config
            plan.protect()
        phase = "local"
        for target, content in plan.targets.items():
            directories = []
            parent = target.parent
            while not parent.exists():
                directories.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            result.completed.extend(f"Created directory {p}" for p in reversed(directories))
            with target.open("x", encoding="utf-8") as stream:
                try:
                    stream.write(content)
                    stream.flush()
                except (OSError, UnicodeError, KeyboardInterrupt):
                    result.incomplete.append(f"Incomplete file: {target}; inspect it before recovery")
                    raise
            result.completed.append(pending.pop(0))
        if change_ignore:
            ignore = plan.path.parent / ".gitignore"
            if ignore.is_symlink() or (ignore.read_bytes() if ignore.exists() else None) != plan.ignore_before:
                raise AgentError(".gitignore changed during apply; preserve the newer entries and finish manually")
            ignore.write_bytes(plan.ignore_after)
            result.completed.append(pending.pop(0))
        plan.protect_local()
        load_config(plan.path)
        result.completed.append(pending.pop(0))
        phase = "labels"
        for label in remote_labels:
            # Recheck to preserve labels another operator created during the wizard.
            existing = {name.casefold() for name in github.labels()}
            if label.name.casefold() in existing:
                result.completed.append(f"Kept existing label {label.name} on {plan.config.repository}")
            else:
                github.create_label(label.name, label.description, label.color)
                result.completed.append(pending[0])
            result.label_names.append(label.name)
            pending.pop(0)
    except KeyboardInterrupt:
        result.error = "Apply interrupted; completed effects remain and remaining steps need manual recovery"
        result.remaining = pending
    except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        result.error = (Doctor.github_failure("board creation/verification", exc) if phase == "boards" else
                        Doctor.github_failure("label creation", exc) if phase == "labels" else
                        f"Local apply failed: {public(exc)}")
        result.remaining = pending
    return result


def recovery_content(plan, result):
    """Retain recovery content in the transcript after leaving the full-screen view."""
    lines = []
    for path, content in plan.targets.items():
        if f"Create {path}" in result.remaining:
            lines.extend([f"Remaining content for {path} (inspect any incomplete file first):", content])
    ignore = plan.path.parent / ".gitignore"
    if f"Update {ignore}" in result.remaining:
        lines.extend([f"Remaining content for {ignore} (preserve any newer entries):", plan.ignore_after.decode("utf-8")])
    return "\n\n".join(lines)


def next_steps(plan):
    cli = plan.choices.runtime.split(":", 1)[0]
    steps = ["Review build/test commands, merge authority, decision-makers and review priorities in .agents/ub_agents.md."]
    if not plan.choices.permissions:
        steps.append(f"grant agent permissions: uncomment or customize runtime-args in {plan.path.name}.")
    if cli == "claude":
        steps.append(f"Add project check commands to --allowedTools in {plan.path.name}.")
    if plan.choices.retrospectives and cli == "claude":
        steps.append('Review permission to post: add "Bash({report_command} retrospective *)" to the chosen roles only.')
    steps.extend([f"Run ub-agents check --config {plan.path}.",
                  "Review, commit and push the starter files through a PR; merge them into the default branch.",
                  "Use a clean default-branch launcher checkout; run ub-agents doctor before ub-agents launch."])
    return steps


def guidance_summary(plan):
    root = plan.path.parent
    messages = []
    for cli in sorted({runtime.cli for agent in plan.config.agents for runtime in agent.runtimes}):
        guidance = runtime_guidance(root, cli)
        if guidance:
            messages.append(f"{cli} loads project guidance from {guidance.relative_to(root)}; kept unchanged.")
        else:
            fallback = runtime_guidance(root, cli, fallback=True)
            if fallback:
                name = fallback.relative_to(root)
                messages.append(f'Warning: {cli} loads no project guidance; checks are documented in {name}. '
                                f'Add a one-line AGENTS.md: "Read {name}".')
            else:
                names = "AGENTS.md" if cli == "codex" else "CLAUDE.md, .claude/CLAUDE.md or AGENTS.md"
                messages.append(f"Warning: {cli} loads no project guidance; add {names} so agents know how to build and test.")
    return messages
