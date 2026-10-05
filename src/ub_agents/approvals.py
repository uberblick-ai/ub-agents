"""Read-only maintainer starts, approvals and filtered assignment snapshots."""

from dataclasses import dataclass, field
import hashlib
import json
import re

from .errors import AgentError, GitHubError, LostOwnership
from .notices import ACTION_MARKER
from .records import (lease_by_id,
                      positive_int, records, recovers, same_run, seconds)
from .trust import LauncherTrust

MARKER = "<!-- ub-agents:approval:v1 -->"
MAINTAINERS = {"maintain", "admin"}
TRUSTED = MAINTAINERS | {"write"}
SHA256 = re.compile(r"[0-9a-f]{64}")


def body_sha(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def content_sha(title, body):
    encoded = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
    return body_sha(encoded)


def approval_body(number, title, body, comments, *, head=None, reviews=(), review_comments=()):
    record = {"pr" if head is not None else "issue": number, "content_sha256": content_sha(title, body),
              "comments": comment_digests(comments)}
    if head is not None:
        record |= {"head_sha": head, "reviews": comment_digests(reviews),
                   "review_comments": comment_digests(review_comments)}
    return f"{MARKER}\n\n```json\n{json.dumps(record, indent=2)}\n```\n"


def comment_digests(comments):
    return [{"id": c["id"], "body_sha256": body_sha(c["body"])}
            for c in sorted(comments, key=lambda c: c["id"])]


def parse_approval(body, number, kind="issue"):
    """Malformed or unsupported records grant nothing, even from maintainers."""
    if not body.startswith(f"{MARKER}\n\n```json\n") or not body.endswith("\n```\n"):
        return None
    try:
        record = json.loads(body[len(MARKER) + len("\n\n```json\n"):-len("\n```\n")])
        if not isinstance(record, dict):
            return None
        fields = {kind, "content_sha256", "comments"}
        if kind == "pr":
            fields |= {"head_sha", "reviews", "review_comments"}
            if not isinstance(record.get("head_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", record["head_sha"]):
                return None
        if (set(record) != fields
                or not positive_int(record[kind]) or record[kind] != number
                or not isinstance(record["content_sha256"], str)
                or not SHA256.fullmatch(record["content_sha256"])
                or not isinstance(record["comments"], list)):
            return None
        for name in ("comments", "reviews", "review_comments") if kind == "pr" else ("comments",):
            if not isinstance(record[name], list):
                return None
            ids = set()
            for comment in record[name]:
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
    def __init__(self, github, role=None, *, trusted_bots=(), strict=False):
        self.github, self.cache = github, {}
        self.role = role or github.role
        self.bots = {login.casefold() for login in trusted_bots}
        self.strict = strict

    def listed_bot(self, actor):
        return (isinstance(actor, dict) and
                isinstance(actor.get("login"), str) and actor["login"].casefold() in self.bots and
                (actor.get("type") == "Bot" or actor.get("__typename") == "Bot"))

    def feedback(self, actor):
        return self.listed_bot(actor) or self(actor) in TRUSTED

    def __call__(self, actor):
        login = (actor or {}).get("login")
        if not isinstance(login, str) or not login:
            return None
        # Feedback trust never gives a listed bot start/approval/edit authority.
        if self.listed_bot(actor):
            return "none"
        key = login.casefold()
        if key not in self.cache:
            self.cache[key] = self.role(login)
        if self.strict and self.cache[key] is None:
            raise AgentError("Input author permissions are unreadable")
        return self.cache[key]


@dataclass(frozen=True)
class ApprovalCheck:
    allowed: bool
    reason: str
    cleared_comment_ids: frozenset[int] = frozenset()
    cleared_review_ids: frozenset[int] = frozenset()
    cleared_review_comment_ids: frozenset[int] = frozenset()
    snapshot: dict = field(default_factory=dict)
    gate: str | None = None
    gate_key: str | None = None


def resolve_policy(configured, visibility):
    """Resolve once per discovery pass; explicit policy needs no GitHub read."""
    if configured is not None:
        return configured, "config"
    value = visibility()
    if not isinstance(value, str) or value not in {"public", "private", "internal"}:
        raise AgentError("Repository visibility is unreadable")
    return ("on" if value == "public" else "off"), f"visibility ({value})"


def trusted_input(github, item, *, trusted_bots=(), strict_permissions=False):
    """Current content and write+ feedback, without approval or history reads."""
    roles = Roles(github, role=getattr(github, "current_role", None),
                  trusted_bots=trusted_bots, strict=strict_permissions)

    def trusted(row):
        if is_record(row):
            return False
        try:
            return roles.feedback(row.get("user"))
        except AgentError as exc:
            if strict_permissions or isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and exc.rate_limited):
                raise
            # Unreadable comment permissions exclude input, never park work.
            login = (row.get("user") or {}).get("login")
            if isinstance(login, str):
                roles.cache[login.casefold()] = None
            return False

    groups = {"comments": github.comments(item.number)}
    if item.kind == "pr":
        groups |= {"reviews": github.reviews(item.number),
                   "review_comments": github.review_comments(item.number)}
    snapshot = {"title": item.title, "body": item.body}
    snapshot.update({name: [row for row in rows if trusted(row)] for name, rows in groups.items()})
    snapshot["withheld_counts"] = {name: sum(not is_record(row) for row in rows) - len(snapshot[name])
                                   for name, rows in groups.items()}
    if item.kind == "pr":
        snapshot["head"] = item.head
    return ApprovalCheck(True, "Approvals off; only write+ feedback is input", snapshot=snapshot)


def filter_input(github, item, policy, trigger_labels, *, actor=None, launchers=None,
                 trusted_bots=(), read_only=False):
    """One filtering entry point for assignment contexts and read-only item reads.

    Reads skip pickup gates, but never skip input clearance or edit history.
    Permission uncertainty fails the whole read; pickup retains its existing
    treatment of unknown authors as outside input.
    """
    if policy == "off":
        return trusted_input(github, item, trusted_bots=trusted_bots, strict_permissions=read_only)
    try:
        return _check_input(github, item.number, set(trigger_labels), item.kind, actor, launchers,
                            trusted_bots=trusted_bots, read_only=read_only, expected=item)
    except (AgentError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and exc.rate_limited):
            raise
        if read_only:
            raise AgentError("Item approval history or permissions are unreadable; no input shown") from exc
        return ApprovalCheck(False, "Assignment approval history is unreadable; retry or ask a maintainer")


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
        if len(before) > 1 and seconds(before[-2]["editedAt"]) == seconds(revision["editedAt"]):
            return None  # Revision order within a second cannot prove the posted content.
        if revision.get("deletedAt") or not isinstance(revision.get("diff"), str):
            return None
        body = revision["diff"]
    elif content["lastEditedAt"] is None or seconds(content["lastEditedAt"]) < at:
        body = content["body"]
    else:
        return None
    return title, body


def check_issue(github, number, trigger_labels, *, trusted_bots=()):
    """Say whether work is authorized and which outside comments are input.

    The caller supplies the union of trigger labels for issue/either agents.
    This function performs reads only; pickup enforcement belongs to the caller.
    """
    try:
        return _check_input(github, number, set(trigger_labels), trusted_bots=trusted_bots)
    except (AgentError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and exc.rate_limited):
            raise
        return ApprovalCheck(False, "Issue approval history is unreadable; retry or ask a maintainer")


def check_pr(github, number, trigger_labels, actor=None, *, launchers=None, trusted_bots=()):
    """PR heads require explicit approval or accepted, eligible agent ancestry."""
    try:
        return _check_input(github, number, set(trigger_labels), "pr", actor or github.actor(), launchers,
                            trusted_bots=trusted_bots)
    except (AgentError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and exc.rate_limited):
            raise
        return ApprovalCheck(False, "PR approval history is unreadable; retry or ask a maintainer")


def is_record(comment):
    # Unsupported coordination versions remain machine comments, never input.
    return (comment["body"].startswith(("<!-- ub-agents:approval:", ACTION_MARKER)) or
            re.match(r"<!-- ub-agents:v[0-9]+ -->", comment["body"]) is not None)


def _check_input(github, number, trigger_labels, kind="issue", actor=None, launchers=None, *,
                 trusted_bots=(), read_only=False, expected=None):
    content = github.issue_content(number) if kind == "issue" else github.pr_content(number)
    if not isinstance(content["title"], str) or not isinstance(content["body"], str):
        raise ValueError("invalid content")
    if expected and (content["title"], content["body"], content.get("head")) != (
            expected.title, expected.body, expected.head):
        return ApprovalCheck(False, "Assignment changed while reading approval input; retry")
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
    roles = Roles(github, trusted_bots=trusted_bots, strict=read_only)
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
    approvals = []
    groups = {"comments": comments}
    if kind == "pr":
        groups |= {"reviews": github.reviews(number), "review_comments": github.review_comments(number)}
    outside_groups = {}
    for name, rows in groups.items():
        outside_groups[name] = []
        for row in rows:
            if not positive_int(row["id"]) or not isinstance(row["body"], str):
                raise ValueError("invalid input")
            seconds(row["created_at"])
            seconds(row["updated_at"])
            if not is_record(row) and not roles.feedback(row.get("user")):
                outside_groups[name].append(row)
    for comment in comments:
        if not comment["body"].startswith(MARKER):
            continue
        if not positive_int(comment["id"]) or not isinstance(comment["body"], str):
            raise ValueError("invalid comment")
        at, updated = seconds(comment["created_at"]), seconds(comment["updated_at"])
        role = roles(comment.get("user"))
        if role not in MAINTAINERS or updated != at:
            continue
        record = parse_approval(comment["body"], number, kind)
        if record is None:
            continue
        old = historical_content(content, renames, at)
        if old is not None and record["content_sha256"] == content_sha(*old):
            approvals.append((at, record))
    cleared_groups = {}
    for name, rows in outside_groups.items():
        cleared = set()
        for comment in rows:
            updated = seconds(comment["updated_at"])
            if any(max(updated, seconds(comment["created_at"])) < start for start in starts):
                cleared.add(comment["id"])
            for at, record in approvals:
                if updated >= at or seconds(comment["created_at"]) >= at:
                    continue
                if any(c["id"] == comment["id"] and c["body_sha256"] == body_sha(comment["body"])
                       for c in record[name]):
                    cleared.add(comment["id"])
        cleared_groups[name] = frozenset(cleared)
    snapshot = {"title": content["title"], "body": content["body"]}
    for name, rows in groups.items():
        snapshot[name] = [c for c in rows if not is_record(c) and
                          (roles.feedback(c.get("user")) or c["id"] in cleared_groups[name])]
    snapshot["withheld_counts"] = {name: sum(not is_record(c) for c in rows) - len(snapshot[name])
                                   for name, rows in groups.items()}
    if kind == "pr":
        snapshot["head"] = content["head"]
    def verdict(allowed, reason, gate=None, evidence=None):
        key = body_sha(json.dumps([kind, gate, evidence], sort_keys=True)) if gate else None
        return ApprovalCheck(allowed, reason, cleared_groups["comments"],
                             cleared_groups.get("reviews", frozenset()),
                             cleared_groups.get("review_comments", frozenset()), snapshot, gate, key)

    trusted_author = roles(content.get("author")) in TRUSTED
    # Only starts and valid records clear title/body for reads. Approving PR
    # reviews retain their existing pickup authority, without clearing input.
    clearance = max(starts + [at for at, _ in approvals], default=None)
    outside_edits = {
        "title": [(seconds(e["created_at"]), e) for e in renames
                  if roles(e.get("actor")) not in TRUSTED],
        "body": [(seconds(e["editedAt"]), e) for e in edits
                 if seconds(e["editedAt"]) > created and roles(e.get("editor")) not in TRUSTED]}
    for name, changes in outside_edits.items():
        reason = None
        if clearance is not None:
            if any(at >= clearance for at, _ in changes):
                reason = f"Outside {name} edit after approval; a maintainer must approve"
        elif not trusted_author:
            reason = "Outside author; no maintainer start or valid approval record"
        elif changes:
            reason = f"Outside {name} edit; a maintainer must approve"
        if reason:
            snapshot[name] = {"withheld": True, "reason": reason}
    if read_only:
        return verdict(True, "Filtered input; pickup authorization is not required")

    approving_reviews = []
    if kind == "pr" and trusted_author:
        for name in ("title", "body"):
            if isinstance(snapshot[name], dict):
                return verdict(False, snapshot[name]["reason"], "input", outside_edits[name])
    if kind == "pr" and trusted_author and not any(isinstance(snapshot[n], dict) for n in ("title", "body")):
        return verdict(True, "Trusted PR author; outside feedback requires clearance")
    if not starts:
        article = "an issue" if kind == "issue" else "a PR"
        return verdict(False, f"No maintainer has applied {article} agent's trigger label", "start")
    if kind == "pr":
        for review in groups["reviews"]:
            if roles(review.get("user")) in MAINTAINERS and review["state"] == "APPROVED":
                if not isinstance(review["commit_id"], str) or not re.fullmatch(r"[0-9a-f]{40}", review["commit_id"]):
                    raise ValueError("invalid reviewed head")
                approving_reviews.append((seconds(review["created_at"]), review["commit_id"]))
        if not eligible_head(content["head"], number, approving_reviews, approvals, starts,
                             comments, roles, actor, launchers=launchers,
                             allow_revisions=(content["head_repository"] is not None and
                                              content["head_repository"].casefold() == github.repository.casefold())):
            return verdict(False, "PR head is not approved; a maintainer must approve the current head",
                           "head", content["head"])
    latest = max(starts + [at for at, _ in approvals] + [at for at, _ in approving_reviews])
    for event in renames:
        if seconds(event["created_at"]) >= clearance and roles(event.get("actor")) not in TRUSTED:
            return verdict(False, "Outside title edit after approval; a maintainer must approve",
                           "input", [latest, event])
    for edit in edits:
        at = seconds(edit["editedAt"])
        if at > created and at >= clearance and roles(edit.get("editor")) not in TRUSTED:
            return verdict(False, "Outside body edit after approval; a maintainer must approve",
                           "input", [latest, edit])
    feedback = {name: [c for c in rows if max(seconds(c["updated_at"]), seconds(c["created_at"])) >= latest]
                for name, rows in outside_groups.items()}
    if kind == "pr" and any(feedback.values()):
        return verdict(False, "Outside PR feedback after approval; a maintainer must approve",
                       "input", [latest, feedback])
    return verdict(True, "Maintainer start approved; no later outside input edits")


def eligible_head(head, number, approving_reviews, approvals, starts, comments, roles, actor, *,
                  allow_revisions, launchers=None):
    """Follow accepted revisions in the base repository from eligible source claims."""
    eligible = {}
    for at, sha in approving_reviews + [(at, r["head_sha"]) for at, r in approvals]:
        eligible[sha] = min(at, eligible.get(sha, at))
    if head in eligible:
        return True
    # Reports observe the current head; they cannot prove who pushed it. Outside
    # fork authors can move their branch during a run, and agents cannot revise it.
    if not allow_revisions:
        return False
    trusted = LauncherTrust(roles.github, launchers, role=lambda login: roles({"login": login}))
    history = records(comments, trusted=lambda author: not roles.listed_bot(author) and trusted(author))
    changed = True
    while changed:
        changed = False
        for outcome in history:
            if (outcome["kind"] != "outcome" or outcome["assignment"] != number
                    or outcome.get("handoff") not in {None, number}
                    or outcome["status"] != "success" or not outcome.get("accepted")
                    or outcome.get("rejected") or not outcome.get("candidate_sha")):
                continue
            lease = lease_by_id(history, outcome["lease_id"])
            source = outcome.get("assignment_sha")
            if (not lease or not same_run(outcome, lease) or source not in eligible
                    or seconds(lease["created"]) < max(min(starts), eligible[source])
                    or lease.get("cleanup") == "unconfirmed"):
                continue
            # Acceptance alone is insufficient while the run can still push or fail.
            released = (lease["state"] == "released" and lease.get("result") == "success") or any(
                recovers(r, lease) and r["state"] == "released"
                and r.get("result") == "success" and r["agent"] == lease["agent"]
                and r["assignment"] == number and r.get("cleanup") != "unconfirmed" for r in history)
            sha, at = outcome["candidate_sha"], seconds(outcome["created"])
            if released and at < eligible.get(sha, float("inf")):
                eligible[sha] = at
                changed = True
    return head in eligible


def approve_issue(github, number, actor):
    """Print reviewed input and post one record; never grant a non-maintainer authority."""
    if not positive_int(number):
        raise AgentError("approve requires a positive issue number")
    if github.role(actor) not in MAINTAINERS:
        raise AgentError("approve requires a maintainer (maintain or admin repository role)")
    item = github.item(number)
    roles = Roles(github)
    groups = {"comments": github.comments(number)}
    if item.kind == "pr":
        groups |= {"reviews": github.reviews(number), "review_comments": github.review_comments(number)}
    outside = {name: [c for c in rows if not is_record(c) and roles(c.get("user")) not in TRUSTED]
               for name, rows in groups.items()}
    label = "Issue" if item.kind == "issue" else "PR"
    print(f"{label} #{number}: {item.title}\n\n{item.body}")
    if item.head:
        print(f"\nHead: {item.head}")
    for name, rows in outside.items():
        print(f"\nOutside {name.replace('_', ' ')}:")
        for comment in rows:
            label = {"comments": "Comment", "reviews": "Review", "review_comments": "Review comment"}[name]
            print(f"\n{label} {comment['id']} by @{(comment.get('user') or {}).get('login', 'unknown')}:\n{comment['body']}")
    print("", flush=True)
    # A concurrent edit between displaying input and posting must not be silently approved.
    current = github.item(number)
    refreshed = {"comments": github.comments(number)}
    if item.kind == "pr":
        refreshed |= {"reviews": github.reviews(number), "review_comments": github.review_comments(number)}
    if (current.title, current.body, current.head) != (item.title, item.body, item.head) or refreshed != groups:
        raise AgentError("Issue or PR changed while displaying approval input; rerun approve")
    return github.create_comment(number, approval_body(number, item.title, item.body, outside["comments"],
                                 head=item.head, reviews=outside.get("reviews", ()),
                                 review_comments=outside.get("review_comments", ())))
