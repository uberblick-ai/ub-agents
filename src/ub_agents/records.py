"""Versioned coordination comments. Prose is for people, JSON is the contract."""

from datetime import datetime, timezone
import json

from .errors import AgentError, RecordError

MARKER = "<!-- ub-agent:v1 -->"
LEASE_STATES = {"claiming", "running", "released", "withdrawn"}
OUTCOMES = {"success", "retry", "blocked"}


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
    title = f"**ub-agent {record['kind']} — {record['agent']}**"
    if record["kind"] == "lease":
        note = record.get("summary") or (f"Result: {record['result']}; reported in the outcome."
                                         if record["state"] == "released" else "Assignment claimed.")
        description = (f"Owner: @{record['actor']} · {record['runtime']}\n\n"
                       f"State: {record['state']} · Lease expires: {record['expires']}\n\n{note}")
    elif record["kind"] == "outcome":
        description = f"{record['status']}: {record['summary']}"
    else:
        description = f"Attempt budget reset: {record['summary']}"
    # The comment author is the actor, except on a handoff copy; empty fields are omitted.
    shown = {k: v for k, v in record.items()
             if v is not None and (k != "actor" or "recorded_by" in record)}
    return f"{MARKER}\n{title}\n\n{description}\n\n```json\n{json.dumps(shown, indent=2, sort_keys=True, ensure_ascii=False)}\n```\n"


def records(comments, trusted_actors=None):
    result = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise AgentError("Unreadable GitHub comment")
        if trusted_actors is not None and not trusted_comment(comment, trusted_actors):
            continue
        try:
            text = comment["body"] or ""
            if not text.startswith(MARKER):
                continue
            payload = text.rsplit("\n```json\n", 1)[1].removesuffix("\n")
            if not payload.endswith("\n```"):
                raise ValueError("missing JSON fence")
            record = json.loads(payload[:-4])
            if not isinstance(record, dict):
                raise ValueError("record must be a JSON object")
            # MARKER versions the record format.
            if "recorded_by" not in record:
                record.setdefault("actor", comment["user"]["login"])
            record = {"assignment_sha": None, "candidate_sha": None, "handoff": None} | record
            validate(record)
            if "recorded_by" in record and (record["kind"] != "outcome" or not record.get("handoff")):
                raise ValueError("recorded_by is only valid for mirrored handoff outcomes")
            if trusted_actors is not None and record["actor"].casefold() not in trusted_actors:
                raise ValueError("source actor is not a configured operator")
            if record.get("recorded_by", record["actor"]).casefold() != comment["user"]["login"].casefold():
                raise ValueError("record actor does not match GitHub comment author")
            if comment.get("issue_url"):
                number = int(comment["issue_url"].rsplit("/", 1)[1])
                destination = record["handoff"] if "recorded_by" in record else record["assignment"]
                if destination != number:
                    raise ValueError("record is posted on the wrong assignment")
            result.append(record | {"id": comment["id"], "url": comment.get("html_url", "")})
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, AgentError) as exc:
            raise RecordError(f"Malformed ub-agent comment {comment.get('id', '?')}: {exc}") from exc
    return sorted(result, key=lambda record: record["id"])


def trusted_comment(comment, trusted_actors):
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return isinstance(login, str) and login.casefold() in trusted_actors


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


def validate(record):
    for field in ("run", "agent", "actor", "runtime", "created"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"missing {field}")
    if type(record.get("assignment")) is not int or record["assignment"] < 1:
        raise ValueError("invalid assignment")
    for field in ("assignment_sha", "candidate_sha"):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"invalid {field}")
    seconds(record["created"])
    if record.get("kind") == "lease":
        if record.get("state") not in LEASE_STATES:
            raise ValueError("invalid lease state")
        if type(record.get("attempt")) is not int or record["attempt"] < 1:
            raise ValueError("invalid attempt")
        if type(record.get("started")) is not bool:
            raise ValueError("invalid started flag")
        if "cleanup" in record and record["cleanup"] != "unconfirmed":
            raise ValueError("invalid cleanup state")
        seconds(record.get("expires"))
        if "retry_after" in record:
            seconds(record["retry_after"])
    elif record.get("kind") == "outcome":
        if record.get("status") not in OUTCOMES or not isinstance(record.get("summary"), str):
            raise ValueError("invalid outcome")
        if type(record.get("lease_id")) is not int:
            raise ValueError("missing lease_id")
        if type(record.get("accepted")) is not bool:
            raise ValueError("invalid accepted flag")
        if record.get("handoff") is not None and (type(record["handoff"]) is not int or record["handoff"] < 1):
            raise ValueError("invalid handoff")
    elif record.get("kind") == "reset":
        if not isinstance(record.get("summary"), str) or not record["summary"].strip():
            raise ValueError("reset requires a reason")
    else:
        raise ValueError("unknown record kind")


def payload(record):
    return {k: v for k, v in record.items() if k not in {"id", "url"}}


def live_leases(history, now):
    return [r for r in history if r["kind"] == "lease"
            and r["state"] in {"claiming", "running"} and seconds(r["expires"]) > now]


def attempts(history, agent, now):
    # Count all starts across PR revisions. An expired tentative claim is conservative
    # crash evidence; confirmed withdrawals and outcome-only recovery cost no start.
    boundary = max((r["id"] for r in history if r["kind"] == "reset" and r["agent"] == agent), default=0)
    return [r for r in history if r["id"] > boundary and r["kind"] == "lease" and r["agent"] == agent
            and r["state"] != "withdrawn" and r.get("mode") != "recovery"
            and (r["started"] or (r["state"] == "claiming" and seconds(r["expires"]) <= now))]
