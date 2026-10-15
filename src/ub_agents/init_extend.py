"""The optional init steps for an already configured checkout."""

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import yaml

from .config import LOCAL_CONFIG, load_config, preview_config
from .doctor import public
from .errors import AgentError
from .init_boards import Boards
from .init_edit import RULE, UnsafeEdit, additions, permission_paths
from .init_flow import Flow, PRIORITIES, board_title, check_priorities
from .init_probes import PROBE_ERRORS, Probes
from .init_prompts import Cancelled, YELLOW
from .init_setup import Choices, priority_labels


class ExistingFlow(Flow):
    def __init__(self, *args, create_boards=False, create_priorities=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.create_boards, self.create_priorities = create_boards, create_priorities

    def run(self):
        self.t.intro("ub-agents init", "Optional setup")
        if not self.path.exists():
            raise AgentError(f"No {self.path.name} here; run plain ub-agents init first")
        if self.path.is_symlink():
            raise AgentError(f"{self.path.name} is a symlink; add the optional entries by hand")
        self.before = self.path.read_bytes()
        self.content = self.before.decode("utf-8")
        self.data = yaml.safe_load(self.content)
        self.config = load_config(self.path)
        self.repository = self.repository or self.config.repository
        if self.repository != self.config.repository:
            raise AgentError(f"Use --repository {self.config.repository} to match {self.path.name}")
        local = self.path.parent / LOCAL_CONFIG
        self.local_before = local.read_bytes() if local.exists() else None
        try:
            return self.steps()
        except (Cancelled, KeyboardInterrupt) as exc:
            self.t.cancelled(str(exc) or "Setup")
            return 1

    def probe(self):
        self.github = self.github_factory(self.repository)
        self.probes = Probes(SimpleNamespace(config=self.config), self.github, self.doctor).run()

    def choose_branch(self):
        # These flags operate on the current branch, without another confirmation.
        self.on_default = bool(self.probes.default_branch) and self.probes.branch == self.probes.default_branch

    def protect(self):
        local = self.path.parent / LOCAL_CONFIG
        if self.path.is_symlink() or self.path.read_bytes() != self.before:
            raise AgentError(f"{self.path.name} changed during setup; run init again to review it")
        if (local.read_bytes() if local.exists() else None) != self.local_before:
            raise AgentError(f"{LOCAL_CONFIG} changed during setup; run init again to review it")

    def steps(self):
        # Explicit optional steps need GitHub checks even with default answers.
        self.check()
        names = [name for name, role in self.data["agents"].items()
                 if self.create_boards and "retrospectives" not in role]
        need_priority = self.create_priorities and "priority" not in self.data.get("queue", {})
        if not names and not need_priority:
            self.t.outro("Nothing to do; the requested setup entries already exist.")
            return 0
        self.claude_agents = [agent.name for agent in self.config.agents
                              if agent.name in names and any(runtime.cli == "claude" for runtime in agent.runtimes)]
        priorities = ()
        if need_priority:
            value = self.ask.text("Priority labels, highest first", PRIORITIES, check_priorities)
            self.t.answered("Priority labels, highest first", value)
            priorities = tuple(label.strip() for label in value.split(","))
        labels = priority_labels(Choices("", priority_labels=priorities))
        existing = {name.casefold() for name in self.github.labels()} if priorities else set()
        labels = [label for label in labels if label.name.casefold() not in existing]
        boards, requests, numbers = Boards(self.github, self.repository), {}, {}
        if names:
            repo = boards.setup()
            categories = [category["name"] for category in repo["discussionCategories"]["nodes"]]
            if not categories:
                raise AgentError("Add a GitHub Discussions category before creating boards")
            category = "General" if "General" in categories else categories[0]
            # Discover every destination before any mutation, including duplicate titles.
            for name in names:
                requests[name] = boards.request(board_title(name), category, repo)
                found = boards.find(board_title(name))
                if found:
                    numbers[name] = found["number"]
        placeholders = {name: numbers.get(name, 1) for name in names}
        try:
            preview = additions(self.content, placeholders, priorities, self.claude_agents)
            preview_config(preview, self.path, ())
        except (UnsafeEdit, AgentError, yaml.YAMLError) as exc:
            self.t.step(f"{self.path.name} needs a manual edit", [str(exc)])
            self.manual(names, numbers, priorities)
            return 1
        lines = [f"Add retrospectives to agents.{name}: #{numbers[name]} (reuse)" if name in numbers else
                 f"Create {board_title(name)!r}; add agents.{name}.retrospectives" for name in names]
        if priorities:
            lines.append("Add queue.priority.labels: " + ", ".join(priorities))
        lines += [f"Create label {label.name}" for label in labels]
        lines += [f"Add {RULE!r} where missing in {'.'.join(path)}"
                  for path in permission_paths(self.data, self.claude_agents)]
        lines.append("Labels and boards are created on GitHub right away; " +
                     ("the config goes to a new PR." if self.on_default else "the config stays local."))
        self.t.note("Changes", lines)
        if not self.ask.confirm("Write setup?", True):
            raise Cancelled("Write setup?")
        self.t.answered("Write setup?", "Yes")
        completed, incomplete = [], []
        try:
            self.protect()
            # Recheck every board before the first write, so a newly duplicated
            # title or changed capability cannot leave a partially applied plan.
            if names:
                current = boards.setup()
                for name, request in requests.items():
                    if boards.request(request.title, request.category, current) != request:
                        raise AgentError("Discussion category changed; run init again")
                    found = boards.find(request.title)
                    if found:
                        numbers[name] = found["number"]
                    else:
                        numbers.pop(name, None)
            for name, request in requests.items():
                if name not in numbers:
                    incomplete.append(f"Board creation may have completed: {request.title!r}; inspect GitHub before retrying")
                    board = boards.create(request)
                    numbers[name] = board["number"]
                    incomplete.pop()
                    completed.append(f"Created board {board['url']} for {name}")
                else:
                    completed.append(f"Reused board #{numbers[name]} for {name}")
                boards.verify(numbers[name])
            for label in labels:
                if label.name.casefold() not in {name.casefold() for name in self.github.labels()}:
                    incomplete.append(f"Label creation may have completed: {label.name!r}; inspect GitHub before retrying")
                    self.github.create_label(label.name, label.description, label.color)
                    incomplete.pop()
                    completed.append(f"Created label {label.name}")
                else:
                    completed.append(f"Kept existing label {label.name}")
            content = additions(self.content, numbers, priorities, self.claude_agents)
            preview_config(content, self.path, ())
            self.write(content)
        except (*PROBE_ERRORS, UnsafeEdit, yaml.YAMLError, KeyboardInterrupt) as exc:
            self.t.step("Setup partly written", [*completed, public(str(exc)) or "Setup interrupted", *incomplete], "▲", YELLOW)
            self.manual(names, numbers, priorities)
            return 1
        self.t.step("Setup written", completed)
        if self.on_default:
            plan = SimpleNamespace(config=self.config, path=self.path, targets={self.path: content},
                                   ignore_before=b"", ignore_after=b"")
            url = self.open_pr(plan, unique=True, title="Add optional ub-agents setup",
                               body="Adds missing retrospective boards or queue priorities with `ub-agents init`.\n\n"
                                    "Boards and labels are already available on GitHub. Review and merge the configuration.")
            if not url:
                self.t.outro("The config is written; finish opening its setup PR by hand.")
                return 1
            self.t.outro("Review and merge the setup PR: ", url)
        else:
            self.t.outro(f"Review and commit {self.path.name}.")
        return 0

    def write(self, content):
        """Replace the config atomically only after a complete, validated edit."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".ub-agents-init-", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), self.path.stat().st_mode & 0o777)
                stream.write(content.encode("utf-8"))
                stream.flush()
                self.protect()
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def manual(self, names, numbers, priorities):
        lines = [f"Entries still to add to {self.path.name} (preserve its other settings):"]
        for name in names:
            number = numbers.get(name, f"<number of {board_title(name)!r}>")
            lines += [f"agents.{name}:", f"  retrospectives: {number}"]
        if priorities:
            lines += ["queue:", "  priority:", "    labels: " + yaml.safe_dump(list(priorities), default_flow_style=True).strip()]
        lines += [f"In {'.'.join(path)}, add {RULE!r} to --allowedTools when missing."
                  for path in permission_paths(self.data, self.claude_agents)]
        self.t.console.print("\n".join(lines), markup=False, highlight=False)
