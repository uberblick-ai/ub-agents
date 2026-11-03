"""Inspect queue assignments and paused launchers through the status command."""

from dataclasses import replace
import json
from pathlib import Path
import socket

from ..attention import attention_details, display_notice, waiting_time
from ..denials import denial_count
from ..launcher_lock import launcher_summary, running_launcher
from ..records import latest_leases, lease_by_id, live_leases, same_run, timestamp
from ..status import lease_summary, process_details
from ._registry import command


def arguments(commands):
    status = commands.add_parser("status", help="matching work, owners, attempts and why items wait",
                                 overview_options=("--json",),
                                 description="Read matching assignments, leases, attempts and reported outcomes. "
                                 "With a number, explain only that issue or PR, even without a matching trigger. "
                                 "Use to inspect queue progress or why an item is waiting without changing it.",
                                 examples=("ub-agents status", "ub-agents status 143", "ub-agents status 143 --json"))
    status.add_argument("number", metavar="NUMBER", type=int, nargs="?", help="inspect only this item (optional)")
    status.add_argument("--json", action="store_true", help="emit structured status")
    return status


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
                                           display_notice(notice) if notice else None))
        active = live_leases(history, now)
        latest = latest_leases(history).get((plan.item.number, plan.agent.name))
        lease = active[0] if active else None
        process, process_reason = None, None
        if lease:
            if lease["id"] not in processes:
                processes[lease["id"]] = process_details(lease, history, now, host, root=loop.config.root)
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


def paused_launcher_status(args):
    launcher = running_launcher(Path(args.config).resolve().parent)
    if launcher is None or not launcher.get("configuration_pause"):
        return False
    # The current files may be precisely what stopped validation. Do not parse
    # them or plan work under a cached configuration.
    if args.json:
        print(json.dumps({"assignments": [], "launcher": launcher}, indent=2))
    else:
        print(launcher_summary(launcher))
    return True


def _print_status_json(rows, launcher, loop, explanation):
    result = {"assignments": rows, "launcher": launcher}
    if loop.milestone_skips:
        result["milestone_skips"] = list(loop.milestone_skips)
    if loop.milestone_wait:
        result["milestone_wait"] = loop.milestone_wait
    if explanation is not None:
        result["explanation"] = explanation
    print(json.dumps(result, indent=2))


def _print_status_row(row, config, now):
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
    if config.queue.milestones in {"order", "gate", "strict-gate"} and row["kind"] == "issue":
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


def configuration_paused(args):
    launcher = running_launcher(args.config.parent)
    return launcher is not None and bool(launcher.get("configuration_pause"))


@command("status", arguments, number="optional", config_required=True,
         missing_config=configuration_paused, early_return=paused_launcher_status,
         build=("config", "github", "coordinator", "loop"))
def status_run(args, config, github, actor, coordinator, loop, stop, interrupt):
    launcher = running_launcher(config.root)
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
        _print_status_json(rows, launcher, loop, explanation)
    elif explanation is not None:
        print(f"#{args.number}: {explanation}")
    elif not rows:
        print("No configured triggers match open GitHub work")
    else:
        for row in rows:
            _print_status_row(row, config, now)
    if not args.json:
        print(launcher_summary(launcher))
        for line in loop.milestone_skips:
            print(line)
        if loop.milestone_wait:
            print(loop.milestone_wait)
    return
