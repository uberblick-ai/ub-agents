"""Small strict YAML configuration; there is no workflow expression language."""

from dataclasses import dataclass
from pathlib import Path
import math
import re

import yaml

from .errors import AgentError, InstructionError
from .github import REPOSITORY, REQUEST_TIMEOUT_SECONDS

DEFAULT_CONFIG = "ub-agents.yaml"
LOCAL_CONFIG = "ub-agents.local.yaml"
LEASE_SECONDS = 30 * 60
LEASE_RENEW_SECONDS = 10 * 60
LEASE_RENEW_MARGIN_SECONDS = 1


def resolve_config_path(path=None, *, root=None):
    """Resolve an explicit path or the project default on every reload."""
    if path is not None:
        return Path(path).resolve()
    root = Path.cwd() if root is None else Path(root)
    return (root / DEFAULT_CONFIG).resolve()


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    path = getattr(loader, "mapping_path", ())
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            if path == ("runtime-args",):
                raise AgentError(f"runtime-args {key!r}: YAML keys must be unique strings")
            if len(path) == 3 and path[0] == "agents" and path[2] == "runtime-args":
                raise AgentError(f"{path[1]} runtime-args {key!r}: YAML keys must be unique strings")
            raise AgentError(f"YAML keys must be unique strings: {key!r}")
        loader.mapping_path = (*path, key)
        try:
            result[key] = loader.construct_object(value_node, deep=deep)
        finally:
            loader.mapping_path = path
        # Preserve exactly these policy spellings, not other YAML booleans.
        if (key == "approvals" and isinstance(value_node, yaml.ScalarNode)
                and value_node.value in {"on", "off"}):
            result[key] = value_node.value
        # YAML 1.1 parses unquoted `off` as False. Preserve just this policy
        # spelling without changing existing YAML booleans elsewhere.
        if key == "runtime-updates" and isinstance(result[key], dict):
            for policy_key, policy_value in value_node.value:
                if (policy_key.value in UPDATE_CLIS
                        and isinstance(policy_value, yaml.ScalarNode)
                        and policy_value.value == "off"):
                    result[key][policy_key.value] = "off"
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


def project_path(root, value, where, *, planned=()):
    path = (root / string(value, where)).resolve()
    if not path.is_relative_to(root):
        raise InstructionError(f"{where} must remain inside the project")
    if path not in planned and not path.is_file():
        raise InstructionError(f"{where} does not exist: {path}")
    return path


def instruction_text(root, path, where):
    if path is None:
        return ""
    validated = project_path(root.resolve(), str(path), where)
    try:
        return validated.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstructionError(f"{where} is unreadable: {path}: {exc}") from exc


CLIS = ("codex", "claude")
UPDATE_CLIS = (*CLIS, "gh", "ub")


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
    runtime_args: tuple[str, ...] | dict[str, tuple[str, ...]]
    different_from: tuple[str, ...]
    kind: str
    worktree: bool
    lease_seconds: float
    timeout_seconds: float
    max_attempts: int
    backoff_seconds: float
    max_backoff_seconds: float
    outcomes: dict
    retrospectives: int | None = None
    health_check: tuple[str, ...] = ()
    on_conflict: str | None = None

    def runtime_args_for(self, runtime):
        if isinstance(self.runtime_args, dict):
            return self.runtime_args.get(runtime.cli, ())
        return self.runtime_args


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
class CheckoutSetup:
    command: tuple[str, ...]
    when_changed: tuple[str, ...] | None = None
    timeout_seconds: float = 600


@dataclass(frozen=True)
class RuntimeUpdates:
    policies: dict
    timeout_seconds: float = 300


@dataclass(frozen=True)
class Config:
    root: Path
    repository: str
    agents: tuple[Agent, ...]
    poll_seconds: float
    stop_labels: tuple[str, ...]
    queue: Queue = Queue()
    cleanup: CleanupHook | None = None
    runtime_updates: RuntimeUpdates | None = None
    launchers: tuple[str, ...] | None = None
    approvals: str | None = None
    trusted_bots: tuple[str, ...] = ()
    shared_instructions: Path | None = None
    checkout_setup: CheckoutSetup | None = None
    lease_seconds: float = LEASE_SECONDS
    lease_renew_seconds: float = LEASE_RENEW_SECONDS
    trusted_pr_authors: tuple[str, ...] = ()

    @property
    def sub_issue_labels(self):
        return frozenset(label for agent in self.agents for transition in agent.outcomes.values()
                         for label in transition.get("sub-issues", {}).get("add", ()))


CLOCKS = {"agent-timeout-minutes", "max-attempts", "retry-backoff-seconds", "max-backoff-seconds"}
LEASE_CLOCKS = {"lease-minutes", "lease-renew-minutes"}
DEFAULTS = {"agent-timeout-minutes": 180, "max-attempts": 5,
            "retry-backoff-seconds": 60, "max-backoff-seconds": 3600}


def _read_config(path, *, optional=False):
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if optional and not path.is_symlink():
            return {}
        raise AgentError(f"Cannot read configuration {path}: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise AgentError(f"Cannot read configuration {path}: {exc}") from exc
    loader = UniqueLoader(content)
    try:
        node = loader.get_single_node()
        # An empty document inherits; an explicit YAML null is still a value.
        empty = node is None or (isinstance(node, yaml.ScalarNode) and node.value == ""
                                 and node.start_mark.index == node.end_mark.index)
        if empty and optional:
            return {}
        data = loader.construct_document(node) if node is not None else None
        if not isinstance(data, dict):
            raise AgentError("configuration must be a mapping")
        return data
    except (AgentError, yaml.YAMLError) as exc:
        raise AgentError(f"Cannot read configuration {path}: {exc}") from exc
    finally:
        loader.dispose()


def _merge_config(base, local):
    """Merge mappings by key; all other supplied values replace the base."""
    merged = base.copy()
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path):
    path = Path(path).resolve()
    base = _read_config(path)
    local_path = path.parent / LOCAL_CONFIG
    local = _read_config(local_path, optional=True)
    try:
        return _parse_config(_merge_config(base, local), path.parent)
    except AgentError as exc:
        if not local:
            raise
        raise type(exc)(f"Invalid configuration {path} with local overrides {local_path}: {exc}") from exc


def preview_config(content, path, planned):
    """Validate a proposed init configuration and local overrides without writes."""
    path = Path(path).resolve()
    try:
        base = yaml.load(content, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise AgentError(f"Cannot read proposed configuration: {exc}") from exc
    local_path = path.parent / LOCAL_CONFIG
    local = _read_config(local_path, optional=True)
    try:
        return _parse_config(_merge_config(base, local), path.parent, planned=planned)
    except AgentError as exc:
        if not local:
            raise
        raise type(exc)(f"Invalid configuration {path} with local overrides {local_path}: {exc}") from exc


def _parse_config(data, root, *, planned=()):
    data = mapping(data, {"repository", "agents", "limits", "poll-seconds", "stop-labels", "queue", "cleanup",
                          "checkout-setup", "runtime-updates", "runtime-args", "launchers", "approvals", "trusted-bots",
                          "trusted-pr-authors", "shared-instructions"},
                   "configuration")
    runtime_defaults = runtime_arguments(
        mapping(data.get("runtime-args", {}), set(CLIS), "runtime-args"), set(CLIS))
    # Keep symlinks in the configured path so each run revalidates their targets.
    shared = (root / string(data["shared-instructions"], "shared-instructions")
              if "shared-instructions" in data else None)
    if shared is not None:
        project_path(root, str(shared), "shared-instructions", planned=planned)
    approvals = data.get("approvals")
    if "approvals" in data and (not isinstance(approvals, str) or approvals not in {"on", "off"}):
        raise AgentError("approvals must be on or off")
    trusted_bots = bot_logins(data.get("trusted-bots", []))
    trusted_pr_authors = bot_logins(data.get("trusted-pr-authors", []), "trusted-pr-authors")
    launchers = None
    if "launchers" in data:
        launchers = argv(data["launchers"], "launchers")
        if any(login != login.strip() for login in launchers):
            raise AgentError("launchers logins must not contain surrounding whitespace")
        if len({login.casefold() for login in launchers}) != len(launchers):
            raise AgentError("launchers logins must be unique (case-insensitive)")
    runtime_updates = None
    if "runtime-updates" in data:
        settings = mapping(data["runtime-updates"], {*UPDATE_CLIS, "timeout-seconds"}, "runtime-updates")
        policies = {}
        for cli in UPDATE_CLIS:
            policy = settings.get(cli, "off")
            if isinstance(policy, str) and policy in {"auto", "off"}:
                policies[cli] = policy
            else:
                command = mapping(policy, {"command"}, f"runtime-updates {cli}")
                args = argv(command.get("command"), f"runtime-updates {cli} command")
                if Path(args[0]).name in {"sudo", "su", "doas", "pkexec"}:
                    raise AgentError("runtime-updates commands must not request elevated privileges")
                if "/" in args[0] and not Path(args[0]).is_absolute():
                    args = (str(root / args[0]),) + args[1:]
                policies[cli] = args
        timeout = number(settings.get("timeout-seconds", 300), "runtime-updates timeout-seconds")
        if timeout > 3600:
            raise AgentError("runtime-updates timeout-seconds must be at most 3600")
        runtime_updates = RuntimeUpdates(policies, timeout)
    cleanup = None
    if "cleanup" in data:
        hook = mapping(data["cleanup"], {"command", "timeout-seconds"}, "cleanup")
        cleanup = CleanupHook(argv(hook.get("command"), "cleanup command"),
                              number(hook.get("timeout-seconds", 60), "cleanup timeout-seconds"))
        if cleanup.timeout_seconds > 3600:
            raise AgentError("cleanup timeout-seconds must be at most 3600")
    checkout_setup = None
    if "checkout-setup" in data:
        setup = mapping(data["checkout-setup"], {"command", "when-changed", "timeout-seconds"}, "checkout-setup")
        paths = (argv(setup["when-changed"], "checkout-setup when-changed")
                 if "when-changed" in setup else None)
        for value in paths or ():
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts or (root / path).is_dir():
                raise AgentError("checkout-setup when-changed must contain repository-relative file paths")
        timeout = number(setup.get("timeout-seconds", 600), "checkout-setup timeout-seconds")
        if timeout > 3600:
            raise AgentError("checkout-setup timeout-seconds must be at most 3600")
        checkout_setup = CheckoutSetup(argv(setup.get("command"), "checkout-setup command"), paths, timeout)
    queue = mapping(data.get("queue", {}), {"milestones", "priority", "dependencies"}, "queue")
    milestones = queue.get("milestones", "ignore")
    if milestones not in ("strict-gate", "gate", "order", "ignore"):
        raise AgentError("queue milestones must be strict-gate, gate, order or ignore")
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
    limits = DEFAULTS | mapping(data.get("limits", {}), CLOCKS | LEASE_CLOCKS, "limits")
    lease_seconds = number(limits.pop("lease-minutes", LEASE_SECONDS / 60), "limits lease-minutes") * 60
    renew_seconds = number(limits.pop("lease-renew-minutes", LEASE_RENEW_SECONDS / 60),
                           "limits lease-renew-minutes") * 60
    if renew_seconds + REQUEST_TIMEOUT_SECONDS + LEASE_RENEW_MARGIN_SECONDS >= lease_seconds:
        raise AgentError("limits lease-renew-minutes plus the renewal request bound "
                         f"({REQUEST_TIMEOUT_SECONDS + LEASE_RENEW_MARGIN_SECONDS} seconds) "
                         "must be shorter than lease-minutes")
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
            "worktree", "outcomes", "retrospectives", "health-check", "on-conflict"}, f"agent {name}")
        retrospectives = item.get("retrospectives")
        if "retrospectives" in item and (type(retrospectives) is not int or retrospectives <= 0):
            raise AgentError(f"{name} retrospectives must be a positive integer discussion number")
        if ("runtime" in item) == ("command" in item):
            raise AgentError(f"{name}: specify exactly one of runtime or command")
        command = argv(item["command"], f"{name} command") if "command" in item else ()
        if command and "/" in command[0] and not Path(command[0]).is_absolute():
            # Relative executables resolve against the operator's checkout, like instructions.
            command = (str(root / command[0]),) + command[1:]
        health_check = argv(item["health-check"], f"{name} health-check") if "health-check" in item else ()
        if health_check and "/" in health_check[0] and not Path(health_check[0]).is_absolute():
            health_check = (str(root / health_check[0]),) + health_check[1:]
        runtimes = []
        for spec in strings(item["runtime"], f"{name} runtime") if "runtime" in item else ():
            parts = spec.split(":")
            if len(parts) != 3 or not all(parts):
                raise AgentError(f"{name}: runtime must be cli:model:effort")
            cli, model, effort = parts
            if cli not in CLIS:
                raise AgentError(f"{name}: runtime cli must be one of {', '.join(CLIS)}")
            runtimes.append(Runtime(cli, model, effort))
        # Retain the configured path, including symlinks, for validation on each run.
        instruction = (root / string(item["instructions"], f"{name} instructions")
                       if "instructions" in item else None)
        if instruction is not None:
            project_path(root, str(instruction), f"{name} instructions", planned=planned)
        if runtimes and instruction is None:
            raise AgentError(f"{name}: runtime execution requires instructions")
        clis = {runtime.cli for runtime in runtimes}
        if "runtime-args" in item:
            runtime_args = runtime_arguments(item["runtime-args"], clis, name)
        else:
            runtime_args = ({cli: args for cli, args in runtime_defaults.items() if cli in clis}
                            if runtimes and runtime_defaults else ())
        different = item.get("different-runtime-from")
        if different is not None:
            different = strings(different, f"{name} different-runtime-from")
            if len(set(different)) != len(different):
                raise AgentError(f"{name}: different-runtime-from must contain unique agent names")
            if any(source not in definitions or source == name for source in different) or command:
                raise AgentError(f"{name}: different-runtime-from must reference another runtime agent")
        else:
            different = ()
        kind = item.get("kind", "either")
        if kind not in ("issue", "pr", "either"):
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
            mapping(transition, {"add", "remove", "sub-issues", "max-rounds"}, f"{name} outcome {outcome}")
            labels = {key: argv(transition.get(key, []), f"{name} {outcome} {key}", empty=True)
                      for key in ("add", "remove")}
            if "max-rounds" in transition:
                limit = transition["max-rounds"]
                if type(limit) is not int or limit <= 0:
                    raise AgentError(f"{name} {outcome} max-rounds must be a positive integer")
                if not stop or set(labels["add"]).intersection(stop):
                    raise AgentError(f"{name} {outcome} max-rounds requires stop labels "
                                     "and an outcome that adds no stop label")
                labels["max-rounds"] = limit
            if "sub-issues" in transition:
                if kind == "pr":
                    raise AgentError(f"{name}: sub-issues requires an agent that takes issues")
                children = mapping(transition["sub-issues"], {"add"}, f"{name} {outcome} sub-issues")
                labels["sub-issues"] = {"add": argv(children.get("add"), f"{name} {outcome} sub-issues add")}
            if set(labels["add"]).intersection(triggers):
                raise AgentError(f"{name}: outcome cannot add its own trigger")
            if set(labels["remove"]).union(triggers).intersection(stop):
                raise AgentError(f"{name}: outcome cannot remove a stop label")
            outcomes[outcome] = labels
        on_conflict = string(item["on-conflict"], f"{name} on-conflict") if "on-conflict" in item else None
        if on_conflict is not None and (kind != "pr" or on_conflict not in outcomes
                                        or set(outcomes[on_conflict]["add"]).intersection(stop)):
            raise AgentError(f"{name}: on-conflict must name a declared outcome of a pr agent "
                             "that adds no stop label")
        agents.append(Agent(name, triggers, instruction,
            tuple(runtimes), command, runtime_args, different, kind, worktree,
            lease_seconds,
            clocks["agent-timeout-minutes"] * 60, clocks["max-attempts"],
            clocks["retry-backoff-seconds"], clocks["max-backoff-seconds"], outcomes, retrospectives, health_check,
            on_conflict))
    issue_triggers = {label for a in agents if a.kind != "pr" for label in a.triggers}
    for agent in agents:
        for transition in agent.outcomes.values():
            if "sub-issues" in transition:
                labels = set(transition["sub-issues"]["add"])
                if labels.intersection(stop) or not labels <= issue_triggers:
                    raise AgentError(f"{agent.name}: sub-issues add must contain only issue agent trigger labels, "
                                     "and no stop labels")
        for source in agent.different_from:
            if not next(a for a in agents if a.name == source).runtimes:
                raise AgentError(f"{agent.name}: runtime independence requires runtime provenance from {source}")
    poll = number(data.get("poll-seconds", 30), "poll-seconds")
    return Config(root, repo, tuple(agents), poll, stop, Queue(milestones, priority, dependencies), cleanup,
                  runtime_updates, launchers, approvals, trusted_bots, shared, checkout_setup,
                  lease_seconds=lease_seconds, lease_renew_seconds=renew_seconds,
                  trusted_pr_authors=trusted_pr_authors)


def bot_logins(value, where="trusted-bots"):
    if (not isinstance(value, list) or any(not isinstance(login, str) or
            not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\[bot\])?", login, re.IGNORECASE)
            for login in value)):
        raise AgentError(f"{where} must be a list of GitHub account logins")
    if len({login.casefold() for login in value}) != len(value):
        raise AgentError(f"{where} logins must be unique (case-insensitive)")
    return tuple(value)


def argv(value, where, empty=False):
    if not isinstance(value, list) or (not empty and not value):
        raise AgentError(f"{where} must be an argv list")
    return tuple(string(v, where) for v in value)


def runtime_arguments(value, clis, name=None):
    prefix = f"{name} " if name is not None else ""
    if isinstance(value, dict):
        result = {}
        for cli, arguments in value.items():
            where = f"{prefix}runtime-args {cli}"
            if cli not in CLIS:
                raise AgentError(f"{where}: key must be one of {', '.join(CLIS)}")
            if cli not in clis:
                raise AgentError(f"{where}: key names no CLI in the agent's runtime list")
            result[cli] = argv(arguments, where, empty=True)
            validate_runtime_args(result[cli], {cli}, name, key=cli)
        return result
    arguments = argv(value, f"{prefix}runtime-args", empty=True)
    validate_runtime_args(arguments, clis, name)
    return arguments


def validate_runtime_args(arguments, clis, name, *, key=None):
    label = "runtime-args" + (f" {key}" if key is not None else "")
    prefix = f"{name}: " if name is not None else ""
    for arg in arguments:
        for placeholder in re.findall(r"\{[a-zA-Z0-9_-]+\}", arg):
            if placeholder not in {"{scratch}", "{report_command}"}:
                raise AgentError(f"{prefix}unknown {label} placeholder {placeholder}")
    # Provenance records cli:model:effort and independence checks trust it; sessions start fresh.
    forbidden = {"--model", "-m", "--effort", "--resume", "-r", "resume", "--continue",
                 "model", "model_provider", "model_reasoning_effort"}
    if "codex" in clis:
        if any(arg.split("=", 1)[0] == "--ephemeral" for arg in arguments):
            raise AgentError(f"{prefix}{label} must not set --ephemeral; "
                             "the launcher reads Codex usage from its fresh session record")
    if "claude" in clis:
        forbidden.add("-c")  # Claude's --continue; Codex's -c is --config.
        if any(arg.split("=", 1)[0] == "--output-format" for arg in arguments):
            raise AgentError(f"{prefix}{label} must not set --output-format; "
                             "the launcher requires stream-json for Claude process.log")
    if any(arg.removeprefix("--config=").split("=", 1)[0] in forbidden for arg in arguments):
        raise AgentError(f"{prefix}{label} must not change the model, effort or session")
