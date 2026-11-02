"""History of branches created by assignment executions."""

from .records import issue_branch


def earlier_branches(coordinator, item, agent, run):
    """Branches from earlier executions; unstarted claims and recovery create none."""
    if item.kind != "issue":
        return []
    return sorted({issue_branch(r) for r in coordinator.history(item.number) if r["kind"] == "lease"
                   and r["agent"] == agent.name and r["run"] != run
                   and r["started"] and r.get("mode") != "recovery"})
