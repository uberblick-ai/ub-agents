"""Read-only maintainer starts, approvals and filtered assignment snapshots."""

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata

from .bots import is_bot, listed_bot
from .errors import AgentError, GitHubError, LostOwnership
from .github import is_fork
from .notices import ACTION_MARKER
from .records import (lease_by_id,
                      positive_int, records, recovers, same_run, seconds)
from .trust import LauncherTrust

MAINTAINERS = {"maintain", "admin"}
TRUSTED = MAINTAINERS | {"write"}
HIDDEN_CONTENT_MARKER = "[hidden content removed]"


def body_sha(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def clean_outside_text(text):
    """Replace hidden markup for input only; approval evidence stays raw."""
    text = re.sub(r"<!--.*?(?:-->|\Z)", HIDDEN_CONTENT_MARKER, text, flags=re.DOTALL)
    parts = []
    in_format_run = False
    for char in text:
        if unicodedata.category(char) == "Cf":
            if not in_format_run:
                parts.append(HIDDEN_CONTENT_MARKER)
            in_format_run = True
        else:
            parts.append(char)
            in_format_run = False
    return "".join(parts)


class Roles:
    """Cache only within one observation; role changes affect the next check."""
    def __init__(self, github, role=None, *, trusted_bots=(), strict=False):
        self.github, self.cache = github, {}
        self.role = role or github.role
        self.bots = {login.casefold() for login in trusted_bots}
        self.strict = strict

    def listed_bot(self, actor):
        return listed_bot(actor, self.bots)

    def feedback(self, actor):
        return self.listed_bot(actor) or self(actor) in TRUSTED

    def __call__(self, actor):
        login = (actor or {}).get("login")
        if not isinstance(login, str) or not login:
            return None
        # GitHub bot identities never gain repository-role authority.
        # Their empty-role or "not a user" permission answers are not read failures.
        if is_bot(actor):
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
    outside_input: bool = False


def resolve_policy(configured, visibility):
    """Resolve once per discovery pass; explicit policy needs no GitHub read."""
    if configured is not None:
        return configured, "config"
    value = visibility()
    if not isinstance(value, str) or value not in {"public", "private", "internal"}:
        raise AgentError("Repository visibility is unreadable")
    return ("on" if value == "public" else "off"), f"visibility ({value})"


def pr_author_exclusions(logins, policy, github):
    """Listed PR authors have no launcher authority while approvals are on."""
    return logins if logins and resolve_policy(policy, lambda: github.visibility())[0] == "on" else ()


def trusted_input(github, item, *, trusted_bots=(), strict_permissions=False, raise_read_errors=False):
    """Current content and write+ feedback, without approval or history reads."""
    roles = Roles(github, role=getattr(github, "current_role", None),
                  trusted_bots=trusted_bots, strict=strict_permissions)

    def trusted(row):
        if is_record(row):
            return False
        try:
            return roles.feedback(row.get("user"))
        except AgentError as exc:
            if (strict_permissions or isinstance(exc, LostOwnership)
                    or (isinstance(exc, GitHubError) and (exc.rate_limited or raise_read_errors))):
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
                 sub_issue_labels=(), trusted_bots=(), trusted_pr_authors=(), read_only=False, raise_read_errors=False):
    """One filtering entry point for assignment contexts and read-only item reads.

    Reads skip pickup gates, but never skip input clearance or edit history.
    Permission uncertainty fails the whole read; pickup retains its existing
    treatment of unknown authors as outside input.
    """
    if policy == "off":
        return trusted_input(github, item, trusted_bots=trusted_bots, strict_permissions=read_only,
                             raise_read_errors=raise_read_errors)
    try:
        return _check_input(github, item.number, set(trigger_labels), item.kind, actor, launchers,
                            sub_issue_labels=sub_issue_labels, trusted_bots=trusted_bots,
                            trusted_pr_authors=trusted_pr_authors,
                            read_only=read_only, expected=item)
    except (AgentError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and (exc.rate_limited or raise_read_errors)):
            raise
        if read_only:
            raise AgentError("Item approval history or permissions are unreadable; no input shown") from exc
        return ApprovalCheck(False, "Assignment approval history is unreadable; retry or ask a maintainer")


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


def check_pr(github, number, trigger_labels, actor=None, *, launchers=None, trusted_bots=(), trusted_pr_authors=()):
    """Outside PR heads require approval or accepted, eligible agent ancestry."""
    try:
        return _check_input(github, number, set(trigger_labels), "pr", actor or github.actor(), launchers,
                            trusted_bots=trusted_bots, trusted_pr_authors=trusted_pr_authors)
    except (AgentError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, LostOwnership) or (isinstance(exc, GitHubError) and exc.rate_limited):
            raise
        return ApprovalCheck(False, "PR approval history is unreadable; retry or ask a maintainer")


def is_record(comment):
    # Unsupported coordination versions remain machine comments, never input.
    return (comment["body"].startswith(ACTION_MARKER) or
            re.match(r"<!-- ub-agents:v[0-9]+ -->", comment["body"]) is not None)


def validate_content(content):
    """Validate identical history completeness for both approval read paths."""
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


def _check_input(github, number, trigger_labels, kind="issue", actor=None, launchers=None, *,
                 sub_issue_labels=(), trusted_bots=(), trusted_pr_authors=(), read_only=False, expected=None):
    content = github.issue_content(number) if kind == "issue" else github.pr_content(number)
    if not isinstance(content["title"], str) or not isinstance(content["body"], str):
        raise ValueError("invalid content")
    if expected and (content["title"], content["body"], content.get("head")) != (
            expected.title, expected.body, expected.head):
        return ApprovalCheck(False, "Assignment changed while reading approval input; retry")
    validate_content(content)
    created, edits = seconds(content["createdAt"]), content["edits"]
    timeline = github.timeline(number)
    roles = Roles(github, trusted_bots=trusted_bots, strict=read_only)
    starts, renames, launcher_events = [], [], []
    for event in timeline:
        if event["event"] not in {"labeled", "renamed"}:
            continue
        at = seconds(event["created_at"])
        if event["event"] == "renamed":
            if not all(isinstance(event["rename"][key], str) for key in ("from", "to")):
                raise ValueError("invalid rename")
            renames.append(event)
        elif event["label"]["name"] in trigger_labels:
            if roles(event.get("actor")) in MAINTAINERS:
                starts.append(at)
            elif kind == "issue" and event["label"]["name"] in sub_issue_labels:
                launcher_events.append(event)
    inherited, pending = inherited_starts(github, number, launcher_events, roles, launchers)
    starts.extend(inherited)
    renames.sort(key=lambda e: seconds(e["created_at"]))
    comments = github.comments(number)
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
    cleared_groups = {}
    for name, rows in outside_groups.items():
        cleared = set()
        for comment in rows:
            updated = seconds(comment["updated_at"])
            if any(max(updated, seconds(comment["created_at"])) < start for start in starts):
                cleared.add(comment["id"])
        cleared_groups[name] = frozenset(cleared)
    snapshot = {"title": content["title"], "body": content["body"]}
    for name, rows in groups.items():
        snapshot[name] = [c | {"body": clean_outside_text(c["body"])}
                          if c["id"] in cleared_groups[name] else c
                          for c in rows if not is_record(c) and
                          (roles.feedback(c.get("user")) or c["id"] in cleared_groups[name])]
    snapshot["withheld_counts"] = {name: sum(not is_record(c) for c in rows) - len(snapshot[name])
                                   for name, rows in groups.items()}
    if kind == "pr":
        snapshot["head"] = content["head"]
    def verdict(allowed, reason, gate=None, evidence=None):
        key = body_sha(json.dumps([kind, gate, evidence], sort_keys=True)) if gate else None
        return ApprovalCheck(allowed, reason, cleared_groups["comments"],
                             cleared_groups.get("reviews", frozenset()),
                             cleared_groups.get("review_comments", frozenset()), snapshot, gate, key,
                             outside_input)

    author = content.get("author")
    trusted_bot_author = (kind == "pr" and listed_bot(author, trusted_pr_authors)
                         and not is_fork(content["head_repository"], github.repository))
    trusted_author = roles(author) in TRUSTED or trusted_bot_author

    def trusted_edit(editor):
        return (roles(editor) in TRUSTED or
                (trusted_bot_author and listed_bot(editor, (author["login"],))))

    # Only starts clear title/body for reads. Approving PR
    # reviews retain their existing pickup authority, without clearing input.
    clearance = max(starts, default=None)
    outside_edits = {
        "title": [(seconds(e["created_at"]), e) for e in renames
                  if not trusted_edit(e.get("actor"))],
        "body": [(seconds(e["editedAt"]), e) for e in edits
                 if seconds(e["editedAt"]) > created and not trusted_edit(e.get("editor"))]}
    blocked_fields = {}
    for name, changes in outside_edits.items():
        reason = None
        if clearance is not None:
            if any(at >= clearance for at, _ in changes):
                reason = f"Outside {name} edit after approval; a maintainer must re-apply a trigger label"
        elif not trusted_author:
            reason = "Outside author; no maintainer start"
        elif changes:
            reason = f"Outside {name} edit; a maintainer must re-apply a trigger label"
        if reason:
            blocked_fields[name] = reason
            snapshot[name] = {"withheld": True, "reason": reason}
        elif not trusted_author or trusted_bot_author or changes:
            snapshot[name] = clean_outside_text(snapshot[name])
    outside_input = (trusted_bot_author or any(cleared_groups.values()) or any(
        (not trusted_author or outside_edits[name]) and isinstance(snapshot[name], str)
        for name in ("title", "body")))
    if trusted_bot_author:
        # Upstream release notes in bot bodies are never assignment input,
        # even after a maintainer start clears another account's body edit.
        snapshot["body"] = {"withheld": True, "reason": "Body by a trusted bot author is not input"}
    if read_only:
        return verdict(True, "Filtered input; pickup authorization is not required")

    approving_reviews = []
    if kind == "pr" and trusted_author:
        for name in ("title", "body"):
            if name in blocked_fields:
                return verdict(False, blocked_fields[name], "input", outside_edits[name])
        return verdict(True, "Trusted PR author; outside feedback requires clearance")
    if not starts:
        if pending:
            return verdict(False, "Sub-issue start is pending completion of its parent outcome")
        article = "an issue" if kind == "issue" else "a PR"
        return verdict(False, f"No maintainer has applied {article} agent's trigger label", "start")
    if kind == "pr":
        for review in groups["reviews"]:
            if roles(review.get("user")) in MAINTAINERS and review["state"] == "APPROVED":
                if not isinstance(review["commit_id"], str) or not re.fullmatch(r"[0-9a-f]{40}", review["commit_id"]):
                    raise ValueError("invalid reviewed head")
                approving_reviews.append((seconds(review["created_at"]), review["commit_id"]))
        if not eligible_head(content["head"], number, approving_reviews, starts,
                             comments, roles, actor, launchers=launchers,
                             trusted_pr_authors=trusted_pr_authors,
                             allow_revisions=not is_fork(content["head_repository"], github.repository)):
            return verdict(False, "PR head is not approved; a maintainer must submit an approving review of the current head",
                           "head", content["head"])
    latest = max(starts + [at for at, _ in approving_reviews])
    for event in renames:
        if seconds(event["created_at"]) >= clearance and roles(event.get("actor")) not in TRUSTED:
            return verdict(False, "Outside title edit after approval; a maintainer must re-apply a trigger label",
                           "input", [latest, event])
    for edit in edits:
        at = seconds(edit["editedAt"])
        if at > created and at >= clearance and roles(edit.get("editor")) not in TRUSTED:
            return verdict(False, "Outside body edit after approval; a maintainer must re-apply a trigger label",
                           "input", [latest, edit])
    feedback = {name: [c for c in rows if max(seconds(c["updated_at"]), seconds(c["created_at"])) >= latest]
                for name, rows in outside_groups.items()}
    if kind == "pr" and any(feedback.values()):
        return verdict(False, "Outside PR feedback after approval; a maintainer must re-apply a trigger label",
                       "input", [latest, feedback])
    return verdict(True, "Maintainer start approved; no later outside input edits")


def inherited_starts(github, number, events, roles, launchers):
    """Only accepted parent transitions authorize launcher-applied issue starts."""
    trusted = LauncherTrust(github, launchers, role=lambda login: roles({"login": login}),
                            trusted_bots=roles.bots)

    def launcher(author):
        # Keep bot/deleted-user handling identical to direct starts. Turning
        # an actor into just a login would lose its bot attribution.
        return roles(author) in TRUSTED and trusted(author)

    events = [e for e in events if launcher(e.get("actor"))]
    if not events:
        return [], False
    parent = github.parent_issue(number)
    if parent is None or parent.repository.casefold() != github.repository.casefold():
        return [], False
    history = records(github.comments(parent.number), trusted=launcher)
    starts, pending = [], False
    for outcome in history:
        if (outcome["kind"] != "outcome" or outcome["assignment"] != parent.number
                or outcome["status"] != "success" or outcome.get("rejected")
                or number not in outcome.get("sub_issues", ())):
            continue
        lease = lease_by_id(history, outcome["lease_id"])
        transition = outcome.get("transition", {})
        if (not lease or not same_run(outcome, lease) or lease.get("cleanup") == "unconfirmed"
                or not transition.get("started") or "sub-issues" not in transition):
            continue
        # Timeline events have second precision; the label can be applied in
        # the same second as the higher precision durable outcome timestamp.
        matching = [seconds(e["created_at"]) for e in events
                    if e["label"]["name"] in transition["sub-issues"]["add"]
                    and seconds(e["created_at"]) >= int(seconds(outcome["created"]))]
        releases = [seconds(r["expires"]) for r in history
                    if (r["id"] == lease["id"] or recovers(r, lease))
                    and r["kind"] == "lease" and r["state"] == "released" and r.get("result") == "success"
                    and r.get("cleanup") != "unconfirmed"]
        if outcome.get("accepted") and outcome.get("transition_complete") and releases:
            # A later unrelated reapplication must not clear outside edits on
            # the authority of an old parent transition.
            starts.extend(at for at in matching if at <= int(max(releases)))
        elif matching and lease.get("result") not in {"retry", "blocked"}:
            pending = True
    return starts, pending


def eligible_head(head, number, approving_reviews, starts, comments, roles, actor, *,
                  allow_revisions, launchers=None, trusted_pr_authors=()):
    """Follow accepted revisions in the base repository from eligible source claims."""
    eligible = {}
    for at, sha in approving_reviews:
        eligible[sha] = min(at, eligible.get(sha, at))
    if head in eligible:
        return True
    # Reports observe the current head; they cannot prove who pushed it. Outside
    # fork authors can move their branch during a run, and agents cannot revise it.
    if not allow_revisions:
        return False
    trusted = LauncherTrust(roles.github, launchers, role=lambda login: roles({"login": login}),
                           trusted_pr_authors=trusted_pr_authors)
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
            source = lease.get("assignment_sha") if lease else None
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
