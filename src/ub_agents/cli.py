"""The standalone ub-agents command. No Uberblick imports or workspace services."""

import argparse
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import uuid

from . import __version__
from .attention import attention_details, waiting_time
from .config import DEFAULT_CONFIG, LOCAL_CONFIG, load_config, resolve_config_path
from .coordination import Coordinator
from .denials import denial_count
from .errors import (AgentError, CheckoutRefreshError, CheckoutSetupInterrupted, CleanupError,
                     GitHubError, InstructionError)
from .execution import repository_checks
from .github import GitHub
from .help import CommentHelpFormatter, HelpParser, InitHelpFormatter
from .loop import Loop, _GracefulStop
from .init_setup import runtime_guidance
from .launch_log import launch_output
from .labels import configured_labels, print_commands
from .refresh import control_checkout_checks
from .records import (MARKER, body, iso, latest_leases, lease_by_id, live_leases, own_comment,
                      record_version, records, same_run, timestamp)
from .run_config import supervised_run
from .status import lease_summary, process_details
from .startup_cleanup import sweep as startup_cleanup


def parser():
    result = HelpParser(prog="ub-agents", description="project-owned engineering loops on GitHub",
                        usage="%(prog)s <command> [options]")
    result.add_argument("-v", "--version", action="version", version=f"ub-agents {__version__}",
                        help="print the version")
    result.add_argument("--config", metavar="PATH", help="project configuration (default: ub-agents.yaml)")
    next(action for action in result._actions if action.dest == "help").help = (
        "show this help; after a command, that command's help")
    commands = result.add_subparsers(dest="command", prog="ub-agents")
    init = commands.add_parser("init", help="set up this repository: starter configuration, agent instructions and workflow labels",
                               formatter_class=InitHelpFormatter,
                               description="Guide terminal setup through a wizard with a review before Apply. "
                               "Use --use-defaults for unattended setup; CI/non-TTY also uses defaults. "
                               "Existing starter files will never be overwritten.",
                               examples=("ub-agents init",
                                         "ub-agents init --repository org/project --runtime claude:claude-opus-5-5:high"))
    init.add_argument("--repository", metavar="OWNER/REPO", help="repository owner/name (otherwise inferred through gh)")
    init.add_argument("--runtime", default="codex:gpt-6.1-sol:high", help="initial cli:model:effort for starter agents")
    init.add_argument("--use-defaults", action="store_true", help="unattended starters, even in a terminal")
    doctor = commands.add_parser("doctor", help="check the machine, GitHub access, labels and agent runtimes",
                                 formatter_class=CommentHelpFormatter,
                                 overview_options=("--json",),
                                 description="Check machine, GitHub and runtime prerequisites. "
                                 "Use --offline for local validation without GitHub or agent runtimes. "
                                 "Create missing workflow labels only after a confirmed interactive prompt. "
                                 "Show warnings and failures with a summary per area by default; "
                                 "required failures exit nonzero.",
                                 examples=("ub-agents doctor", "ub-agents doctor --offline", "ub-agents doctor --json"))
    doctor.add_argument("--offline", action="store_true", help="validate locally without GitHub or agent runtimes")
    doctor.add_argument("--json", action="store_true", help="emit versioned prerequisite results")
    doctor.add_argument("--verbose", action="store_true", help="show the full per-check list (does not change --json)")
    launch = commands.add_parser("launch", help="run the queue in the foreground, or handle one item",
                                 formatter_class=CommentHelpFormatter,
                                 description="Run the queue in the foreground under the configured gates. Without a number, "
                                 "watch the queue; with a number, handle only that issue or PR, then exit. "
                                 "Use --agent NAME to serve only one configured agent; without a number, "
                                 "the queue keeps polling until you stop it.",
                                 examples=("ub-agents launch", "ub-agents launch --agent issue-preparer",
                                           "ub-agents launch 143 --agent implementer"))
    launch.add_argument("number", metavar="NUMBER", type=int, nargs="?", help="run only this item, then exit (optional)")
    launch.add_argument("--agent", metavar="NAME", help="serve only this configured agent (queue or NUMBER)")
    launch.add_argument("--once", action="store_true", help="observe once, run at most one assignment, then exit")
    launch.add_argument("--no-ui", action="store_true", help="plain lines instead of the terminal view")
    status = commands.add_parser("status", help="matching work, owners, attempts and why items wait",
                                 overview_options=("--json",),
                                 description="Read matching assignments, leases, attempts and reported outcomes. "
                                 "With a number, explain only that issue or PR, even without a matching trigger. "
                                 "Use to inspect queue progress or why an item is waiting without changing it.",
                                 examples=("ub-agents status", "ub-agents status 143", "ub-agents status 143 --json"))
    status.add_argument("number", metavar="NUMBER", type=int, nargs="?", help="inspect only this item (optional)")
    status.add_argument("--json", action="store_true", help="emit structured status")
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
    # main requires one number, accepting the hidden alias for one release.
    retry.add_argument("number", metavar="NUMBER", type=int, nargs="?", required_for_help=True,
                       help="issue or PR number")
    retry.add_argument("--number", dest="legacy_number", type=int, help=argparse.SUPPRESS)
    read = commands.add_parser("read", help="read an issue or PR as filtered JSON", run_command=True,
                               description="Read an open or closed issue or PR under the assignment input trust rules. "
                               "Withheld input is marked or counted; history and permission failures show no item content. "
                               "trusted-bots trusts listed GitHub bot feedback like write, without maintainer authority. "
                               "Inside a supervised run use the launcher's report_command followed by read N; "
                               "its repository and configuration are pinned by the launcher. This command makes no writes.",
                               examples=("ub-agents read 143", "ub-agents read 150 --config workflow.yaml"))
    read.add_argument("number", metavar="NUMBER", type=int, help="issue or PR number in the configured repository")
    for command in (init, doctor, launch, status, retry, read):
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
    from .init_setup import (Choices, apply_plan, guidance_summary, infer_repository, make_plan,
                             next_steps, recovery_content)
    path = Path(args.config).absolute()
    repository = args.repository or infer_repository(path.parent)
    plan = make_plan(path, repository, Choices(args.runtime))
    interactive = (not args.use_defaults and not os.environ.get("CI")
                   and sys.stdin.isatty() and sys.stdout.isatty())
    if interactive:
        from .init_ui import run_wizard
        from .init_wizard import Wizard
        return run_wizard(Wizard(plan, GitHub(plan.config.repository)))
    result = apply_plan(plan, GitHub(plan.config.repository))
    print(f"config: {path.name}")
    print("policy: .agents/ub_agents.md (checks, merge policy, decision-makers, review priorities)")
    print("roles: .agents/ (issue-preparer, implementer, reviewer, integrator)")
    print("Defaults: starter runtime; permissions deferred; priority off; retrospectives off; "
          "Uberblick integration off; no GitHub labels created.")
    for message in guidance_summary(plan):
        print(message)
    if not result.ok:
        print("Setup partially failed; completed effects remain:")
        for step in result.completed:
            print(step)
        print(result.error)
        for step in result.incomplete:
            print(step)
        print("Remaining work (complete manually; init refuses existing starters):")
        for step in result.remaining:
            print(step)
        print(recovery_content(plan, result))
    else:
        print("Setup completed; launch readiness requires review of project checks and authority.")
    for step in next_steps(plan):
        print(f"next: {step}")
    print_commands(configured_labels(plan.config), plan.config.repository,
                   "Unattended defaults; GitHub labels were not read or written")
    return 0 if result.ok else 1


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


def status_rows(loop, now=None, *, plans=None):
    now = timestamp() if now is None else now
    host = socket.gethostname()
    processes = {}
    rows = []
    if plans is None:
        plans = loop.plans()
    for plan in plans:
        history = plan.history
        notice = plan.attention_notice if plan.state in {"parked", "blocked", "failed"} else None
        attention = {"notice_url": notice.get("html_url") if notice else None,
                     "waiting_since": None, "stop_labels": [], "attention_reason": None}
        attention.update(attention_details(plan, loop.config.stop_labels,
                         {"text": notice["body"], "created_at": notice.get("created_at")} if notice else None))
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
                     "outcome": outcomes[-1] if outcomes else None, **attention})
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


def launch_checks(config, github, read_git=None):
    for _, error in control_checkout_checks(config, github.default_branch(), read_git):
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
        return init_project(args)
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
        result = doctor.run(args.config, offline=args.offline)
        render(result, json_output=args.json, verbose=args.verbose)
        if not args.offline and not args.json and doctor.create_missing_labels():
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
            names = ', '.join(agent.name for agent in config.agents)
            parser().commands()["launch"].error(f"Unknown configured agent: {args.agent}; configured agents: {names}")
    github = GitHub(config.repository)
    actor = None if args.command == "launch" else github.actor()
    coordinator = Coordinator(github, actor, launchers=config.launchers, trusted_bots=config.trusted_bots)
    if args.command == "retry" and args.agent is None:
        item = github.item(args.number)
        agent = next((agent for agent in config.agents if agent.kind in {item.kind, "either"}), None)
        if agent is None:
            raise AgentError(f"No configured agent applies to {item.kind} #{item.number}")
        args.agent = agent.name
        print(f"Using agent {agent.name} for {item.kind} #{item.number}.")
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
        explanation = None
        if args.number is None:
            rows = status_rows(loop, now)
        else:
            item, plans = loop.item_plans(args.number)
            scoped = (replace(plan, priority=config.queue.priority.effective(item.labels),
                              milestone=item.milestone) for plan in plans)
            rows = status_rows(loop, now, plans=scoped)
            if not rows:
                explanation = loop.item_explanation(item)
        if args.json:
            result = {"assignments": rows}
            if loop.milestone_skips:
                result["milestone_skips"] = list(loop.milestone_skips)
            if explanation is not None:
                result["explanation"] = explanation
            print(json.dumps(result, indent=2))
        elif explanation is not None:
            print(f"#{args.number}: {explanation}")
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
                if config.queue.milestones in {"order", "gate"} and row["kind"] == "issue":
                    value = f"#{row['milestone']}" if row["milestone"] is not None else "none"
                    milestone = f" · milestone {value}"
                    if row["milestone_inherited_from"] is not None:
                        milestone += f" (inherited from #{row['milestone_inherited_from']})"
                state = "running" if row["state"] == "owned" and row["process"] == "running" else row["state"]
                print(f"#{row['number']} {row['agent']}: {state} · priority {priority}{milestone} · attempts {row['attempts']}{owner}{verdict}{outcome}")
                print(f"  {row['process_reason'] or row['reason']}")
                if row["notice_url"]:
                    waiting = waiting_time(row["waiting_since"], now)
                    suffix = f" · waiting {waiting}" if waiting else ""
                    print(f"  Action needed: {row['notice_url']}{suffix}")
        if not args.json:
            for line in loop.milestone_skips:
                print(line)
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
        loop._before_claim()
        session = loop.observer.state['session'] if loop.observer is not None else None
        swept = startup_cleanup(config, github, loop.coordinator.actor, output=loop.output, session=session)
        loop._observe("startup_cleanup", swept)
        if not stop.is_set():
            from .launch_ui import open_view
            view = open_view(config.root, session, args.launch_output, stop,
                             no_ui=args.no_ui, poll=loop.request_poll)
            if view is not None:
                loop.enable_poll_now()
        if args.number is not None:
            loop.refusal_output = args.launch_output.refusals
            return loop.launch(once=True, number=args.number, agent_name=args.agent)
        if args.agent is not None:
            return loop.launch(once=args.once, agent_name=args.agent)
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


def launch_error_message(exc, root):
    # Request reads and continuous polling can wrap the original failure.
    # A failed fallback signal can also mask CleanupError with an OSError.
    errors = []
    error = exc
    while error is not None and error not in errors:
        errors.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    refresh = next((error for error in errors if isinstance(error, CheckoutRefreshError)), None)
    cleanup = next((error for error in errors if isinstance(error, CleanupError)), None)
    if refresh is not None:
        detail = str(refresh)
    elif cleanup is not None:
        detail = f"{cleanup}; {cleanup.next_step}, then launch again"
    elif any(isinstance(error, GitHubError) for error in errors):
        detail = str(exc)
        advice = "Fix the cause and restart ub-agents launch."
        if not detail.endswith(advice):
            detail = f"{detail}; {advice}"
    elif any(isinstance(error, InstructionError) for error in errors):
        detail = f"{root}: {exc}; restore readable, valid instruction files and launch again"
    else:
        return str(exc)
    return " ".join(detail.split())


def main(argv=None):
    with ExitStack() as stack:
        args = None
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
            if args.command == "retry":
                if args.number is not None and args.legacy_number is not None:
                    command_parser.error(f"{args.command} accepts either N or --number N, not both")
                args.number = args.number if args.number is not None else args.legacy_number
                if args.number is None or args.number < 1:
                    command_parser.error(f"{args.command} requires a positive item number")
            args.default_config = args.config is None
            if args.command == "read" and args.number < 1:
                command_parser.error("read requires a positive item number")
            if args.command == "status" and args.number is not None and args.number < 1:
                command_parser.error("status requires a positive item number")
            if args.command == "launch":
                if args.number is not None and args.number < 1:
                    command_parser.error("launch requires a positive item number")
            args.config = (Path(args.config or DEFAULT_CONFIG).absolute() if args.command == "init" else
                           Path(args.config or DEFAULT_CONFIG).resolve() if args.command in {"report", "retrospective"}
                           else resolve_config_path(args.config))
            needs_config = args.command in {"status", "launch", "retry", "read"}
            if args.command == "read" and "UB_AGENTS_RUN" in os.environ:
                needs_config = False  # Supervised reads use the launcher's pinned input policy.
            if needs_config and not args.config.exists():
                name = DEFAULT_CONFIG if args.default_config else str(args.config)
                raise AgentError(f"No {name} here; run ub-agents init to set up a project")
            if args.command == "launch":
                args.launch_output = stack.enter_context(launch_output(args.config.parent))
                from .launcher_code import startup_copy
                stack.enter_context(startup_copy())
            return run(args) or 0
        except KeyboardInterrupt as exc:
            detail = (f"ub-agents: {exc}" if isinstance(exc, CheckoutSetupInterrupted)
                      else "Stopped; supervised execution terminated")
            print(detail, file=sys.stderr)
            return 130
        except (AgentError, OSError) as exc:
            detail = str(exc)
            if args is not None and args.command == "launch" and isinstance(args.config, Path):
                detail = launch_error_message(exc, args.config.parent)
            print(f"ub-agents: {detail}", file=sys.stderr)
            return 1
