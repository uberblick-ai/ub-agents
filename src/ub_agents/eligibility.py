"""Shared start rules, evaluated against each caller's observed inputs."""

from dataclasses import dataclass

from .config import Agent, Queue
from .github import Item


@dataclass(frozen=True)
class AgentMatches:
    configured: tuple[Agent, ...]
    matched: tuple[Agent, ...]
    trigger_labels: frozenset[str]
    workflow_triggers: frozenset[str]

    @classmethod
    def for_item(cls, item, agents):
        agents = tuple(agents)
        applicable = tuple(a for a in agents if a.kind in {"either", item.kind})
        return cls(agents,
                   tuple(a for a in applicable if item.state == "open" and item.labels.intersection(a.triggers)),
                   frozenset(label for a in applicable for label in a.triggers),
                   frozenset(label for a in agents for label in a.triggers))


@dataclass(frozen=True)
class StartCheck:
    reason: str | None = None
    stop_reason: str | None = None

    @property
    def allowed(self):
        return self.reason is None


def check_start(item: Item, agent: Agent, matches: AgentMatches, stop_labels,
                queue: Queue, blockers=()):
    stops = item.labels.intersection(stop_labels)
    stop_reason = f"Stop label {', '.join(sorted(stops))} is present" if stops else None
    if item.state != "open":
        return StartCheck(f"{item.kind} is {item.state}", stop_reason)
    if agent.kind not in {"either", item.kind}:
        return StartCheck(f"Agent {agent.name} does not apply to this {item.kind}", stop_reason)
    if agent not in matches.matched:
        return StartCheck("No trigger matches", stop_reason)
    if stop_reason:
        return StartCheck(stop_reason, stop_reason)
    reasons = []
    if item.kind == "issue":
        if queue.dependencies == "wait" and blockers:
            reasons.append(f"Waiting for blockers {', '.join(blockers)}")
    return StartCheck("; ".join(reasons) if reasons else None)


def open_blockers(github, item):
    return tuple(dict.fromkeys(b.reference(github.repository) for b in
        sorted(github.blocked_by(item.number), key=lambda b: (b.repository.casefold(), b.number))
        if b.state == "open"))
