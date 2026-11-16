"""Reset attempts and restore triggers through the registered retry command."""

import argparse

from ..errors import AgentError
from ..records import body, iso, live_leases, records, timestamp
from ._registry import command


def arguments(commands):
    retry = commands.add_parser("retry", help="let stopped work run again, with a recorded reason",
                                description="Record a human-authorized reset of blocked work and attempt limits. "
                                "Use after resolving the cause; on open items it then removes stop labels and restores the agent's trigger. "
                                "Without --agent, print and use the first configured agent whose kind applies and whose trigger is on the item. "
                                "If none has a trigger on the item, use the first agent whose kind applies.",
                                examples=('ub-agents retry 143 --reason "Blocker resolved"',
                                          'ub-agents retry 150 --agent reviewer --reason "Checks restored"'))
    retry.add_argument("--agent", metavar="NAME",
                       help="configured agent (default: first matching item kind and trigger, else first matching kind)")
    retry.add_argument("--reason", required=True, help="record why the human-authorized reset is justified")
    # main requires one number, accepting the hidden alias for one release.
    retry.add_argument("number", metavar="NUMBER", type=int, nargs="?", required_for_help=True,
                       help="issue or PR number")
    retry.add_argument("--number", dest="legacy_number", type=int, help=argparse.SUPPRESS)
    return retry


def retry_labels(github, item, agent, stop_labels):
    if item.state == "closed":
        return f"#{item.number} is closed."
    stops = [label for label in stop_labels if label in item.labels]
    added = [] if any(label in item.labels for label in agent.triggers) else [agent.triggers[0]]
    remaining = list(stops)
    try:
        for label in stops:
            github.remove_label(item.number, label)
            remaining.remove(label)
        if added:
            github.add_labels(item.number, added)
    except AgentError as exc:
        steps = []
        if remaining:
            steps.append(f"remove {', '.join(remaining)}")
        if added:
            steps.append(f"add {', '.join(added)}")
        raise AgentError(f"Reset recorded, but label changes failed ({exc}). "
                         f"By hand, {'; '.join(steps)}.") from exc
    changes = []
    if stops:
        changes.append(f"removed {', '.join(stops)}")
    if added:
        changes.append(f"added {', '.join(added)}")
    message = ', '.join(changes) if changes else "Labels were already in place"
    return f"{message[0].upper()}{message[1:]}. A running launcher picks it up on its next poll."


@command("retry", arguments, number="required", legacy_number=True, config_required=True,
         build=("config", "github", "coordinator"))
def retry_run(args, config, github, actor, coordinator):
    if args.agent is None:
        item = github.item(args.number)
        matching = [agent for agent in config.agents if agent.kind in {item.kind, "either"}]
        if not matching:
            raise AgentError(f"No configured agent applies to {item.kind} #{item.number}")
        agent = next((agent for agent in matching if item.labels.intersection(agent.triggers)), matching[0])
        args.agent = agent.name
        print(f"Using agent {agent.name} for {item.kind} #{item.number}.")
    agent = next((agent for agent in config.agents if agent.name == args.agent), None)
    if agent is None:
        raise AgentError("Unknown configured agent")
    if not args.reason.strip():
        raise AgentError("retry requires a positive item number and a reason")
    history = coordinator.history(args.number)
    if live_leases(history, timestamp()):
        raise AgentError("Cannot reset attempts while an assignment is owned")
    item = github.item(args.number)
    record = {"kind": "reset", "agent": args.agent, "actor": actor,
              "assignment": item.number, "created": iso(timestamp()), "summary": args.reason}
    created = records([github.create_comment(item.number, body(record))])[0]
    coordinator.notices.resumed(item.number)
    print(f"Reset {args.agent} attempts on #{item.number}: {created['url']}")
    print(retry_labels(github, item, agent, config.stop_labels))
    return
