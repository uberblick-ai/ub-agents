"""Issue 616 experiment, not a supported protocol or configuration interface.

Only serialized completion interleavings are modeled. There is no early process
cancellation, parser/doctor/UI integration, or cross-launcher transition lock.
"""

from .records import lease_by_id, same_run


def compatible(left, right):
    return (left.get("parallel") is True and right.get("parallel") is True
            and left.get("assignment_kind") == right.get("assignment_kind") == "pr"
            and left["assignment"] == right["assignment"]
            and left["assignment_sha"] == right["assignment_sha"]
            and left["agent"] != right["agent"])


def winners(contenders):
    elected = []
    for contender in sorted(contenders, key=lambda r: r["id"]):
        if all(compatible(contender, owner) for owner in elected):
            elected.append(contender)
    return elected


def role_claim(item, agent):
    return {"parallel": agent.parallel, "assignment_kind": item.kind,
            "assignment": item.number, "assignment_sha": item.head, "agent": agent.name}


def openings(history, triggers):
    """Per-trigger opening identity, derived from durable intent, without groups.

    A missing opener is a manual start in this experiment. Production must define
    explicit admission and repair for manually applied trigger labels.
    """
    result = {}
    for record in history:
        if record["kind"] != "outcome" or not record.get("transition", {}).get("started"):
            continue
        for trigger in triggers:
            if trigger in record["transition"]["add"]:
                result[trigger] = record["run"]
    return result


def join_ready(item, agent, history, coordinator):
    if not agent.trigger_all:
        return True
    if not set(agent.triggers) <= item.labels:
        return False
    common_openings = None
    for label in agent.triggers:
        label_openings = set()
        for report in history:
            if (report["kind"] != "outcome" or not report.get("accepted")
                    or report.get("rejected") or report.get("cancelled")
                    or not report.get("transition_complete")
                    or label not in report.get("transition", {}).get("add", ())):
                continue
            source = lease_by_id(history, report["lease_id"])
            if (not source or not source.get("parallel") or not same_run(report, source)
                    or report["candidate_sha"] != item.head or source["assignment_sha"] != item.head
                    or not coordinator.released_success(history, report)
                    or source.get("gate_openings", {}) != openings(history, source["triggers"])):
                continue
            # A later durable invalidation defeats an older pass even when a
            # crash leaves its label visible. IDs order this deterministic dry run.
            if any(r["kind"] == "outcome" and r["id"] > report["id"]
                   and r.get("transition", {}).get("started")
                   and label in r["transition"].get("remove", ()) for r in history):
                continue
            label_openings.update(source.get("gate_openings", {}).values())
        if not label_openings:
            return False
        common_openings = label_openings if common_openings is None else common_openings & label_openings
    return bool(common_openings)
