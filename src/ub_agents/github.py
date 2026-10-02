"""Authenticated, unindexed GitHub reads through gh. Errors never mean no work."""

from dataclasses import dataclass
import json
import math
import re
import subprocess
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .errors import AgentError, GitHubError
from .records import iso, positive_int, seconds, timestamp

REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def response_parts(output):
    """Separate gh --include headers from JSON, retaining HTTP failure metadata."""
    output = output.replace("\r\n", "\n")
    if not output.startswith("HTTP/"):
        return None, {}, output
    header, separator, payload = output.partition("\n\n")
    match = re.fullmatch(r"HTTP/\d+(?:\.\d+)? (\d{3})(?: .*?)?", header.split("\n")[0])
    if not match or not separator:
        raise ValueError("invalid HTTP response headers")
    headers = {}
    for line in header.split("\n")[1:]:
        name, colon, value = line.partition(":")
        if not colon:
            raise ValueError("invalid HTTP response header")
        key = name.lower()
        headers[key] = f"{headers[key]}, {value.strip()}" if key in headers else value.strip()
    return int(match[1]), headers, payload


def failure_retry(status, headers, detail):
    """Only known transport/server errors and explicit rate-limit resets retry."""
    rate_limited = (status == 429 or (status == 403 and
                    (headers.get("x-ratelimit-remaining") == "0"
                     or "rate limit" in detail.lower())))
    if rate_limited:
        try:
            if "retry-after" in headers:
                delay = float(headers["retry-after"])
                if not math.isfinite(delay) or delay < 0:
                    return False, None
                reset = timestamp() + delay
            elif headers.get("x-ratelimit-remaining") == "0":
                reset = float(headers["x-ratelimit-reset"])
            else:
                return False, None
            return (True, reset) if math.isfinite(reset) and reset > 0 else (False, None)
        except (KeyError, ValueError, OverflowError):
            return False, None
    if status is not None and status >= 400:
        return 500 <= status <= 599, None
    transport = re.search(r"dial tcp|connection (?:refused|reset)|network is unreachable|"
                          r"no such host|i/o timeout|TLS handshake timeout|"
                          r"context deadline exceeded|Client.Timeout exceeded|"
                          r"timeout awaiting response headers|operation timed out|"
                          r"no route to host|broken pipe|(?:unexpected )?EOF\s*$", detail, re.IGNORECASE)
    return bool(transport), None


@dataclass(frozen=True)
class Item:
    number: int
    kind: str
    title: str
    body: str
    labels: frozenset[str]
    state: str
    created_at: str
    head: str | None = None
    branch: str | None = None
    milestone: int | None = None
    draft: bool = False
    total_blocked_by: int | None = None


@dataclass(frozen=True)
class Dependency:
    repository: str
    number: int
    state: str

    def reference(self, repository):
        return (f"#{self.number}" if self.repository.casefold() == repository.casefold()
                else f"{self.repository}#{self.number}")


def links_issue(pr, repository, number):
    link = f"https://github.com/{repository}/issues/{number}"
    return bool(re.search(rf"(?<![\w/])#{number}\b", pr.body) or link in pr.body)


def closing_issues(pr, repository):
    """Local closing references, requiring a supported keyword for each issue."""
    pattern = (r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?):?\s+"
               rf"(?:(?P<repo>{REPOSITORY})?#(?P<number>[1-9][0-9]*)\b"
               rf"|https://github\.com/(?P<url_repo>{REPOSITORY})/issues/(?P<url_number>[1-9][0-9]*)\b)")
    return {int(match["number"] or match["url_number"])
            for match in re.finditer(pattern, pr.body, re.IGNORECASE)
            if (match["repo"] or match["url_repo"] or repository).casefold() == repository.casefold()}


def dependency_total(data):
    """Optional read optimization; an absent or invalid summary means unknown."""
    summary = data.get("issue_dependencies_summary")
    fields = ("blocked_by", "blocking", "total_blocked_by", "total_blocking")
    if (not isinstance(summary, dict)
            or any(type(summary.get(key)) is not int or summary[key] < 0 for key in fields)
            or summary["blocked_by"] > summary["total_blocked_by"]
            or summary["blocking"] > summary["total_blocking"]):
        return None
    return summary["total_blocked_by"]


def parse_item(data, kind):
    try:
        seconds(data["created_at"])
        milestone = data["milestone"]["number"] if data.get("milestone") is not None else None
        if (not positive_int(data["number"])
                or not isinstance(data["title"], str)
                or not isinstance(data.get("body") or "", str)
                or data["state"] not in {"open", "closed"}
                or not isinstance(data["labels"], list)
                or (milestone is not None and not positive_int(milestone))):
            raise ValueError("invalid work item fields")
        if kind == "pr" and type(data["draft"]) is not bool:
            raise ValueError("invalid PR draft field")
        return Item(data["number"], kind, data["title"], data.get("body") or "",
                    frozenset(x["name"] for x in data["labels"]), data["state"], data["created_at"],
                    data["head"]["sha"] if kind == "pr" else None,
                    data["head"]["ref"] if kind == "pr" else None, milestone,
                    data["draft"] if kind == "pr" else False,
                    dependency_total(data) if kind == "issue" else None)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AgentError("Unreadable GitHub work item") from exc


class GitHub:
    def __init__(self, repository, runner=None):
        self.runner = runner
        self.repository = repository
        self.prefix = f"repos/{repository}"
        self._comment_cache = {}
        self._comment_since = None
        self.last_request = None

    def request(self, endpoint, method="GET", data=None, paginate=False, array=False):
        if paginate:
            parts = urlsplit(endpoint)
            query = dict(parse_qsl(parts.query)) | {"per_page": "100"}
            items, page = [], 1
            while True:
                query["page"] = str(page)
                address = urlunsplit(parts._replace(query=urlencode(query)))
                batch = self.request(address, array=True)
                items.extend(batch)
                if len(batch) < 100:
                    return items
                page += 1
        self.last_request = f"GitHub {method} {endpoint}"
        command = ["gh", "api", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", "--include", endpoint]
        if data is not None:
            command += ["--input", "-"]
        try:
            result = (self.runner or subprocess.run)(
                command, input=json.dumps(data) if data is not None else None,
                capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = GitHubError(method, endpoint, str(exc), retryable=isinstance(
                exc, (subprocess.TimeoutExpired, TimeoutError, ConnectionError)))
            error.probe_reason = ("timed out (20s)" if isinstance(exc, subprocess.TimeoutExpired)
                                  else "could not run")
            raise error from exc
        try:
            status, headers, payload = response_parts(result.stdout)
        except ValueError as exc:
            raise GitHubError(method, endpoint, f"Unreadable GitHub response: {exc}") from exc
        if result.returncode or (status is not None and status >= 400):
            detail = result.stderr.strip() or payload.strip() or f"HTTP {status}"
            retryable, reset_at = failure_retry(status, headers, detail)
            error = GitHubError(method, endpoint, detail, retryable=retryable, reset_at=reset_at)
            error.probe_reason = f"exit {result.returncode}"
            raise error
        if method == "DELETE" and not payload.strip():
            return None
        try:
            value = json.loads(payload)
            if not isinstance(value, list if array else dict):
                raise ValueError("expected an array" if array else "expected an object")
            return value
        except (ValueError, TypeError) as exc:
            raise GitHubError(method, endpoint, f"Unreadable GitHub response: {exc}") from exc

    def actor(self):
        data = self.request("user")
        if not isinstance(data.get("login"), str) or not data["login"]:
            raise AgentError("GitHub authentication returned no actor")
        return data["login"]

    def labels(self):
        rows = self.request(f"{self.prefix}/labels", paginate=True)
        if any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
               or not row["name"].strip() for row in rows):
            raise AgentError("Unreadable GitHub labels")
        return [row["name"] for row in rows]

    def create_label(self, name, description, color):
        return self.request(f"{self.prefix}/labels", "POST",
                            {"name": name, "description": description, "color": color})

    def observe(self):
        # Repository issues include PRs. Do not use indexed search or a fixed --limit.
        data = self.request(f"{self.prefix}/issues?state=open&sort=created&direction=asc&per_page=100",
                            paginate=True)
        items = []
        for raw in data:
            if not isinstance(raw, dict):
                raise AgentError("Unreadable GitHub issue list")
            item = parse_item(raw, "issue")
            if "pull_request" in raw:
                items.append(self.item(item.number, "pr"))
            else:
                items.append(item)
        return sorted(items, key=lambda item: item.number)

    def item(self, number, kind=None):
        if kind == "pr":
            return parse_item(self.request(f"{self.prefix}/pulls/{number}"), "pr")
        raw = self.request(f"{self.prefix}/issues/{number}")
        return self.item(number, "pr") if "pull_request" in raw else parse_item(raw, "issue")

    def active_milestone(self):
        data = self.request(f"{self.prefix}/milestones?state=open&per_page=100", paginate=True)
        active = []
        for raw in data:
            try:
                created = seconds(raw["created_at"])
                if (not positive_int(raw["number"]) or raw["state"] not in {"open", "closed"}
                        or type(raw["open_issues"]) is not int or raw["open_issues"] < 0):
                    raise ValueError("invalid milestone fields")
                # GitHub's milestone count includes open issues and pull requests.
                if raw["state"] == "open" and raw["open_issues"]:
                    active.append((created, raw["number"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError("Unreadable GitHub milestone") from exc
        return min(active)[1] if active else None

    def blocked_by(self, number):
        data = self.request(f"{self.prefix}/issues/{number}/dependencies/blocked_by", paginate=True)
        if not isinstance(data, list):
            raise AgentError(f"Unreadable GitHub dependencies for #{number}")
        blockers = []
        for raw in data:
            try:
                match = re.fullmatch(rf"https://api\.github\.com/repos/({REPOSITORY})/issues/([1-9][0-9]*)",
                                     raw["url"])
                if (not positive_int(raw["number"])
                        or raw["state"] not in {"open", "closed"} or match is None
                        or int(match[2]) != raw["number"] or "pull_request" in raw):
                    raise ValueError("invalid dependency fields")
                blockers.append(Dependency(match[1], raw["number"], raw["state"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError(f"Unreadable GitHub dependency for #{number}") from exc
        return blockers

    def comments(self, number):
        return self.request(f"{self.prefix}/issues/{number}/comments?per_page=100", paginate=True)

    def repository_comments(self):
        # Recovery must not depend on a trigger still being present or an item
        # still being open. Rebuild from GitHub on startup, then read edits/new
        # comments incrementally. Capture the cursor BEFORE scanning, with overlap.
        cursor = iso(int(timestamp()) - 60)
        since, comments = self._comment_since, {}
        while True:
            query = {"sort": "updated", "direction": "asc", "per_page": 100}
            if since is not None:
                query["since"] = since
            # Page offsets over update order skip rows when earlier comments move
            # or disappear. Always read the first page beyond this timestamp.
            batch = self.request(f"{self.prefix}/issues/comments?{urlencode(query)}", array=True)
            updated = []
            for comment in batch:
                if not isinstance(comment, dict) or type(comment.get("id")) is not int:
                    raise AgentError("Unreadable repository comment")
                updated.append(seconds(comment.get("updated_at")))
                comments[comment["id"]] = comment
            if len(batch) > 100 or updated != sorted(updated):
                raise AgentError("Repository comment page has invalid size or update ordering")
            if len(batch) < 100:
                break
            boundary = int(updated[-1]) - 1
            if since is not None and boundary <= seconds(since):
                raise AgentError("Cannot safely paginate a full comment page within one update second; "
                                 "discovery cursor retained")
            since = iso(boundary)
        # Commit the index and cursor only after the entire scan completes.
        self._comment_cache.update(comments)
        self._comment_since = cursor
        # Cached records only discover item numbers. Claims/status reread the
        # item's comments, so a deleted cached record never supplies authority.
        return sorted(self._comment_cache.values(), key=lambda comment: comment["id"])

    def create_comment(self, number, body):
        return self.request(f"{self.prefix}/issues/{number}/comments", "POST", {"body": body})

    def update_comment(self, comment_id, body):
        return self.request(f"{self.prefix}/issues/comments/{comment_id}", "PATCH", {"body": body})

    def default_branch(self):
        raw = self.request(self.prefix)
        if not isinstance(raw.get("default_branch"), str):
            raise AgentError("GitHub returned no default branch")
        return raw["default_branch"]

    def prs_for_branch(self, branch, state="open"):
        owner = self.repository.split("/", 1)[0]
        query = urlencode({"state": state, "head": f"{owner}:{branch}", "per_page": 100})
        return [parse_item(raw, "pr") for raw in
                self.request(f"{self.prefix}/pulls?{query}", paginate=True)]

    def add_labels(self, number, labels):
        self.request(f"{self.prefix}/issues/{number}/labels", "POST", {"labels": list(labels)}, array=True)

    def remove_label(self, number, label):
        self.request(f"{self.prefix}/issues/{number}/labels/{quote(label, safe='')}", "DELETE", array=True)
