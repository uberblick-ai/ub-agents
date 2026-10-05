"""Versioned coordination comments. Prose is for people, JSON is the contract."""

from datetime import datetime, timezone
import json
import re

from .errors import AgentError, RecordError

MARKER = "<!-- ub-agents:v3 -->"
# All coordination markers are excluded from assignment and approval input;
# only MARKER is parsed or used as coordination authority.
RECORD_MARKERS = (MARKER, "<!-- ub-agents:v1 -->", "<!-- ub-agents:v2 -->")
LEASE_STATES = {"claiming", "running", "released", "withdrawn"}
OUTCOMES = {"success", "retry", "blocked"}
# Fields that tie an outcome, a recovery or a contender to the run that owns it.
PROVENANCE = ("run", "agent", "actor", "runtime", "assignment", "assignment_sha")


def record_version(text):
    """Recognize a format marker for diagnostics, without parsing its record."""
    match = re.match(r"<!-- ub-agents:v([1-9][0-9]*) -->", text) if isinstance(text, str) else None
    return int(match[1]) if match else None


def same_run(record, lease):
    return all(record.get(k) == lease.get(k) for k in PROVENANCE)


def same_handoff(record, source):
    # A recovery may copy another account's outcome onto its handoff item.
    # GitHub keeps the copying account as that comment's author.
    return all(record.get(k) == source.get(k) for k in PROVENANCE if k != "actor")


def recovers(recovery, source):
    return (recovery["kind"] == "lease" and recovery.get("mode") == "recovery"
            and recovery["state"] != "withdrawn"
            and recovery.get("recovered_lease_id") == source["id"]
            and recovery.get("recovered_run") == source["run"]
            and all(recovery[k] == source[k] for k in ("assignment", "agent")))


def lease_by_id(history, lease_id):
    return next((r for r in history if r["kind"] == "lease" and r["id"] == lease_id), None)


def positive_int(value):
    return type(value) is int and value >= 1


def timestamp():
    return datetime.now(timezone.utc).timestamp()


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def seconds(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError(f"Invalid coordination timestamp: {value!r}") from exc


def body(record):
    if record["kind"] == "lease":
        state = record.get("result") or record["state"]
        note = record.get("summary") or "Assignment claimed."
    elif record["kind"] == "outcome":
        state = record.get("outcome") or record["status"]
        note = record["summary"]
    else:
        state, note = "reset", record["summary"]
    icon = {"success": "✅", "blocked": "⏸️", "retry": "↻", "reset": "↻"}.get(
        record.get("status") or record.get("result") or state, "▶️")
    sha = record.get("candidate_sha") or record.get("assignment_sha")
    candidate = f" {sha[:7]}" if sha else ""
    note = " ".join(note.split())
    if len(note) > 240:
        note = note[:237] + "…"
    line = f"{icon} {record['agent']} {state}{candidate} · {record['runtime']} — {note}"
    # The comment author is the actor; empty fields are omitted.
    shown = {k: v for k, v in record.items() if v is not None and k != "actor"}
    return (f"{MARKER}\n{line}\n\n<details>\n<summary>Coordination record</summary>\n\n"
            f"```json\n{json.dumps(shown, indent=2, sort_keys=True, ensure_ascii=False)}\n```\n\n</details>\n")


def records(comments, actor=None, *, trusted=None):
    """Parse marked records, checking author trust before parsing their payloads."""
    result = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise AgentError("Unreadable GitHub comment")
        if actor is not None and not own_comment(comment, actor):
            continue
        if (isinstance(comment.get("body"), str) and comment["body"].startswith(MARKER)
                and trusted is not None and not trusted(comment.get("user"))):
            continue
        try:
            text = comment["body"] or ""
            if not text.startswith(MARKER):
                continue
            text = text.removesuffix("\n")
            if not text.endswith("\n\n</details>") or "<details>\n<summary>Coordination record</summary>\n" not in text:
                raise ValueError("missing collapsed record")
            text = text.removesuffix("\n\n</details>")
            payload = text.rsplit("\n```json\n", 1)[1].removesuffix("\n")
            if not payload.endswith("\n```"):
                raise ValueError("missing JSON fence")
            record = json.loads(payload[:-4])
            if not isinstance(record, dict):
                raise ValueError("record must be a JSON object")
            # MARKER versions the record format. Payload fields cannot grant trust:
            # the actor is always the GitHub comment author.
            record["actor"] = comment["user"]["login"]
            record = {"assignment_sha": None, "candidate_sha": None, "handoff": None} | record
            validate(record)
            if comment.get("issue_url"):
                number = int(comment["issue_url"].rsplit("/", 1)[1])
                if number not in {record["assignment"], record["handoff"]}:
                    raise ValueError("record is posted on the wrong item")
            result.append(record | {"id": comment["id"], "url": comment.get("html_url", "")})
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, AgentError) as exc:
            raise RecordError(f"Malformed ub-agents comment {comment.get('id', '?')}: {exc}") from exc
    return sorted(result, key=lambda record: record["id"])


def own_comment(comment, actor):
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return isinstance(login, str) and login.casefold() == actor.casefold()


def latest_leases(history):
    """Latest non-withdrawn lease after each item/agent's last explicit reset."""
    resets = {(r["assignment"], r["agent"]): r["id"] for r in history if r["kind"] == "reset"}
    latest = {}
    for record in history:
        key = record["assignment"], record["agent"]
        if (record["kind"] == "lease" and record["state"] != "withdrawn"
                and record["id"] > resets.get(key, 0)):
            latest[key] = record
    return latest


def lease_summary(history, lease):
    """A released lease omits a summary that repeats its run's outcome."""
    if lease.get("summary"):
        return lease["summary"]
    return next((r["summary"] for r in history if r["kind"] == "outcome"
                 and r["lease_id"] == lease["id"]), "")


def validate_labels(labels):
    if (not isinstance(labels, list)
            or any(not isinstance(label, str) or not label.strip() or "\x00" in label
                   for label in labels)):
        raise ValueError("invalid transition labels")


def validate_transition(transition, started=False):
    required = {"add"}
    if started:
        required.add("started")
    allowed = required | {"remove"}
    if (not isinstance(transition, dict) or not required <= set(transition)
            or not set(transition) <= allowed):
        raise ValueError("invalid transition")
    if started and type(transition["started"]) is not bool:
        raise ValueError("invalid transition start flag")
    for key in ("add", "remove"):
        if key in transition:
            validate_labels(transition[key])


def declared_transition(lease, name):
    """Resolve a compact declaration without consulting current configuration."""
    declaration = lease["outcomes"][name]
    changes = {"add": declaration} if isinstance(declaration, list) else declaration
    return changes | {"triggers": lease["declared_triggers"], "stop_labels": lease["stop_labels"],
                      "remove": sorted(set(lease["declared_triggers"]).union(changes.get("remove", ())))}


def stop_report(lease, status, outcome=None):
    return (status == "blocked" or outcome is not None
            and bool(set(declared_transition(lease, outcome)["add"]).intersection(lease["stop_labels"])))


def validate_action(action, required=False):
    if action is None and not required:
        return
    if (not isinstance(action, str) or not action.strip() or len(action) > 300
            or action.splitlines() != [action] or "\x00" in action):
        raise ValueError("--action must be one non-empty line of at most 300 characters")


def validate_report_action(lease, outcome):
    # Old leases predate the requirement. Launcher-only reports and recovery
    # receipts have no agent ask; their notices retain the existing form.
    required = (lease.get("action_required", False) and not lease.get("unreported")
                and lease.get("mode") != "recovery"
                and stop_report(lease, outcome["status"], outcome.get("outcome")))
    validate_action(outcome.get("action"), required=required)


def resolve_transition(transition, declaration):
    """Resolve a compact transition using its original lease context."""
    return transition | {"triggers": declaration["triggers"], "stop_labels": declaration["stop_labels"],
                         "remove": sorted(set(declaration["triggers"]).union(transition.get("remove", ())))}


def validate(record):
    for field in ("run", "agent", "actor", "runtime", "created"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"missing {field}")
    if not positive_int(record.get("assignment")):
        raise ValueError("invalid assignment")
    for field in ("assignment_sha", "candidate_sha"):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"invalid {field}")
    seconds(record["created"])
    if ((record.get("kind") == "lease" or "attempt_effect" in record)
            and record.get("attempt_effect") not in {"pending", "failure", "reset", "unchanged"}):
        raise ValueError("invalid attempt effect")
    if record.get("kind") == "lease":
        if "action_required" in record and type(record["action_required"]) is not bool:
            raise ValueError("invalid action requirement")
        validate_labels(record.get("declared_triggers"))
        validate_labels(record.get("stop_labels"))
        if record.get("state") not in LEASE_STATES:
            raise ValueError("invalid lease state")
        if not positive_int(record.get("attempt")):
            raise ValueError("invalid attempt")
        if type(record.get("started")) is not bool:
            raise ValueError("invalid started flag")
        if "cleanup" in record and record["cleanup"] != "unconfirmed":
            raise ValueError("invalid cleanup state")
        if "process_group" in record and (type(record["process_group"]) is not int or record["process_group"] < 1):
            raise ValueError("invalid process group")
        if "cleanup_hook_error" in record and not isinstance(record["cleanup_hook_error"], str):
            raise ValueError("invalid cleanup hook diagnostic")
        if record.get("mode") == "recovery":
            if (not positive_int(record.get("recovered_lease_id"))
                    or not isinstance(record.get("recovered_run"), str) or not record["recovered_run"]):
                raise ValueError("invalid recovered lease identity")
        if "recovery_reason" in record:
            if (record.get("mode") != "recovery" or not isinstance(record["recovery_reason"], str)
                    or not record["recovery_reason"].strip()):
                raise ValueError("invalid operator recovery reason")
        if "outcomes" in record:
            declarations = record["outcomes"]
            if (not isinstance(declarations, dict) or not declarations
                    or any(not isinstance(name, str) or not name.strip() for name in declarations)):
                raise ValueError("invalid outcome declarations")
            for transition in declarations.values():
                if isinstance(transition, list):
                    validate_labels(transition)
                else:
                    validate_transition(transition)
        seconds(record.get("expires"))
        if "retry_after" in record:
            seconds(record["retry_after"])
    elif record.get("kind") == "outcome":
        if "action" in record:
            validate_action(record["action"], required=True)
        if record.get("status") not in OUTCOMES or not isinstance(record.get("summary"), str):
            raise ValueError("invalid outcome")
        if type(record.get("lease_id")) is not int:
            raise ValueError("missing lease_id")
        if type(record.get("accepted")) is not bool:
            raise ValueError("invalid accepted flag")
        if "outcome" in record and (not isinstance(record["outcome"], str) or not record["outcome"].strip()):
            raise ValueError("invalid named outcome")
        if "transition_complete" in record and type(record["transition_complete"]) is not bool:
            raise ValueError("invalid transition completion flag")
        if "rejected" in record and not isinstance(record["rejected"], str):
            raise ValueError("invalid rejected outcome")
        if "transition" in record:
            validate_transition(record["transition"], started=True)
        if record.get("handoff") is not None and not positive_int(record["handoff"]):
            raise ValueError("invalid handoff")
    elif record.get("kind") == "reset":
        if not isinstance(record.get("summary"), str) or not record["summary"].strip():
            raise ValueError("reset requires a reason")
    else:
        raise ValueError("unknown record kind")


def payload(record):
    return {k: v for k, v in record.items() if k not in {"id", "url"}}


def live_leases(history, now):
    # A recovery claim permanently revokes its source, even before expiry. The
    # recoverers still elect the lowest live comment id using the usual rules.
    sources = {r["id"]: r for r in history if r["kind"] == "lease"}
    superseded = {source["id"] for recovery in history
                  if recovery["kind"] == "lease" and recovery.get("mode") == "recovery"
                  and recovery["state"] != "withdrawn"
                  if (source := sources.get(recovery.get("recovered_lease_id"))) is not None
                  and recovers(recovery, source)}
    return [r for r in history if r["kind"] == "lease"
            and r["id"] not in superseded
            and r["state"] in {"claiming", "running"} and seconds(r["expires"]) > now]


def attempt_effect(history, lease, now):
    """Resolve a run's verdict, including recovery, without rewriting its lease."""
    recoveries = [r for r in history if r["kind"] == "lease"
                  and recovers(r, lease)
                  and (r["state"] == "released" or r.get("cleanup") == "unconfirmed"
                       or (r.get("result") and r.get("attempt_effect") in {"failure", "unchanged"}))]
    verdict = recoveries[-1] if recoveries else lease
    if lease.get("cleanup") == "unconfirmed" or verdict.get("cleanup") == "unconfirmed":
        return "failure"
    effect = verdict["attempt_effect"]
    if effect != "pending":
        return effect
    if lease["state"] == "released":
        return "failure"  # A missing final classification is unsafe.
    outcomes = [r for r in history if r["kind"] == "outcome" and r["lease_id"] == lease["id"]
                and same_run(r, lease)
                and seconds(lease["created"]) <= seconds(r["created"]) <= seconds(lease["expires"])]
    if (len(outcomes) == 1 and outcomes[0]["status"] == "success"
            and outcomes[0]["accepted"] and not outcomes[0].get("rejected")):
        return "reset"
    if seconds(lease["expires"]) > now:
        return "pending"
    if len(outcomes) != 1:
        return "failure"  # No report, or conflicting reports.
    outcome = outcomes[0]
    if outcome.get("rejected"):
        return outcome.get("attempt_effect", "failure")
    if outcome["status"] == "success":
        return "reset" if outcome["accepted"] else "pending"
    return "failure" if outcome["status"] == "retry" else "unchanged"


def attempts(history, agent, now):
    """Consecutive failures per assignment/agent."""
    failures = {}
    for record in history:
        if record["agent"] != agent:
            continue
        current = failures.setdefault(record["assignment"], [])
        if record["kind"] == "reset":
            current.clear()
        elif (record["kind"] == "lease" and record["state"] != "withdrawn"
              and record.get("mode") != "recovery"):
            effect = attempt_effect(history, record, now)
            if effect == "reset":
                current.clear()
            elif effect == "failure":
                current.append(record)
    return sorted((r for group in failures.values() for r in group), key=lambda r: r["id"])


def backoff(agent, failures):
    """Delay after a failure: configured base, doubling with consecutive failures."""
    return min(agent.max_backoff_seconds, agent.backoff_seconds * 2 ** min(max(0, failures - 1), 32))
