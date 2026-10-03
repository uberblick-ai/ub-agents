"""The standalone ub-agents command. No Uberblick imports or workspace services."""

import argparse
from contextlib import ExitStack
from importlib.resources import files
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import uuid

from . import __version__
from .config import DEFAULT_CONFIG, load_config, resolve_config_path
from .coordination import Coordinator
from .errors import AgentError
from .execution import repository_checks
from .github import GitHub
from .loop import Loop, _GracefulStop
from .launch_log import launch_output
from .labels import provision_labels
from .records import body, iso, latest_leases, lease_by_id, live_leases, records, same_run, timestamp
from .status import lease_summary, process_details
from .runtime_usage import local_pauses


def parser():
    result = argparse.ArgumentParser(prog="ub-agents", description="Project-owned engineering loops on GitHub")
    result.add_argument("--version", action="version", version=f"ub-agents {__version__}")
    result.add_argument("--config", help="Project configuration (default: ub-agents.yaml)")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Copy customizable starter configuration and instructions")
    init.add_argument("--repository", help="GitHub owner/name (otherwise inferred through gh)")
    init.add_argument("--runtime", default="codex:gpt-6.1-sol:high", help="Initial cli:model:effort for starter agents")
    check = commands.add_parser("check", help="Validate local project configuration without executing agents")
    doctor = commands.add_parser("doctor", help="Check machine, GitHub and runtime prerequisites")
    doctor.add_argument("--json", action="store_true", help="Emit versioned prerequisite results")
    launch = commands.add_parser("launch", help="Run the serial foreground loop")
    launch.add_argument("number", type=int, nargs="?", help="Run only this item, then exit")
    launch.add_argument("--agent", help="Evaluate only this configured agent (requires an item number)")
    launch.add_argument("--once", action="store_true", help="Observe once and execute at most one assignment")
    status = commands.add_parser("status", help="Read current assignments, leases, attempts, and outcomes")
    status.add_argument("--json", action="store_true", help="Emit structured status")
    cleanup = commands.add_parser("cleanup", help="Preview stale owned worktrees and local branches")
    cleanup.add_argument("--apply", action="store_true", help="Remove eligible artifacts after rechecking")
    report = commands.add_parser("report", help="Record a supervised run's explicit outcome on GitHub")
    verdict = report.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--status", choices=["retry", "blocked"], help="Failure verdict; changes no labels")
    verdict.add_argument("--outcome", help="Declared project outcome; reports success")
    report.add_argument("--summary", required=True)
    report.add_argument("--handoff", type=int, help="Implementation PR number; its head is recorded")
    retry = commands.add_parser("retry", help="Record a human-authorized reset of blocked work/attempt limits")
    retry.add_argument("--agent", help="Configured agent (default: first matching item kind)")
    retry.add_argument("--reason", required=True)
    recover = commands.add_parser("recover", help="Recover a stopped local launcher's reported outcome before expiry")
    recover.add_argument("--agent", help="Configured agent (default: first matching item kind)")
    recover.add_argument("--reason", required=True, help="Attest that the launcher has stopped")
    approve = commands.add_parser("approve", help="Approve current issue or PR input as a maintainer")
    for command in (retry, recover, approve):
        command.add_argument("number", type=int, nargs="?", help="Issue or PR number")
        command.add_argument("--number", dest="legacy_number", type=int, help=argparse.SUPPRESS)
    for command in (init, check, doctor, launch, status, cleanup, retry, recover, approve):
        command.add_argument("--config", dest="command_config", metavar="CONFIG",
                             help="Project configuration (default: ub-agents.yaml)")
    return result


def init_project(args):
    config_path = Path(args.config).resolve()
    root = config_path.parent
    root.mkdir(parents=True, exist_ok=True)
    repository = args.repository
    if not repository:
        import subprocess
        process = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=root,
                                 capture_output=True, text=True, timeout=20, check=False)
        if process.returncode:
            raise AgentError("Cannot infer repository; pass --repository owner/name")
        try:
            repository = json.loads(process.stdout)["nameWithOwner"]
        except (ValueError, KeyError) as exc:
            raise AgentError("Unreadable gh repository response") from exc
    templates = files("ub_agents").joinpath("templates")
    targets = {config_path: templates.joinpath("ub-agents.yaml").read_text()
               .replace("your-org/your-project", repository).replace("codex:gpt-6.1-sol:high", args.runtime)}
    if args.runtime.split(":", 1)[0] == "claude":
        targets[config_path] = targets[config_path].replace(
            "[--sandbox, danger-full-access]",
            '[--permission-mode, acceptEdits, --permission-prompts, none, --allowedTools, '
            '"Bash(git *)", "Bash(gh *)", "Bash(ub-agents *)"]').replace(
            "Grants full access without the Codex sandbox",
            "Grants unattended edits and git/gh/report commands")
    for name in ("issue-preparer", "implementer", "reviewer", "integrator"):
        targets[root / ".agents" / f"{name}.md"] = templates.joinpath(f"{name}.md").read_text()
    existing = [str(p) for p in targets if p.exists()]
    if existing:
        raise AgentError(f"Starter files already exist; nothing overwritten: {', '.join(existing)}")
    guidance = root / "AGENTS.md"
    if guidance.exists():
        print(f"Kept existing {guidance}; shared guidance was not modified.")
    else:
        targets[guidance] = templates.joinpath("AGENTS.md").read_text()
    for target, content in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x") as stream:
            stream.write(content)
    ignore = root / ".gitignore"
    if not ignore.exists() or ".ub-agents/" not in ignore.read_text().splitlines():
        with ignore.open("a") as stream:
            stream.write("\n# Disposable ub-agents execution artifacts\n.ub-agents/\n")
    # Validate even the generated configuration; errors are actionable before launch.
    config = load_config(config_path)
    print(f"Created {config_path} and .agents instructions; shared guidance is in {guidance}. "
          "Customize and commit them before launch.")
    provision_labels(config, GitHub(config.repository))


def report_run(args):
    env = {key: os.environ.get(f"UB_AGENTS_{key}")
           for key in ("REPOSITORY", "ASSIGNMENT", "RUN", "LEASE_ID")}
    if any(not value for value in env.values()):
        raise AgentError("report requires the environment of a supervised ub-agents assignment")
    if not args.summary.strip() or len(args.summary) > 8000:
        raise AgentError("summary must contain 1–8000 characters")
    try:
        number = int(env["ASSIGNMENT"])
        lease_id = int(env["LEASE_ID"])
    except ValueError as exc:
        raise AgentError("Invalid supervised assignment environment") from exc
    github = GitHub(env["REPOSITORY"])
    coordinator = Coordinator(github, github.actor())
    lease = next((r for r in coordinator.history(number) if r["kind"] == "lease"
                  and r["id"] == lease_id and r["run"] == env["RUN"]), None)
    if lease is None or lease["actor"].casefold() != coordinator.actor.casefold():
        raise AgentError("Supervised lease was not found on GitHub or is not owned by this account")
    record = coordinator.report(lease, args.status or "success", args.summary, args.handoff,
                                outcome=args.outcome)
    print(json.dumps({"run": record["run"], "status": record["status"], "url": record["url"]}))


def status_rows(loop, now=None):
    now = timestamp() if now is None else now
    host = socket.gethostname()
    processes = {}
    rows = []
    for plan in loop.plans():
        history = plan.history
        active = live_leases(history, now)
        latest = latest_leases(history).get((plan.item.number, plan.agent.name))
        lease = active[0] if active else None
        process, process_reason = None, None
        if lease:
            if lease["id"] not in processes:
                processes[lease["id"]] = process_details(lease, history, now, host)
            process, process_reason = processes[lease["id"]]
        source = next((r for r in active if r["agent"] == plan.agent.name), None) or latest
        if source and source.get("mode") == "recovery":
            source = lease_by_id(history, source.get("recovered_lease_id"))
        outcomes = [r for r in history if source and r["kind"] == "outcome" and r["agent"] == plan.agent.name
                    and r["lease_id"] == source["id"] and same_run(r, source)]
        rows.append({"number": plan.item.number, "kind": plan.item.kind, "agent": plan.agent.name,
                     "priority": plan.priority, "priority_inherited_from": plan.priority_source,
                     "priority_from_issue": plan.priority_from_issue,
                     "milestone": plan.milestone, "milestone_inherited_from": plan.milestone_source,
                     "open_blockers": list(plan.blockers),
                     "state": plan.state, "reason": plan.reason, "attempts": plan.attempt - 1,
                     "runtime": plan.runtime.name if plan.runtime else None,
                     "candidate_sha": plan.item.head,
                     "result": latest.get("result") if latest else None,
                     "lease": lease,
                     "process": process, "process_reason": process_reason,
                     "outcome": outcomes[-1] if outcomes else None})
    return rows


def retry_next_step(item, agent, stop_labels):
    if item.state == "closed":
        return f"#{item.number} is closed."
    stops = [label for label in stop_labels if label in item.labels]
    triggers = [label for label in agent.triggers if label in item.labels]
    if stops:
        plural = len(stops) > 1
        message = (f"#{item.number} has stop label{'s' if plural else ''} {', '.join(stops)}; "
                   f"it stays parked until {'the labels are' if plural else 'the label is'} removed.")
        if not triggers:
            message += (f" One of {agent.name}'s trigger labels must also be added: "
                        f"{', '.join(agent.triggers)}.")
        return message
    if not triggers:
        return (f"#{item.number} won't run until one of {agent.name}'s trigger labels is added: "
                f"{', '.join(agent.triggers)}.")
    return (f"#{item.number} has trigger label{'s' if len(triggers) > 1 else ''} {', '.join(triggers)}; "
            "a running launcher picks it up on its next poll. `ub-agents status` shows its progress.")


def run(args):
    if args.command == "init":
        init_project(args)
        return
    if args.command == "report":
        report_run(args)
        return
    if args.command == "doctor":
        from .doctor import diagnose, render
        result = diagnose(args.config)
        render(result, json_output=args.json)
        return 0 if result["ok"] else 1
    config = load_config(args.config)
    if args.command == "launch" and args.agent is not None:
        if not any(agent.name == args.agent for agent in config.agents):
            parser().error(f"Unknown configured agent: {args.agent}")
    if args.command == "check":
        from .config import instruction_text
        for agent in config.agents:
            instruction_text(config.root, agent.instructions, f"{agent.name} instructions")
        print(f"Valid configuration: {config.repository}, {len(config.agents)} agents")
        return
    github = GitHub(config.repository)
    actor = None if args.command == "launch" else github.actor()
    if args.command == "approve":
        from .approvals import approve_issue
        created = approve_issue(github, args.number, actor)
        print(f"Approval posted: {created['html_url']}")
        return
    coordinator = Coordinator(github, actor, launchers=config.launchers)
    if args.command in {"retry", "recover"} and args.agent is None:
        item = github.item(args.number)
        agent = next((agent for agent in config.agents if agent.kind in {item.kind, "either"}), None)
        if agent is None:
            raise AgentError(f"No configured agent applies to {item.kind} #{item.number}")
        args.agent = agent.name
        print(f"Using agent {agent.name} for {item.kind} #{item.number}.")
    if args.command == "recover":
        from .recovery import recover_run
        recover_run(config, github, actor, args.number, args.agent, args.reason)
        return
    if args.command == "cleanup":
        from .cleanup import Cleaner
        for _, error in repository_checks(config):
            if error is not None:
                raise error
        Cleaner(config, github, actor).clean(apply=args.apply)
        return
    if args.command == "retry":
        agent = next((agent for agent in config.agents if agent.name == args.agent), None)
        if agent is None:
            raise AgentError("Unknown configured agent")
        if not args.reason.strip() or args.number < 1:
            raise AgentError("retry requires a positive item number and a reason")
        history = coordinator.history(args.number)
        if live_leases(history, timestamp()):
            raise AgentError("Cannot reset attempts while an assignment is owned")
        item = github.item(args.number)
        record = {"kind": "reset", "run": uuid.uuid4().hex,
                  "agent": args.agent, "actor": actor, "runtime": "operator",
                  "assignment": item.number, "assignment_sha": item.head, "created": iso(timestamp()), "summary": args.reason}
        created = records([github.create_comment(item.number, body(record))])[0]
        coordinator.notices.resumed(item.number)
        print(f"Reset {args.agent} attempts on #{item.number}: {created['url']}")
        print(retry_next_step(item, agent, config.stop_labels))
        return
    stop = threading.Event()
    interrupt = threading.Event()
    loop = Loop(config, github, actor, stop,
                config_path=config.root / DEFAULT_CONFIG if args.default_config else args.config,
                default_config=args.default_config, interrupt_event=interrupt,
                usage_read_only=args.command == "status")
    if args.command == "status":
        now = timestamp()
        rows = status_rows(loop, now)
        pauses = local_pauses(config.root, now)
        if args.json:
            print(json.dumps({"assignments": rows, "runtime_pauses": pauses}, indent=2))
        elif not rows:
            print("No configured triggers match open GitHub work")
        else:
            for row in rows:
                owner = (f" · {lease_summary(row['lease'], now)}"
                         if row["lease"] else "")
                outcome = ""
                if row["outcome"]:
                    reported = row["outcome"]
                    acceptance = " (unaccepted)" if reported["status"] == "success" and not reported["accepted"] else ""
                    outcome = f" · reported: {reported['status']}{acceptance}"
                verdict = f" · last result: {row['result']}" if row["result"] else ""
                priority = row["priority"] or "none"
                if row["priority_inherited_from"] is not None:
                    priority += f" (inherited from #{row['priority_inherited_from']})"
                elif row["priority_from_issue"] is not None:
                    priority += f" (from closed issue #{row['priority_from_issue']})"
                milestone = ""
                if config.queue.milestones == "order" and row["kind"] == "issue":
                    value = f"#{row['milestone']}" if row["milestone"] is not None else "none"
                    milestone = f" · milestone {value}"
                    if row["milestone_inherited_from"] is not None:
                        milestone += f" (inherited from #{row['milestone_inherited_from']})"
                state = "running" if row["state"] == "owned" and row["process"] == "running" else row["state"]
                print(f"#{row['number']} {row['agent']}: {state} · priority {priority}{milestone} · attempts {row['attempts']}{owner}{verdict}{outcome}")
                print(f"  {row['process_reason'] or row['reason']}")
        if not args.json:
            for pause in pauses:
                print(f"{pause['cli']} paused: {pause['reason']}; pause ends {pause['ends_at']}")
        return
    for _, error in repository_checks(config):
        if error is not None:
            raise error
    local = config.root / ".ub-agents"
    local.mkdir(mode=0o700, exist_ok=True)
    def stop_now(*_):
        interrupt.set()
        stop.set()

    handlers = {sig: signal.signal(sig, stop_now)
                for sig in (signal.SIGINT, signal.SIGHUP)}
    handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, lambda *_: loop.stop_gracefully())
    from .observations import Observations, Publisher
    publisher = None
    try:
        try:
            publisher = Publisher(config.root, output=loop.output)
            loop._before_claim()
            loop.observer = Observations(config, actor, loop.config_path, publisher,
                                         clock=loop.coordinator.clock)
        except _GracefulStop:
            raise
        except Exception as exc:
            if publisher is not None:
                publisher.warning(str(exc))
            else:
                loop.output(f"Cannot publish launcher observations: {exc}")
        if args.number is not None:
            return loop.launch(once=True, number=args.number, agent_name=args.agent)
        loop.launch(once=args.once)
    except _GracefulStop:
        return
    finally:
        if publisher is not None:
            publisher.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    with ExitStack() as stack:
        try:
            args = parser().parse_args(argv)
            command_config = getattr(args, "command_config", None)
            if command_config is not None:
                if args.config is not None:
                    parser().error("--config may be given before or after the command, not both")
                args.config = command_config
            if args.command in {"approve", "retry", "recover"}:
                if args.number is not None and args.legacy_number is not None:
                    parser().error(f"{args.command} accepts either N or --number N, not both")
                args.number = args.number if args.number is not None else args.legacy_number
                if args.number is None or args.number < 1:
                    parser().error(f"{args.command} requires a positive item number")
            args.default_config = args.config is None
            if args.command == "launch":
                if args.agent is not None and args.number is None:
                    parser().error("launch --agent requires an item number")
                if args.number is not None and args.number < 1:
                    parser().error("launch requires a positive item number")
                stack.enter_context(launch_output(Path(args.config or DEFAULT_CONFIG).resolve().parent))
            args.config = (Path(args.config or DEFAULT_CONFIG).resolve()
                           if args.command in {"init", "report"} else resolve_config_path(args.config))
            return run(args) or 0
        except KeyboardInterrupt:
            print("Stopped; supervised execution terminated", file=sys.stderr)
            return 130
        except (AgentError, OSError) as exc:
            print(f"ub-agents: {exc}", file=sys.stderr)
            return 1
