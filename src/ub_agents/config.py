"""Small strict YAML configuration; there is no workflow expression language."""

from dataclasses import dataclass
from pathlib import Path
import math
import re

import yaml

from .errors import AgentError
from .github import REPOSITORY


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise AgentError(f"YAML keys must be unique strings: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def mapping(value, allowed, where):
    if not isinstance(value, dict):
        raise AgentError(f"{where} must be a mapping")
    unknown = value.keys() - allowed
    if unknown:
        raise AgentError(f"Unknown {where} keys: {', '.join(sorted(unknown))}")
    return value


def string(value, where):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AgentError(f"{where} must be a nonempty string")
    return value


def strings(value, where):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise AgentError(f"{where} must be a string or nonempty list")
    return tuple(string(v, where) for v in value)


def number(value, where, integer=False, zero=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise AgentError(f"{where} must be a number")
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise AgentError(f"{where} must be finite and {'nonnegative' if zero else 'positive'}")
    if integer and not isinstance(value, int):
        raise AgentError(f"{where} must be an integer")
    return value


def project_path(root, value, where):
    path = (root / string(value, where)).resolve()
    if not path.is_relative_to(root):
        raise AgentError(f"{where} must remain inside the project")
    if not path.is_file():
        raise AgentError(f"{where} does not exist: {path}")
    return path


CLIS = ("codex", "claude")


@dataclass(frozen=True)
class Runtime:
    cli: str
    model: str
    effort: str

    @property
    def name(self):
        return f"{self.cli}:{self.model}:{self.effort}"

    def different_from(self, other):
        # Effort alone never establishes independent execution; the CLI implies the provider.
        return self.cli != other.cli and self.model != other.model


@dataclass(frozen=True)
class Agent:
    name: str
    triggers: tuple[str, ...]
    instructions: Path | None
    runtimes: tuple[Runtime, ...]
    command: tuple[str, ...]
    runtime_args: tuple[str, ...]
    different_from: str | None
    kind: str
    worktree: bool
    lease_seconds: float
    timeout_seconds: float
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    outcomes: dict


@dataclass(frozen=True)
class Priority:
    labels: tuple[str, ...] = ()
    default: str | None = None

    def effective(self, labels):
        return next((label for label in self.labels if label in labels), self.default)

    def rank(self, labels):
        label = self.effective(labels)
        return self.labels.index(label) if label is not None else len(self.labels)


@dataclass(frozen=True)
class Queue:
    milestones: str = "ignore"
    priority: Priority = Priority()
    dependencies: str = "wait"


@dataclass(frozen=True)
class CleanupHook:
    command: tuple[str, ...]
    timeout_seconds: float = 60


@dataclass(frozen=True)
class Config:
    root: Path
    repository: str
    agents: tuple[Agent, ...]
    poll_seconds: float
    stop_labels: tuple[str, ...]
    queue: Queue = Queue()
    cleanup: CleanupHook | None = None


CLOCKS = {"agent-timeout-minutes", "max-attempts", "retry-backoff-seconds", "max-backoff-seconds"}
DEFAULTS = {"agent-timeout-minutes": 180, "max-attempts": 5,
            "retry-backoff-seconds": 60, "max-backoff-seconds": 3600}
# A claim is never renewed: its lease covers the run's timeout, the cleanup hook, setup and completion.
LEASE_GRACE_SECONDS = 15 * 60


def load_config(path):
    path = Path(path).resolve()
    root = path.parent
    try:
        data = yaml.load(path.read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise AgentError(f"Cannot read configuration {path}: {exc}") from exc
    data = mapping(data, {"repository", "agents", "limits", "poll-seconds", "stop-labels", "queue", "cleanup"},
                   "configuration")
    cleanup = None
    if "cleanup" in data:
        hook = mapping(data["cleanup"], {"command", "timeout-seconds"}, "cleanup")
        cleanup = CleanupHook(argv(hook.get("command"), "cleanup command"),
                              number(hook.get("timeout-seconds", 60), "cleanup timeout-seconds"))
        if cleanup.timeout_seconds > 3600:
            raise AgentError("cleanup timeout-seconds must be at most 3600")
    queue = mapping(data.get("queue", {}), {"milestones", "priority", "dependencies"}, "queue")
    milestones = queue.get("milestones", "ignore")
    if milestones not in ("gate", "ignore"):
        raise AgentError("queue milestones must be gate or ignore")
    dependencies = queue.get("dependencies", "wait")
    if dependencies not in ("wait", "ignore"):
        raise AgentError("queue dependencies must be wait or ignore")
    priority = Priority()
    if "priority" in queue:
        settings = mapping(queue["priority"], {"labels", "default"}, "queue priority")
        labels = argv(settings.get("labels"), "queue priority labels")
        if len(set(labels)) != len(labels):
            raise AgentError("queue priority labels must be unique")
        default = settings.get("default")
        if "default" in settings and default not in labels:
            raise AgentError("queue priority default must be one of labels")
        priority = Priority(labels, default)
    repo = string(data.get("repository"), "repository")
    if not re.fullmatch(REPOSITORY, repo):
        raise AgentError("repository must be owner/name")
    limits = DEFAULTS | mapping(data.get("limits", {}), CLOCKS, "limits")
    definitions = data.get("agents")
    if not isinstance(definitions, dict) or not definitions:
        raise AgentError("agents must be a nonempty mapping")
    stop = () if data.get("stop-labels") == [] else strings(data.get("stop-labels", ["needs-human"]), "stop-labels")
    agents = []
    for name, definition in definitions.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise AgentError("Agent names must be lowercase slugs")
        item = mapping(definition, CLOCKS | {"runtime", "trigger", "instructions",
            "command", "runtime-args", "different-runtime-from", "kind",
            "worktree", "outcomes"}, f"agent {name}")
        if ("runtime" in item) == ("command" in item):
            raise AgentError(f"{name}: specify exactly one of runtime or command")
        command = argv(item["command"], f"{name} command") if "command" in item else ()
        if command and "/" in command[0] and not Path(command[0]).is_absolute():
            # Relative executables resolve against the operator's checkout, like instructions.
            command = (str(root / command[0]),) + command[1:]
        runtimes = []
        for spec in strings(item["runtime"], f"{name} runtime") if "runtime" in item else ():
            parts = spec.split(":")
            if len(parts) != 3 or not all(parts):
                raise AgentError(f"{name}: runtime must be cli:model:effort")
            cli, model, effort = parts
            if cli not in CLIS:
                raise AgentError(f"{name}: runtime cli must be one of {', '.join(CLIS)}")
            runtimes.append(Runtime(cli, model, effort))
        instruction = (project_path(root, item["instructions"], f"{name} instructions")
                       if "instructions" in item else None)
        if runtimes and instruction is None:
            raise AgentError(f"{name}: runtime execution requires instructions")
        runtime_args = argv(item.get("runtime-args", []), f"{name} runtime-args", empty=True)
        # Provenance records cli:model:effort and independence checks trust it; sessions start fresh.
        forbidden = {"--model", "-m", "--effort", "--resume", "-r", "resume", "--continue",
                     "model", "model_provider", "model_reasoning_effort"}
        if any(r.cli == "claude" for r in runtimes):
            forbidden.add("-c")  # Claude's --continue; Codex's -c is --config.
        if any(arg.split("=", 1)[0] in forbidden for arg in runtime_args):
            raise AgentError(f"{name}: runtime-args must not change the model, effort or session")
        different = item.get("different-runtime-from")
        if different is not None:
            string(different, f"{name} different-runtime-from")
            if different not in definitions or different == name or command:
                raise AgentError(f"{name}: different-runtime-from must reference another runtime agent")
        kind = item.get("kind", "either")
        if kind not in {"issue", "pr", "either"}:
            raise AgentError(f"{name}: kind must be issue, pr, or either")
        worktree = item.get("worktree", False)
        if not isinstance(worktree, bool):
            raise AgentError(f"{name}: worktree must be boolean")
        clocks = limits | {k: item[k] for k in CLOCKS if k in item}
        for key, value in clocks.items():
            number(value, f"{name} {key}", integer=key == "max-attempts",
                   zero=key in {"retry-backoff-seconds", "max-backoff-seconds"})
        if clocks["max-backoff-seconds"] < clocks["retry-backoff-seconds"]:
            raise AgentError(f"{name}: max-backoff-seconds must cover retry-backoff-seconds")
        triggers = strings(item.get("trigger"), f"{name} trigger")
        declared = item.get("outcomes")
        if not isinstance(declared, dict) or not declared:
            raise AgentError(f"{name}: outcomes must be a nonempty mapping of names to transitions")
        outcomes = {}
        for outcome, transition in declared.items():
            string(outcome, f"{name} outcome")
            mapping(transition, {"add", "remove"}, f"{name} outcome {outcome}")
            labels = {key: argv(transition.get(key, []), f"{name} {outcome} {key}", empty=True)
                      for key in ("add", "remove")}
            if set(labels["add"]).intersection(triggers):
                raise AgentError(f"{name}: outcome cannot add its own trigger")
            if set(labels["remove"]).union(triggers).intersection(stop):
                raise AgentError(f"{name}: outcome cannot remove a stop label")
            outcomes[outcome] = labels
        agents.append(Agent(name, triggers, instruction,
            tuple(runtimes), command, runtime_args, different, kind, worktree,
            clocks["agent-timeout-minutes"] * 60 + LEASE_GRACE_SECONDS + (cleanup.timeout_seconds if cleanup else 0),
            clocks["agent-timeout-minutes"] * 60, clocks["max-attempts"],
            clocks["retry-backoff-seconds"], clocks["max-backoff-seconds"], outcomes))
    for agent in agents:
        if agent.different_from and not next(a for a in agents if a.name == agent.different_from).runtimes:
            raise AgentError(f"{agent.name}: runtime independence requires runtime provenance")
    poll = number(data.get("poll-seconds", 30), "poll-seconds")
    return Config(root, repo, tuple(agents), poll, stop, Queue(milestones, priority, dependencies), cleanup)


def argv(value, where, empty=False):
    if not isinstance(value, list) or (not empty and not value):
        raise AgentError(f"{where} must be an argv list")
    return tuple(string(v, where) for v in value)
