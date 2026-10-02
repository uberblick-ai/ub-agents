"""Maintainer starts and content-bound approvals, independent of claim enforcement."""

from dataclasses import dataclass
import hashlib
import json
import re

from .errors import AgentError
from .records import positive_int, seconds

MARKER = "<!-- ub-agent:approval:v1 -->"
MAINTAINERS = {"maintain", "admin"}
TRUSTED = MAINTAINERS | {"write"}
SHA256 = re.compile(r"[0-9a-f]{64}")


def body_sha(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def content_sha(title, body):
    encoded = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
    return body_sha(encoded)


def approval_body(number, title, body, comments):
    record = {"issue": number, "content_sha256": content_sha(title, body),
              "comments": [{"id": c["id"], "body_sha256": body_sha(c["body"])}
                           for c in sorted(comments, key=lambda c: c["id"])]}
    return f"{MARKER}\n\n```json\n{json.dumps(record, indent=2)}\n```\n"


def parse_approval(body, number):
    """Malformed or unsupported records grant nothing, even from maintainers."""
    if not body.startswith(f"{MARKER}\n\n```json\n") or not body.endswith("\n```\n"):
        return None
    try:
        record = json.loads(body[len(MARKER) + len("\n\n```json\n"):-len("\n```\n")])
        if (set(record) != {"issue", "content_sha256", "comments"}
                or not positive_int(record["issue"]) or record["issue"] != number
                or not isinstance(record["content_sha256"], str)
                or not SHA256.fullmatch(record["content_sha256"])
                or not isinstance(record["comments"], list)):
            return None
        ids = set()
        for comment in record["comments"]:
            if (set(comment) != {"id", "body_sha256"} or not positive_int(comment["id"])
                    or comment["id"] in ids or not isinstance(comment["body_sha256"], str)
                    or not SHA256.fullmatch(comment["body_sha256"])):
                return None
            ids.add(comment["id"])
        return record
    except (ValueError, TypeError, KeyError):
        return None


class Roles:
    """Cache only within one observation; role changes affect the next check."""
    def __init__(self, github):
        self.github, self.cache = github, {}

    def __call__(self, actor):
        login = (actor or {}).get("login")
        if not isinstance(login, str) or not login:
            return None
        key = login.casefold()
        if key not in self.cache:
            self.cache[key] = self.github.role(login)
        return self.cache[key]


@dataclass(frozen=True)
class ApprovalCheck:
    allowed: bool
    reason: str
    cleared_comment_ids: frozenset[int] = frozenset()


def historical_content(content, renames, at):
    """Recover exactly the content at posting; uncertainty grants no approval."""
    title = content["title"]
    for event in reversed(renames):
        when = seconds(event["created_at"])
        if when == at:
            return None  # GitHub timestamps cannot order same-second changes.
        if when > at:
            rename = event["rename"]
            if rename["to"] != title:
                return None
            title = rename["from"]
    edits = sorted(content["edits"], key=lambda e: seconds(e["editedAt"]))
    if any(seconds(e["editedAt"]) == at and e["editedAt"] != content["createdAt"] for e in edits):
        return None
    before = [e for e in edits if seconds(e["editedAt"]) <= at]
    if before:
        revision = before[-1]
        if revision.get("deletedAt") or not isinstance(revision.get("diff"), str):
            return None
        body = revision["diff"]
    elif content["lastEditedAt"] is None or seconds(content["lastEditedAt"]) < at:
        body = content["body"]
    else:
        return None
    return title, body


def check_issue(github, number, trigger_labels):
    """Say whether work is authorized and which outside comments are input.

    The caller supplies the union of trigger labels for issue/either agents.
    This function performs reads only; pickup enforcement belongs to the caller.
    """
    try:
        return _check_issue(github, number, set(trigger_labels))
    except (AgentError, KeyError, TypeError, ValueError, AttributeError):
        return ApprovalCheck(False, "Issue approval history is unreadable; retry or ask a maintainer")


def _check_issue(github, number, trigger_labels):
    content = github.issue_content(number)
    if not isinstance(content["title"], str) or not isinstance(content["body"], str):
        raise ValueError("invalid content")
    created = seconds(content["createdAt"])
    edits = content["edits"]
    for edit in edits:
        seconds(edit["editedAt"])
        # A missing editor is outside; a deleted diff still has edit attribution.
        edit["editor"]
    if content["lastEditedAt"] is not None:
        last = seconds(content["lastEditedAt"])
        if not edits or max(seconds(e["editedAt"]) for e in edits) != last:
            raise ValueError("incomplete body history")
    if sum(seconds(e["editedAt"]) == created for e in edits) > 1:
        raise ValueError("cannot distinguish creation from same-second edits")
    timeline = github.timeline(number)
    roles = Roles(github)
    starts, renames = [], []
    for event in timeline:
        if event["event"] not in {"labeled", "renamed"}:
            continue
        at = seconds(event["created_at"])
        if event["event"] == "renamed":
            if not all(isinstance(event["rename"][key], str) for key in ("from", "to")):
                raise ValueError("invalid rename")
            renames.append(event)
        elif event["label"]["name"] in trigger_labels and roles(event.get("actor")) in MAINTAINERS:
            starts.append(at)
    renames.sort(key=lambda e: seconds(e["created_at"]))
    comments = github.comments(number)
    outside, approvals = [], []
    for comment in comments:
        if not positive_int(comment["id"]) or not isinstance(comment["body"], str):
            raise ValueError("invalid comment")
        at, updated = seconds(comment["created_at"]), seconds(comment["updated_at"])
        role = roles(comment.get("user"))
        if role not in TRUSTED:
            outside.append(comment)
        if role not in MAINTAINERS or updated != at:
            continue
        record = parse_approval(comment["body"], number)
        if record is None:
            continue
        old = historical_content(content, renames, at)
        if old is not None and record["content_sha256"] == content_sha(*old):
            approvals.append((at, record))
    cleared = set()
    for comment in outside:
        updated = seconds(comment["updated_at"])
        if any(updated < start for start in starts):
            cleared.add(comment["id"])
        for at, record in approvals:
            if updated >= at or seconds(comment["created_at"]) >= at:
                continue
            if any(c["id"] == comment["id"] and c["body_sha256"] == body_sha(comment["body"])
                   for c in record["comments"]):
                cleared.add(comment["id"])
    cleared = frozenset(cleared)
    if not starts:
        return ApprovalCheck(False, "No maintainer has applied an issue agent's trigger label", cleared)
    latest = max(starts + [at for at, _ in approvals])
    for event in renames:
        if seconds(event["created_at"]) >= latest and roles(event.get("actor")) not in TRUSTED:
            return ApprovalCheck(False, "Outside title edit after approval; a maintainer must approve", cleared)
    for edit in edits:
        at = seconds(edit["editedAt"])
        if at > created and at >= latest and roles(edit.get("editor")) not in TRUSTED:
            return ApprovalCheck(False, "Outside body edit after approval; a maintainer must approve", cleared)
    return ApprovalCheck(True, "Maintainer start approved; no later outside title or body edits", cleared)


def approve_issue(github, number, actor):
    """Print reviewed input and post one record; never grant a non-maintainer authority."""
    if not positive_int(number):
        raise AgentError("approve requires a positive issue number")
    if github.role(actor) not in MAINTAINERS:
        raise AgentError("approve requires a maintainer (maintain or admin repository role)")
    item = github.item(number)
    if item.kind != "issue":
        raise AgentError("approve accepts issues only")
    roles = Roles(github)
    comments = github.comments(number)
    outside = [c for c in comments if roles(c.get("user")) not in TRUSTED]
    print(f"Issue #{number}: {item.title}\n\n{item.body}\n\nOutside comments:")
    for comment in outside:
        print(f"\nComment {comment['id']} by @{(comment.get('user') or {}).get('login', 'unknown')}:\n{comment['body']}")
    print("", flush=True)
    # A concurrent edit between displaying input and posting must not be silently approved.
    current = github.item(number)
    if (current.title, current.body) != (item.title, item.body) or github.comments(number) != comments:
        raise AgentError("Issue changed while displaying approval input; rerun approve")
    return github.create_comment(number, approval_body(number, item.title, item.body, outside))
