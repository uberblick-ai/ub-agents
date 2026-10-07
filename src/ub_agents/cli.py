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
from .denials import denial_count
from .errors import AgentError
from .execution import repository_checks
from .github import GitHub
from .help import HelpParser
from .loop import Loop, _GracefulStop
from .launch_log import launch_output
from .labels import configured_labels, provision_labels
from .refresh import control_checkout_checks
from .records import (MARKER, body, iso, latest_leases, lease_by_id, live_leases, own_comment,
                      record_version, records, same_run, timestamp)
from .run_config import supervised_run
from .status import lease_summary, process_details


def parser():
    result = HelpParser(prog="ub-agents", description="project-owned engineering loops on GitHub",
                        usage="%(prog)s <command> [options]")
    result.add_argument("-v", "--version", action="version", version=f"ub-agents {__version__}",
                        help="print the version")
    result.add_argument("--config", metavar="PATH", help="project configuration (default: ub-agents.yaml)")
    next(action for action in result._actions if action.dest == "help").help = (
        "show this help; after a command, that command's help")
    commands = result.add_subparsers(dest="command")
    init = commands.add_parser("init", help="set up this repository: starter configuration, agent instructions and workflow labels",
                               description="Create starter configuration and agent instructions for a project. "
                               "Use when adopting ub-agents; existing starter files are never overwritten.",
                               examples=("ub-agents init --repository org/project",
                                         "ub-agents init --repository org/project --runtime claude:opus:high"))
    init.add_argument("--repository", metavar="OWNER/REPO", help="repository owner/name (otherwise inferred through gh)")
    init.add_argument("--runtime", default="codex:gpt-6.1-sol:high", help="initial cli:model:effort for starter agents")
    check = commands.add_parser("check", help="validate the configuration and instruction files",
                                 description="Validate project configuration, including trusted-bots, and instruction files without executing agents. "
                                 "Use after editing the workflow or before launching it.",
                                 examples=("ub-agents check", "ub-agents check --config workflow.yaml"))
    doctor = commands.add_parser("doctor", help="check the machine, GitHub access, labels and agent runtimes",
                                 overview_options=("--json",),
                                 description="Check machine, GitHub and runtime prerequisites. "
                                 "Create missing workflow labels only after a confirmed interactive prompt. "
                                 "Show warnings and failures with a summary per area by default; "
                                 "use --verbose for every check. "
                                 "Use during setup or to diagnose launch failures; required failures exit nonzero.",
                                 examples=("ub-agents doctor", "ub-agents doctor --verbose", "ub-agents doctor --json"))
    doctor.add_argument("--json", action="store_true", help="emit versioned prerequisite results")
    doctor.add_argument("--verbose", action="store_true", help="show the full per-check list (does not change --json)")
    launch = commands.add_parser("launch", help="run the queue in the foreground, or handle one item",
                                 description="Run the queue in the foreground under the configured gates. Without a number, "
                                 "watch the queue; with a number, handle only that issue or PR, then exit.",
                                 examples=("ub-agents launch", "ub-agents launch --once",
                                           "ub-agents launch 143 --agent implementer"))
    launch.add_argument("number", metavar="NUMBER", type=int, nargs="?", help="run only this item, then exit (optional)")
    launch.add_argument("--agent", metavar="NAME", help="evaluate only this configured agent (needs NUMBER)")
    launch.add_argument("--once", action="store_true", help="observe once, run at most one assignment, then exit")
    launch.add_argument("--no-ui", action="store_true", help="plain lines instead of the terminal view")
    status = commands.add_parser("status", help="matching work, owners, attempts and why items wait",
                                 overview_options=("--json",),
                                 description="Read matching assignments, leases, attempts and reported outcomes. "
                                 "Use to inspect queue progress or why an item is waiting without changing it.",
                                 examples=("ub-agents status", "ub-agents status --json"))
    status.add_argument("--json", action="store_true", help="emit structured status")
    cleanup = commands.add_parser("cleanup", help="preview or remove stale worktrees and branches",
                                  overview_options=("--apply",),
                                  description="Preview stale owned worktrees and local branches. "
                                  "Use after stopped runs; --apply removes eligible artifacts after rechecking ownership.",
                                  examples=("ub-agents cleanup", "ub-agents cleanup --apply"))
    cleanup.add_argument("--apply", action="store_true", help="remove eligible artifacts after rechecking")
    report = commands.add_parser("report", help="record the run's outcome", run_command=True,
                                 description="Record a supervised run's explicit result on GitHub. "
                                 "Use inside the launcher-provided assignment environment; choose --status or --outcome (required). "
                                 "Stop reports (--status blocked or an outcome adding a configured stop label) require --action or --option.",
                                 examples=('ub-agents report --outcome handed-off --summary "Ready for review" --handoff 150',
                                           'ub-agents report --status blocked --summary "Decision pending" '
                                           '--option "Maintainer: use A." --option "Maintainer: use B."'))
    verdict = report.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--outcome", help="declared project outcome; reports success")
    verdict.add_argument("--status", choices=["retry", "blocked"], help="failure verdict; changes no labels")
    report.add_argument("--summary", required=True, help="explain the result in 1–8000 characters")
    report.add_argument("--action", action="append", help="one independent ask that is needed, at most 300 characters; repeat for each ask")
    report.add_argument("--option", action="append", help="one alternative way to unblock, at most 300 characters; repeat with the recommendation first; append : `COMMAND` for a command block")
    report.add_argument("--handoff", type=int, metavar="NUMBER", help="implementation PR number; its head is recorded")
    retrospective = commands.add_parser("retrospective", help="post to the agent's retrospective board", run_command=True,
                                        description="Post a body file as a top-level comment on the agent's configured "
                                        "retrospective discussion. Only available inside a supervised run; use the "
                                        "launcher's literal report_command. The repository and board are pinned by "
                                        "the launcher. Prints the comment URL and does not change the run's outcome.",
                                        examples=("ub-agents retrospective --body-file /run/scratch/retrospective.md",
                                                  'ub-agents retrospective --body-file "/run/scratch/run notes.md"'))
    retrospective.add_argument("--body-file", required=True, metavar="PATH", help="path to a UTF-8 retrospective body file")
    retry = commands.add_parser("retry", help="let stopped work run again, with a recorded reason",
                                description="Record a human-authorized reset of blocked work and attempt limits. "
                                "Use after resolving the cause; stop labels and missing triggers still prevent pickup. "
                                "Without --agent, print and use the first configured agent whose kind applies to the item.",
                                examples=('ub-agents retry 143 --reason "Blocker resolved"',
                                          'ub-agents retry 150 --agent reviewer --reason "Checks restored"'))
    retry.add_argument("--agent", metavar="NAME", help="configured agent (default: first matching item kind)")
    retry.add_argument("--reason", required=True, help="record why the human-authorized reset is justified")
    approve = commands.add_parser("approve", help="record approval of an issue's or PR's current input",
                                  description="Display current issue or PR input and record maintainer approval. "
                                  "Use to clear changed input or outside feedback; requires maintain or admin access and changes no labels.",
                                  examples=("ub-agents approve 143", "ub-agents approve 150"))
    for command in (retry, approve):
        # main requires one number, accepting the hidden alias for one release.
        command.add_argument("number", metavar="NUMBER", type=int, nargs="?", required_for_help=True,
                             help="issue or PR number")
        command.add_argument("--number", dest="legacy_number", type=int, help=argparse.SUPPRESS)
    read = commands.add_parser("read", help="read an issue or PR as filtered JSON", run_command=True,
                               description="Read an open or closed issue or PR under the assignment input trust rules. "
                               "Withheld input is marked or counted; history and permission failures show no item content. "
                               "trusted-bots trusts listed GitHub bot feedback like write, without maintainer authority. "
                               "Inside a supervised run use the launcher's report_command followed by read N; "
                               "its repository and configuration are pinned by the launcher. This command makes no writes.",
                               examples=("ub-agents read 143", "ub-agents read 150 --config workflow.yaml"))
    read.add_argument("number", metavar="NUMBER", type=int, help="issue or PR number in the configured repository")
    for command in (init, check, doctor, launch, status, cleanup, retry, approve, read):
        command.add_argument("--config", dest="command_config", metavar="PATH",
                             help="project configuration (default: ub-agents.yaml)")
    help_command = commands.add_parser("help", overview_hidden=True,
                                       description="Show the command overview or detailed help for a command. "
                                       "Use anywhere without project configuration, GitHub authentication or network access.",
                                       examples=("ub-agents help", "ub-agents help launch"))
    help_command.add_argument("topic", metavar="COMMAND", nargs="?",
                              help="command to describe; omit for the overview (optional)")
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
    runtime_cli = args.runtime.split(":", 1)[0]
    if runtime_cli == "claude":
        targets[config_path] = targets[config_path].replace(
            "[--sandbox, danger-full-access]",
            '[--permission-mode, acceptEdits, --permission-prompts, none, --allowedTools, '
            '"Bash(git *)", "Bash(gh *)", "Bash({report_command} report *)", "Bash({report_command} read *)", --add-dir, "{scratch}"]').replace(
            "Grants full access without the Codex sandbox",
            "Grants unattended edits and git/gh/report commands")
        targets[config_path] = targets[config_path].replace(
            '    # runtime-args:',
            '    # Add the project\'s check commands to --allowedTools: "Bash(<project check command>)".\n'
            '    # runtime-args:')
    for name in ("issue-preparer", "implementer", "reviewer", "integrator"):
        targets[root / ".agents" / f"{name}.md"] = templates.joinpath(f"{name}.md").read_text()
    guidance = runtime_guidance(root, runtime_cli, fallback=True)
    checks = (f"Project checks are documented in `{guidance.relative_to(root)}`."
              if guidance else
              "Replace these placeholders with the project's required commands before launch:\n\n"
              "- Build: `<project build command>`\n"
              "- Tests: `<project test command>`\n"
              "- Other checks: `<project lint or validation command>`")
    targets[root / ".agents" / "ub_agents.md"] = templates.joinpath("ub_agents.md").read_text().replace(
        "{checks}", checks)
    existing = [str(p) for p in targets if p.exists()]
    if existing:
        raise AgentError(f"Starter files already exist; nothing overwritten: {', '.join(existing)}")
    permissions_enabled = False
    if not os.environ.get("CI") and sys.stdin.isatty() and sys.stdout.isatty():
        if runtime_cli == "claude":
            print("Claude starter permissions grant unattended edits plus git, gh and report commands; "
                  "the project's check commands must still be added to --allowedTools.")
        else:
            print("Codex starter permissions grant full access without the sandbox.")
        try:
            answer = input("Enable starter permissions for all four agents? [y/N] ")
        except EOFError:
            answer = ""
        permissions_enabled = answer.strip().casefold() in {"y", "yes"}
    if permissions_enabled:
        targets[config_path] = targets[config_path].replace("    # runtime-args:", "    runtime-args:")
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
    print(f"config: {config_path.name}")
    print("policy: .agents/ub_agents.md (checks, merge policy, decision-makers, review priorities)")
    print("roles: .agents/ (issue-preparer, implementer, reviewer, integrator)")
    steps = ["fill in the checks and policy in .agents/ub_agents.md"]
    if not permissions_enabled:
        steps.append(f"grant agent permissions: uncomment or customize runtime-args in {config_path.name}")
    if runtime_cli == "claude":
        steps.append(f"add check commands to --allowedTools in {config_path.name}")
    steps.extend(["run ub-agents check", "commit and push the starter files", "run ub-agents doctor"])
    print(f"next: {'; '.join(steps)}.")
    for cli in sorted({runtime.cli for agent in config.agents for runtime in agent.runtimes}):
        guidance = runtime_guidance(root, cli)
        if guidance:
            print(f"{cli} loads project guidance from {guidance.relative_to(root)}; kept unchanged.")
        else:
            existing_guidance = runtime_guidance(root, cli, fallback=True)
            if existing_guidance:
                name = existing_guidance.relative_to(root)
                print(f'Warning: {cli} loads no project guidance; checks are documented in {name}. '
                      f'Add a one-line AGENTS.md: "Read {name}".')
            else:
                choices = "AGENTS.md" if cli == "codex" else "CLAUDE.md, .claude/CLAUDE.md or AGENTS.md"
                print(f"Warning: {cli} loads no project guidance; add {choices} so agents know how to build and test.")
    provision_labels(config, GitHub(config.repository))


def runtime_guidance(root, cli, *, fallback=False):
    """Prefer guidance the runtime loads; optionally find another checks source."""
    names = ("AGENTS.md",) if cli == "codex" else ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")
    if fallback and cli == "codex":
        names += ("CLAUDE.md", ".claude/CLAUDE.md")
    return next((root / name for name in names if (root / name).is_file()), None)


def report_run(args):
    context = supervised_run()
    if not args.summary.strip() or len(args.summary) > 8000:
        raise AgentError("summary must contain 1–8000 characters")
    number, lease_id = context["assignment"], context["lease_id"]
    github = GitHub(context["repository"])
    coordinator = Coordinator(github, github.actor(), trusted_bots=context["trusted-bots"])
    # Only inspect the supervised comment's marker and GitHub author. Future
    # payloads are not a contract this build can validate or use for authority.
    comments = github.comments(number)
    supervised = next((c for c in comments if isinstance(c, dict) and c.get("id") == lease_id
                       and own_comment(c, coordinator.actor)), None)
    version = record_version(supervised.get("body")) if supervised else None
    supported = record_version(MARKER)
    if version is not None and version > supported:
        command = os.environ.get("UB_AGENTS_REPORT")
        instruction = f"{command} report (UB_AGENTS_REPORT)" if command else "the launcher's command in UB_AGENTS_REPORT"
        raise AgentError(f"Supervised lease uses record format v{version}, newer than this ub-agents "
                         f"reads (v{supported}). Report with {instruction}.")
    lease = next((r for r in records(comments, trusted=coordinator.trust.observation()) if r["kind"] == "lease"
                  and r["id"] == lease_id and r["run"] == context["run"]), None)
    if lease is None or lease["actor"].casefold() != coordinator.actor.casefold():
        raise AgentError("Supervised lease was not found on GitHub or is not owned by this account")
    record = coordinator.report(lease, args.status or "success", args.summary, args.handoff,
                                outcome=args.outcome, action=args.action, option=args.option)
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


def launch_checks(config, github):
    for _, error in control_checkout_checks(config, github.default_branch()):
        if error is not None:
            raise error
    existing = {name.casefold() for name in github.labels()}
    missing = [label.name for label in configured_labels(config)
               if any(use.required for use in label.uses) and label.name.casefold() not in existing]
    if missing:
        raise AgentError(f"Missing GitHub workflow labels ({', '.join(missing)}); "
                         "run ub-agents doctor for setup commands")


def run(args):
    if args.command == "init":
        init_project(args)
        return
    if args.command == "report":
        report_run(args)
        return
    if args.command == "retrospective":
        from .retrospective import read_body
        policy = supervised_run()
        if policy["retrospectives"] is None:
            raise AgentError(f"No retrospective board configured for agent {policy['agent']}; nothing posted")
        content = read_body(args.body_file)
        print(GitHub(policy["repository"]).create_discussion_comment(policy["retrospectives"], content))
        return
    if args.command == "read":
        from .read_input import read_item, read_policy
        policy = (supervised_run() if "UB_AGENTS_RUN" in os.environ else
                  read_policy(load_config(args.config)))
        print(json.dumps(read_item(GitHub(policy["repository"]), args.number, policy), indent=2))
        return
    if args.command == "doctor":
        from .doctor import Doctor, render, render_check, render_counts
        doctor = Doctor()
        result = doctor.run(args.config)
        render(result, json_output=args.json, verbose=args.verbose)
        if not args.json and doctor.create_missing_labels():
            previous = result["checks"]
            result = doctor.result()
            for check in result["checks"]:
                if check["status"] in {"fail", "warn"} and check not in previous:
                    render_check(check)
            render_counts(result)
        return 0 if result["ok"] else 1
    config = load_config(args.config)
    if args.command == "launch" and args.agent is not None:
        if not any(agent.name == args.agent for agent in config.agents):
            parser().commands()["launch"].error(f"Unknown configured agent: {args.agent}")
    if args.command == "check":
        from .config import instruction_text
        instruction_text(config.root, config.shared_instructions, "shared-instructions")
        for agent in config.agents:
            instruction_text(config.root, agent.instructions, f"{agent.name} instructions")
        print(f"Valid configuration: {config.repository}, {len(config.agents)} agents")
        print(f"Approvals: {config.approvals} (config)" if config.approvals is not None else
              "Approvals: from repository visibility (public: on; private/internal: off)")
        return
    github = GitHub(config.repository)
    actor = None if args.command == "launch" else github.actor()
    if args.command == "approve":
        from .approvals import approve_issue
        created = approve_issue(github, args.number, actor)
        print(f"Approval posted: {created['html_url']}")
        return
    coordinator = Coordinator(github, actor, launchers=config.launchers, trusted_bots=config.trusted_bots)
    if args.command == "retry" and args.agent is None:
        item = github.item(args.number)
        agent = next((agent for agent in config.agents if agent.kind in {item.kind, "either"}), None)
        if agent is None:
            raise AgentError(f"No configured agent applies to {item.kind} #{item.number}")
        args.agent = agent.name
        print(f"Using agent {agent.name} for {item.kind} #{item.number}.")
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
                default_config=args.default_config, interrupt_event=interrupt)
    if args.command == "status":
        now = timestamp()
        rows = status_rows(loop, now)
        if args.json:
            print(json.dumps({"assignments": rows}, indent=2))
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
                    count = denial_count(reported)
                    if count:
                        outcome += f" · {count} denied"
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
    view = None
    try:
        launch_checks(config, github)
        from .updates import Updates
        loop.updates = Updates(config.root)
        if loop.updates is not None:
            loop.updates.start()
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
        if not stop.is_set():
            from .launch_ui import open_view
            session = loop.observer.state['session'] if loop.observer is not None else None
            view = open_view(config.root, session, args.launch_output, stop,
                             no_ui=args.no_ui, poll=loop.request_poll)
            if view is not None:
                loop.enable_poll_now()
        if args.number is not None:
            return loop.launch(once=True, number=args.number, agent_name=args.agent)
        loop.launch(once=args.once)
    except _GracefulStop:
        return
    finally:
        if loop.updates is not None:
            loop.updates.close()
        if view is not None:
            view.close()
        if publisher is not None:
            publisher.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    with ExitStack() as stack:
        try:
            command_line = parser()
            args = command_line.parse_args(argv)
            if args.command is None or args.command == "help":
                if args.command == "help" and args.topic and args.topic not in command_line.commands():
                    command_line.unknown_command(args.topic)
                target = command_line.commands()[args.topic] if args.command == "help" and args.topic else command_line
                target.print_help()
                return 0
            command_parser = command_line.commands()[args.command]
            command_config = getattr(args, "command_config", None)
            if command_config is not None:
                if args.config is not None:
                    command_parser.error("--config may be given before or after the command, not both")
                args.config = command_config
            if args.command in {"approve", "retry"}:
                if args.number is not None and args.legacy_number is not None:
                    command_parser.error(f"{args.command} accepts either N or --number N, not both")
                args.number = args.number if args.number is not None else args.legacy_number
                if args.number is None or args.number < 1:
                    command_parser.error(f"{args.command} requires a positive item number")
            args.default_config = args.config is None
            if args.command == "read" and args.number < 1:
                command_parser.error("read requires a positive item number")
            if args.command == "launch":
                if args.agent is not None and args.number is None:
                    command_parser.error("launch --agent requires an item number")
                if args.number is not None and args.number < 1:
                    command_parser.error("launch requires a positive item number")
            args.config = (Path(args.config or DEFAULT_CONFIG).resolve()
                           if args.command in {"init", "report", "retrospective"} else resolve_config_path(args.config))
            needs_config = args.command in {"check", "status", "launch", "cleanup", "retry", "approve", "read"}
            if args.command == "read" and "UB_AGENTS_RUN" in os.environ:
                needs_config = False  # Supervised reads use the launcher's pinned input policy.
            if needs_config and not args.config.exists():
                name = DEFAULT_CONFIG if args.default_config else str(args.config)
                raise AgentError(f"No {name} here; run ub-agents init to set up a project")
            if args.command == "launch":
                args.launch_output = stack.enter_context(launch_output(args.config.parent))
            return run(args) or 0
        except KeyboardInterrupt:
            print("Stopped; supervised execution terminated", file=sys.stderr)
            return 130
        except (AgentError, OSError) as exc:
            print(f"ub-agents: {exc}", file=sys.stderr)
            return 1
