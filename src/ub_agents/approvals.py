"""Content-bound issue approvals, independent of coordination and pickup policy."""

from dataclasses import dataclass
import hashlib
import json
import re

from .errors import AgentError
from .records import MARKER as COORDINATION_MARKER, positive_int

MARKER = "<!-- ub-agent-approval:v1 -->"
INPUT_VERSION = "ub-agent-issue-input:v1"


@dataclass(frozen=True)
class IssueSnapshot:
    repository: str
    number: int
    title: str
    body: str
    comments: tuple[dict, ...]

    @property
    def issue_url(self):
        return f"https://api.github.com/repos/{self.repository}/issues/{self.number}"


def trusted_approver(login, approvers, launcher_account):
    """Only GitHub's author login supplies identity; agents cannot approve."""
    return (isinstance(login, str) and bool(login) and bool(launcher_account)
            and login.casefold() != launcher_account.casefold()
            and login.casefold() in {approver.casefold() for approver in approvers})


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate approval field")
        result[key] = value
    return result


def parse_approval(text):
    """Malformed marker-like text remains ordinary input, even from an approver."""
    prefix = f"{MARKER}\n```json\n"
    if not text.startswith(prefix) or not text.endswith("\n```\n"):
        return None
    try:
        record = json.loads(text[len(prefix):-5], object_pairs_hook=unique_object)
        if (not isinstance(record, dict) or record.keys() != {"issue", "stage", "digest"}
                or not positive_int(record["issue"])
                or not isinstance(record["stage"], str) or not record["stage"].strip()
                or not isinstance(record["digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["digest"])):
            return None
        return record
    except (ValueError, TypeError):
        return None


def approval_body(number, stage, digest):
    record = {"issue": number, "stage": stage, "digest": digest}
    text = f"{MARKER}\n```json\n{json.dumps(record, sort_keys=True, indent=2)}\n```\n"
    if parse_approval(text) is None:
        raise AgentError("Invalid approval record")
    return text


def snapshot_comments(snapshot):
    """Fail closed on incomplete reads rather than silently omitting input."""
    if (not isinstance(snapshot.repository, str) or not snapshot.repository
            or not positive_int(snapshot.number)
            or not isinstance(snapshot.title, str) or not isinstance(snapshot.body, str)):
        raise AgentError("Unreadable issue approval input")
    seen, result = set(), []
    for comment in snapshot.comments:
        try:
            login = comment["user"]["login"]
            if (not positive_int(comment["id"]) or comment["id"] in seen
                    or not isinstance(login, str) or not login
                    or (comment["body"] is not None and not isinstance(comment["body"], str))
                    or not isinstance(comment["issue_url"], str)):
                raise ValueError("invalid comment fields")
            seen.add(comment["id"])
            result.append(comment)
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError("Unreadable issue approval comment") from exc
    return tuple(sorted(result, key=lambda comment: comment["id"]))


def trusted_record(snapshot, comment, approvers, launcher_account):
    if not trusted_approver(comment["user"]["login"], approvers, launcher_account):
        return None
    record = parse_approval(comment["body"] or "")
    # Validate both the payload's target and GitHub's actual posting location.
    if (record is None or record["issue"] != snapshot.number
            or comment["issue_url"].casefold() != snapshot.issue_url.casefold()):
        return None
    return record


def input_comments(snapshot, approvers, launcher_account):
    included = []
    for comment in snapshot_comments(snapshot):
        login, text = comment["user"]["login"], comment["body"] or ""
        if (launcher_account and login.casefold() == launcher_account.casefold()
                and text.startswith(COORDINATION_MARKER)):
            continue
        # Stale records must also be excluded: including their digest would make
        # the next approval self-referential. Stage validity is checked separately.
        if trusted_record(snapshot, comment, approvers, launcher_account) is not None:
            continue
        included.append({"id": comment["id"], "author": login, "body": text})
    return tuple(included)


def canonical_input(snapshot, approvers, launcher_account):
    data = {"version": INPUT_VERSION, "repository": snapshot.repository,
            "number": snapshot.number, "title": snapshot.title, "body": snapshot.body,
            "comments": input_comments(snapshot, approvers, launcher_account)}
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def input_digest(snapshot, approvers, launcher_account):
    return hashlib.sha256(canonical_input(snapshot, approvers, launcher_account)).hexdigest()


def has_approval(snapshot, stage, approvers, launcher_account):
    digest = input_digest(snapshot, approvers, launcher_account)
    for comment in snapshot_comments(snapshot):
        record = trusted_record(snapshot, comment, approvers, launcher_account)
        if record is not None and record["stage"] == stage and record["digest"] == digest:
            return True
    return False


def read_snapshot(github, number):
    item = github.item(number)
    if item.kind != "issue" or item.number != number:
        raise AgentError("approve requires an issue, not a pull request")
    return IssueSnapshot(github.repository, item.number, item.title, item.body,
                         tuple(github.comments(number)))


def approve(config, github, actor, number, stage):
    if not positive_int(number):
        raise AgentError("approve requires a positive issue number")
    if not config.launcher_account:
        raise AgentError("approve requires launcher-account to identify the account used by agents")
    if not trusted_approver(actor, config.approvers, config.launcher_account):
        raise AgentError("approve requires a listed approver other than the launcher account")
    snapshot = read_snapshot(github, number)
    digest = input_digest(snapshot, config.approvers, config.launcher_account)
    print(f"Issue: {snapshot.repository}#{number}; stage: {stage}")
    print(f"SHA-256: {digest}")
    print("Included comments (id order):")
    print(json.dumps(input_comments(snapshot, config.approvers, config.launcher_account),
                     ensure_ascii=False, indent=2), flush=True)
    # Reads are not atomic. Avoid knowingly posting an already stale approval.
    current = read_snapshot(github, number)
    if input_digest(current, config.approvers, config.launcher_account) != digest:
        raise AgentError("Issue input changed while preparing approval; rerun approve")
    created = github.create_comment(number, approval_body(number, stage, digest))
    print(f"Approval posted: {created['html_url']}")
