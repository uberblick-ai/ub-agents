"""Configured workflow labels and explicitly confirmed provisioning."""

from dataclasses import dataclass
import os
import shlex
import sys
from textwrap import shorten

from .errors import AgentError


@dataclass(frozen=True)
class LabelUse:
    agent: str | None
    meaning: str
    required: bool = True


@dataclass(frozen=True)
class Label:
    name: str
    uses: tuple[LabelUse, ...]

    @property
    def explanation(self):
        return "; ".join(use.meaning for use in self.uses)

    @property
    def description(self):
        description = f"ub-agents: {self.uses[0].meaning}"
        if len(description) > 100:
            return shorten(description, width=100, placeholder="...",
                           break_long_words=False, break_on_hyphens=False)
        for use in self.uses[1:]:
            candidate = f"{description}; {use.meaning}"
            if len(candidate) > 100:
                break
            description = candidate
        return description

    @property
    def color(self):
        return "d876e3" if any(use.agent is None for use in self.uses) else "1d76db"

    def command(self, repository):
        return shlex.join(["gh", "label", "create", self.name, "--repo", repository,
                           "--description", self.description, "--color", self.color])


def configured_labels(config):
    """Deduplicate names and retain every consumer, with effects before transitions."""
    labels = {}

    def add(name, use, *, effect=False):
        key = name.casefold()
        if key not in labels:
            labels[key] = (name, [], [])
        _, effects, transitions = labels[key]
        (effects if effect else transitions).append(use)

    for agent in config.agents:
        kind = {"issue": "an issue", "pr": "a PR", "either": "an issue or PR"}[agent.kind]
        for name in agent.triggers:
            add(name, LabelUse(agent.name, f"starts {agent.name} on {kind}"), effect=True)
        for outcome, transition in agent.outcomes.items():
            for action in ("add", "remove"):
                for name in transition[action]:
                    meaning = f"{'added' if action == 'add' else 'removed'} when {agent.name} reports {outcome}"
                    add(name, LabelUse(agent.name, meaning))
    for name in config.stop_labels:
        add(name, LabelUse(None, "parks an issue or PR until a person decides", required=False), effect=True)
    return [Label(name, tuple(effects + transitions)) for name, effects, transitions in labels.values()]


def print_commands(labels, repository, reason):
    print(f"{reason}; create workflow labels with these commands (skip any that already exist):")
    for label in labels:
        print(label.command(repository))


def confirm_label_creation(config, github, missing):
    """Share init's prompt; the caller has already explained every missing use."""
    if not missing or os.environ.get("CI") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input(f"Create these {len(missing)} labels on {config.repository}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().casefold() not in {"y", "yes"}:
        return False
    for label in missing:
        github.create_label(label.name, label.description, label.color)
        print(f"Created label {label.name} on {config.repository}.")
    return True


def provision_labels(config, github):
    labels = configured_labels(config)
    if os.environ.get("CI") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print_commands(labels, config.repository, "No interactive terminal; GitHub labels were not read or written")
        return
    try:
        existing = {name.casefold() for name in github.labels()}
    except (AgentError, OSError, UnicodeError):
        print_commands(labels, config.repository, "Could not read GitHub labels; no labels were written")
        return
    missing = [label for label in labels if label.name.casefold() not in existing]
    if not missing:
        print(f"All configured workflow labels exist on {config.repository}.")
        return
    print(f"Missing workflow labels on {config.repository}:")
    for label in missing:
        print(f"  {label.name}: {label.explanation}")
    if not confirm_label_creation(config, github, missing):
        print_commands(missing, config.repository, "Label creation declined; no labels were written")
