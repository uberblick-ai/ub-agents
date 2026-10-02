"""The standalone ub-agent command. No Uberblick imports or workspace services."""

import argparse
from importlib.resources import files
import json
import os
from pathlib import Path
import signal
import sys
import threading
import uuid

from . import __version__
from .config import load_config
from .coordination import Coordinator
from .errors import AgentError, RecordError
from .execution import repository_checks
from .github import GitHub
from .loop import Loop
from .labels import provision_labels
from .records import body, iso, latest_leases, live_leases, records, timestamp


def parser():
    result = argparse.ArgumentParser(prog="ub-agent", description="Project-owned engineering loops on GitHub")
    result.add_argument("--version", action="version", version=f"ub-agent {__version__}")
    result.add_argument("--config", default="ub-agent.yaml", help="Project configuration (default: ub-agent.yaml)")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Copy customizable starter configuration and instructions")
    init.add_argument("--repository", help="GitHub owner/name (otherwise inferred through gh)")
    init.add_argument("--runtime", default="codex:gpt-6.1-sol:high", help="Initial cli:model:effort for starter agents")
    commands.add_parser("check", help="Validate local project configuration without executing agents")
    doctor = commands.add_parser("doctor", help="Check machine, GitHub and runtime prerequisites")
    doctor.add_argument("--json", action="store_true", help="Emit versioned prerequisite results")
    launch = commands.add_parser("launch", help="Run the serial foreground loop")
    launch.add_argument("--once", action="store_true", help="Observe once and execute at most one assignment")
    status = commands.add_parser("status", help="Read current assignments, leases, attempts, and outcomes")
    status.add_argument("--json", action="store_true", help="Emit structured status")
    cleanup = commands.add_parser("cleanup", help="Preview stale owned worktrees and local branches")
    cleanup.add_argument("--apply", action="store_true", help="Remove eligible artifacts after rechecking")
    report = commands.add_parser("report", help="Record a supervised run's explicit outcome on GitHub")
    verdict = report.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--status", choices=["success", "retry", "blocked"])
    verdict.add_argument("--outcome", help="Declared project outcome; reports success")
    report.add_argument("--summary", required=True)
    report.add_argument("--handoff", type=int, help="Implementation PR number; its head is recorded")
    retry = commands.add_parser("retry", help="Record a human-authorized reset of blocked work/attempt limits")
    retry.add_argument("--number", type=int, required=True)
    retry.add_argument("--agent", required=True)
    retry.add_argument("--reason", required=True)
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
    targets = {config_path: templates.joinpath("ub-agent.yaml").read_text()
               .replace("your-org/your-project", repository).replace("codex:gpt-6.1-sol:high", args.runtime)}
    if args.runtime.split(":", 1)[0] == "claude":
        targets[config_path] = targets[config_path].replace(
            "[--sandbox, danger-full-access]",
            '[--permission-mode, acceptEdits, --permission-prompts, none, --allowedTools, '
            '"Bash(git *)", "Bash(gh *)", "Bash(ub-agent *)"]').replace(
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
    if not ignore.exists() or ".ub-agent/" not in ignore.read_text().splitlines():
        with ignore.open("a") as stream:
            stream.write("\n# Disposable ub-agent execution artifacts\n.ub-agent/\n")
    # Validate even the generated configuration; errors are actionable before launch.
    config = load_config(config_path)
    print(f"Created {config_path} and .agents instructions; shared guidance is in {guidance}. "
          "Customize and commit them before launch.")
    provision_labels(config, GitHub(config.repository))


def report_run(args):
    required = ["UB_AGENT_REPOSITORY", "UB_AGENT_ASSIGNMENT", "UB_AGENT_RUN", "UB_AGENT_LEASE_ID"]
    if any(not os.environ.get(key) for key in required):
        raise AgentError("report requires the environment of a supervised ub-agent assignment")
    if not args.summary.strip() or len(args.summary) > 8000:
        raise AgentError("summary must contain 1–8000 characters")
    try:
        number = int(os.environ["UB_AGENT_ASSIGNMENT"])
        lease_id = int(os.environ["UB_AGENT_LEASE_ID"])
    except ValueError as exc:
        raise AgentError("Invalid supervised assignment environment") from exc
    github = GitHub(os.environ["UB_AGENT_REPOSITORY"])
    try:
        operators = json.loads(os.environ.get("UB_AGENT_OPERATORS", "[]"))
        if not isinstance(operators, list) or any(not isinstance(login, str) for login in operators):
            raise ValueError("expected operator logins")
    except ValueError as exc:
        raise AgentError("Invalid supervised operator environment") from exc
    coordinator = Coordinator(github, github.actor(), trusted_actors=operators)
    lease = next((r for r in coordinator.history(number) if r["kind"] == "lease"
                  and r["id"] == lease_id and r["run"] == os.environ["UB_AGENT_RUN"]), None)
    if lease is None:
        raise AgentError("Supervised lease was not found on GitHub")
    if lease["actor"].casefold() != coordinator.actor.casefold():
        raise AgentError("Authenticated GitHub actor does not own this run")
    record = coordinator.report(lease, args.status or "success", args.summary, args.handoff,
                                outcome=args.outcome)
    print(json.dumps({"run": record["run"], "status": record["status"], "url": record["url"]}))


def status_rows(loop):
    rows = []
    for plan in loop.plans():
        try:
            history = loop.coordinator.history(plan.item.number)
        except RecordError:
            history = []  # The plan already displays the item's coordination error.
        active = live_leases(history, timestamp())
        latest = latest_leases(history).get((plan.item.number, plan.agent.name))
        outcomes = [r for r in history if r["kind"] == "outcome" and r["agent"] == plan.agent.name]
        rows.append({"number": plan.item.number, "kind": plan.item.kind, "agent": plan.agent.name,
                     "priority": plan.priority, "priority_inherited_from": plan.priority_source,
                     "priority_from_issue": plan.priority_from_issue,
                     "open_blockers": list(plan.blockers),
                     "state": plan.state, "reason": plan.reason, "attempts": plan.attempt - 1,
                     "runtime": plan.runtime.name if plan.runtime else None,
                     "candidate_sha": plan.item.head,
                     "result": latest.get("result") if latest else None,
                     "lease": active[0] if active else None,
                     "outcome": outcomes[-1] if outcomes else None})
    return rows


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
    if args.command == "check":
        print(f"Valid configuration: {config.repository}, {len(config.agents)} agents")
        return
    github = GitHub(config.repository)
    actor = github.actor()
    coordinator = Coordinator(github, actor, trusted_actors=config.operators)
    if args.command == "cleanup":
        from .cleanup import Cleaner
        for _, error in repository_checks(config):
            if error is not None:
                raise error
        Cleaner(config, github, actor).clean(apply=args.apply)
        return
    if args.command == "retry":
        if args.agent not in {agent.name for agent in config.agents}:
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
        print(json.dumps({"agent": args.agent, "number": item.number, "url": created["url"]}))
        return
    stop = threading.Event()
    loop = Loop(config, github, actor, stop)
    if args.command == "status":
        rows = status_rows(loop)
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No configured triggers match open GitHub work")
        else:
            for row in rows:
                owner = (f" · @{row['lease']['actor']} until {row['lease']['expires']}"
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
                print(f"#{row['number']} {row['agent']}: {row['state']} · priority {priority} · attempts {row['attempts']}{owner}{verdict}{outcome}")
                print(f"  {row['reason']}")
        return
    for _, error in repository_checks(config):
        if error is not None:
            raise error
    local = config.root / ".ub-agent"
    local.mkdir(mode=0o700, exist_ok=True)
    handlers = {sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        loop.launch(once=args.once)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    try:
        return run(parser().parse_args(argv)) or 0
    except KeyboardInterrupt:
        print("Stopped; supervised execution terminated", file=sys.stderr)
        return 130
    except (AgentError, OSError) as exc:
        print(f"ub-agent: {exc}", file=sys.stderr)
        return 1
