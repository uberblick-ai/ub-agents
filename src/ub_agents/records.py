"""Versioned coordination comments. Prose is for people, JSON is the contract."""

from datetime import datetime, timezone
import json

from .errors import AgentError

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
        description = (f"Owner: @{record['actor']} · {record['runtime']}\n\n"
                       f"State: {record['state']} · Lease expires: {record['expires']}\n\n"
                       f"{record.get('summary', 'Assignment claimed.')}")
    elif record["kind"] == "outcome":
        description = f"{record['status']}: {record['summary']}"
    else:
        description = f"Attempt budget reset: {record['summary']}"
    return f"{MARKER}\n{title}\n\n{description}\n\n```json\n{json.dumps(record, indent=2, sort_keys=True)}\n```\n"


def records(comments):
    result = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise AgentError("Unreadable GitHub comment")
        try:
            text = comment["body"] or ""
            if not text.startswith(MARKER):
                continue
            payload = text.rsplit("\n```json\n", 1)[1].removesuffix("\n")
            if not payload.endswith("\n```"):
                raise ValueError("missing JSON fence")
            record = json.loads(payload[:-4])
            validate(record)
            if record.get("recorded_by", record["actor"]).casefold() != comment["user"]["login"].casefold():
                raise ValueError("record actor does not match GitHub comment author")
            result.append(record | {"id": comment["id"], "url": comment.get("html_url", "")})
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise AgentError(f"Malformed ub-agent comment {comment.get('id', '?')}: {exc}") from exc
    return sorted(result, key=lambda record: record["id"])


def validate(record):
    if not isinstance(record, dict) or type(record.get("version")) is not int or record.get("version") != 1:
        raise ValueError("unsupported record version")
    for field in ("run", "agent", "actor", "runtime", "created"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"missing {field}")
    if type(record.get("assignment")) is not int or record["assignment"] < 1:
        raise ValueError("invalid assignment")
    if record.get("assignment_kind") not in {"issue", "pr"}:
        raise ValueError("invalid assignment kind")
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
        seconds(record.get("expires"))
        if "finished" in record:
            seconds(record["finished"])
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
    # crash evidence; losing contenders that confirm withdrawal cost no attempt.
    boundary = max((r["id"] for r in history if r["kind"] == "reset" and r["agent"] == agent), default=0)
    return [r for r in history if r["id"] > boundary and r["kind"] == "lease" and r["agent"] == agent
            and r["state"] != "withdrawn"
            and (r["started"] or (r["state"] == "claiming" and seconds(r["expires"]) <= now))]
