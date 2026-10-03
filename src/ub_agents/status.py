"""Read-only local process details and compact lease times for status."""

from datetime import datetime, timezone
from pathlib import Path

from .errors import CleanupError
from .execution import group_members
from .records import same_run, seconds


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


def process_details(lease, history, now, host):
    if lease.get("mode") == "recovery":
        return "recovery", "Outcome recovery in progress; no agent process to check."
    if not lease.get("host"):
        if lease["state"] == "claiming":
            return "claiming", "Claim in progress; host not recorded yet."
        return "unknown", "Process state unknown: host not recorded."
    if lease["host"] != host:
        return "other-host", "Process can't be checked from here; lease is on another host."
    if not lease.get("process_group"):
        return "starting", "Run is starting; no process group recorded yet."
    try:
        members = group_members(lease["process_group"])
    except CleanupError as exc:
        return "unknown", f"Process state unknown: {exc}"
    if members:
        log = (f"log: {Path(lease['log_dir']) / 'process.log'}" if lease.get("log_dir")
               else "log directory not recorded")
        return "running", f"Agent running; {log}"
    expiry = display_time(lease["expires"], now)
    reported = any(r["kind"] == "outcome" and r["lease_id"] == lease["id"]
                   and same_run(r, lease) for r in history)
    if reported:
        return "exited", ("Agent process has exited. A launcher accepts the reported outcome "
                          f"when it recovers the lease after {expiry}, or run "
                          f"`ub-agent recover --number {lease['assignment']} "
                          f"--agent {lease['agent']} --reason TEXT` now.")
    return "exited", ("Agent process has exited. Its launcher finishes the run, or another "
                      f"launcher recovers it after the lease ends at {expiry}.")
