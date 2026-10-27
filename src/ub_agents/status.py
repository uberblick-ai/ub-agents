"""Read-only local process details and compact lease times for status."""

from datetime import datetime, timezone

from .errors import CleanupError
from .execution import group_members
from .records import live_leases, same_run, seconds
from .run_config import process_group, run_directory


def refusal_reason(plan, now, host, *, root, include_log=True):
    """Use status's process verdict and lease details for a scoped refusal."""
    active = live_leases(plan.history, now)
    lease = active[0] if active else plan.owner if plan.state == "owned" else None
    if lease is None:
        return plan.state, plan.reason
    process, reason = process_details(lease, plan.history, now, host, root=root, include_log=include_log)
    state = "running" if plan.state == "owned" and process == "running" else plan.state
    summary = f"{lease['agent']} {lease_summary(lease, now)}"
    if lease["assignment"] != plan.item.number:
        summary = f"{plan.reason}; {summary}"
    return state, f"{summary} · {reason}"


def display_time(value, now):
    moment = datetime.fromtimestamp(seconds(value), timezone.utc)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return moment.strftime("%H:%MZ" if moment.date() == today else "%Y-%m-%d %H:%MZ")


def lease_summary(lease, now):
    minutes = max(0, int((seconds(lease["expires"]) - now) / 60))
    hours, minutes = divmod(minutes, 60)
    remaining = f"{hours}h {minutes}m" if hours else f"{minutes}m" if minutes else "<1m"
    return (f"claimed by @{lease['actor']} on {lease.get('host') or 'unknown host'} "
            f"at {display_time(lease['created'], now)} · {lease['runtime']} · "
            f"lease ends {display_time(lease['expires'], now)} (in {remaining})")


def process_details(lease, history, now, host, *, root, include_log=True):
    if lease.get("mode") == "recovery":
        return "recovery", "Outcome recovery in progress; no agent process to check."
    if not lease.get("host"):
        if lease["state"] == "claiming":
            return "claiming", "Claim in progress; host not recorded yet."
        return "unknown", "Process state unknown: host not recorded."
    if lease["host"] != host:
        return "other-host", "Process can't be checked from here; lease is on another host."
    try:
        group = process_group(root, lease["run"])
        if group is None:
            return "starting", "Run is starting; no process group recorded yet."
        members = group_members(group)
    except CleanupError as exc:
        return "unknown", f"Process state unknown: {exc}"
    if members:
        if not include_log:
            return "running", "Agent running."
        log = f"log: {run_directory(root, lease['run']) / 'process.log'}"
        return "running", f"Agent running; {log}"
    expiry = display_time(lease["expires"], now)
    reported = any(r["kind"] == "outcome" and r["lease_id"] == lease["id"]
                   and same_run(r, lease) for r in history)
    if reported:
        return "exited", ("Agent process has exited. A launcher accepts the reported outcome "
                          f"when it recovers the lease after {expiry}.")
    return "exited", ("Agent process has exited. Its launcher finishes the run, or another "
                      f"launcher recovers it after the lease ends at {expiry}.")
