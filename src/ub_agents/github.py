"""Authenticated, unindexed GitHub reads through gh. Errors never mean no work."""

from dataclasses import dataclass, replace
import json
import math
import re
import signal
import subprocess
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .errors import AgentError, GitHubError
from .records import iso, positive_int, seconds, timestamp

REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def repository_visibility(raw):
    visibility = raw.get("visibility") if isinstance(raw, dict) else None
    if not isinstance(visibility, str) or visibility not in {"public", "private", "internal"}:
        raise AgentError("Repository visibility is unreadable")
    return visibility


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


RATE_LIMIT_MARGIN_SECONDS = 5
RATE_LIMIT_FALLBACK_SECONDS = 60
RATE_LIMIT_MAX_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 20


def is_rate_limit(status, headers, detail):
    message = detail.lower()
    return status in {403, 429} and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        or "api rate limit exceeded" in message or "secondary rate limit" in message)


def failure_retry(status, headers, detail):
    """Known rate limits, transport failures and server errors may retry."""
    if is_rate_limit(status, headers, detail):
        try:
            if headers.get("x-ratelimit-remaining") == "0" or "api rate limit exceeded" in detail.lower():
                reset = float(headers["x-ratelimit-reset"])
                if not math.isfinite(reset) or reset <= 0:
                    raise ValueError("invalid reset")
                return True, reset + RATE_LIMIT_MARGIN_SECONDS
            delay = float(headers["retry-after"])
            if not math.isfinite(delay) or delay < 0:
                raise ValueError("invalid retry-after")
            return True, timestamp() + delay
        except (KeyError, ValueError, OverflowError):
            return True, timestamp() + RATE_LIMIT_FALLBACK_SECONDS
    if status is not None and status >= 400:
        return 500 <= status <= 599, None
    transport = re.search(r"dial tcp|connection (?:refused|reset)|network is unreachable|"
                          r"no such host|i/o timeout|TLS handshake timeout|"
                          r"context deadline exceeded|Client.Timeout exceeded|"
                          r"timeout awaiting response headers|operation timed out|"
                          r"no route to host|broken pipe|unexpected end of JSON input|"
                          r"(?:unexpected )?EOF\s*$", detail, re.IGNORECASE)
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
    updated_at: str | None = None
    open_blocked_by: int | None = None
    author: str | None = None


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


def parse_item(data, kind, endpoint=None):
    try:
        seconds(data["created_at"])
        if data.get("updated_at") is not None:
            seconds(data["updated_at"])
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
                    dependency_total(data) if kind == "issue" else None, data.get("updated_at"),
                    data["issue_dependencies_summary"]["blocked_by"]
                    if kind == "issue" and dependency_total(data) is not None else None,
                    data["user"].get("login") if isinstance(data.get("user"), dict)
                    and isinstance(data["user"].get("login"), str) else None)
    except (KeyError, TypeError, ValueError, AttributeError, AgentError) as exc:
        if endpoint is not None:
            raise GitHubError("GET", endpoint, "Unreadable GitHub work item") from exc
        raise AgentError("Unreadable GitHub work item") from exc


class GitHub:
    def __init__(self, repository, runner=None):
        self.runner = runner
        self.repository = repository
        self.prefix = f"repos/{repository}"
        self.quota_headers = {}
        self.resource_quotas = {}
        # All attempted REST calls, including revalidations and transport failures.
        self.rest_requests = 0
        # REST responses that consume quota; GraphQL has a separate budget.
        self.quota_requests = 0
        self.rate_limited = False
        self._etag_cache = {}
        self._comment_cache = {}
        self._comment_since = None

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
        command = ["gh", "api", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", "--include", endpoint]
        if data is not None:
            command += ["--input", "-"]
        rest = urlsplit(endpoint).path.rstrip("/") not in {"graphql", "/graphql"}
        conditional = rest and method == "GET"
        # Discovery's moving since cursor produces one-shot URLs. Retaining
        # their validators and payloads would grow memory on every poll.
        cacheable = conditional and "since" not in dict(
            parse_qsl(urlsplit(endpoint).query, keep_blank_values=True))
        cached = self._etag_cache.get(endpoint) if cacheable else None
        status, headers, payload = self._response(command, endpoint, method, data, cached)
        if status == 304:
            if not conditional:
                raise GitHubError(method, endpoint, "Unexpected HTTP 304 for an unconditional operation")
            if cached is None:
                # A bodyless response cannot establish authority. Retry only once,
                # with no validator, even if gh incorrectly returns another 304.
                status, headers, payload = self._response(command, endpoint, method, data, None)
                if status == 304:
                    raise GitHubError(method, endpoint, "HTTP 304 without a stored response after refetch")
            else:
                etag, payload = cached
                self._etag_cache[endpoint] = (headers.get("etag") or etag, payload)
        if method == "DELETE" and not payload.strip():
            return None
        try:
            value = json.loads(payload)
            if not isinstance(value, list if array else dict):
                raise ValueError("expected an array" if array else "expected an object")
        except (ValueError, TypeError) as exc:
            raise GitHubError(method, endpoint, f"Unreadable GitHub response: {exc}") from exc
        if cacheable and status != 304:
            # Keep the wire payload so callers cannot mutate later cache hits.
            if headers.get("etag"):
                self._etag_cache[endpoint] = (headers["etag"], payload)
            else:
                self._etag_cache.pop(endpoint, None)
        return value

    def _response(self, command, endpoint, method, data, cached):
        if cached is not None:
            command = command + ["-H", f"If-None-Match: {cached[0]}"]
        if urlsplit(endpoint).path.rstrip("/") not in {"graphql", "/graphql"}:
            self.rest_requests += 1
        try:
            result = (self.runner or subprocess.run)(
                command, input=json.dumps(data) if data is not None else None,
                capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = GitHubError(method, endpoint, str(exc), retryable=isinstance(
                exc, (subprocess.TimeoutExpired, TimeoutError, ConnectionError)))
            error.probe_reason = ("timed out (20s)" if isinstance(exc, subprocess.TimeoutExpired)
                                  else "could not run")
            raise error from exc
        except UnicodeError as exc:
            raise GitHubError(method, endpoint, f"Unreadable GitHub response: {exc}") from exc
        # Launch defers signal handling until owned execution can be cleaned up.
        # gh shares the terminal's foreground group and can exit first on Ctrl-C.
        if result.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
            raise KeyboardInterrupt
        try:
            status, headers, payload = response_parts(result.stdout)
        except ValueError as exc:
            raise GitHubError(method, endpoint, f"Unreadable GitHub response: {exc}") from exc
        # GraphQL has its own quota; doctor reports the REST account quota.
        if urlsplit(endpoint).path.rstrip("/") not in {"graphql", "/graphql"}:
            if status != 304 and (status is not None or result.returncode == 0):
                self.quota_requests += 1
            if "x-ratelimit-remaining" in headers:
                self.quota_headers = headers
        resource = headers.get("x-ratelimit-resource")
        if resource in {"core", "graphql"}:
            self.resource_quotas[resource] = headers
        # gh exits 1 for a 304 and writes "gh: HTTP 304" to stderr. The
        # server's freshness confirmation takes precedence over that exit code.
        if status == 304:
            return status, headers, payload
        self.rate_limited |= is_rate_limit(status, headers, result.stderr + payload)
        if result.returncode or (status is not None and status >= 400):
            detail = result.stderr.strip() or payload.strip() or f"HTTP {status}"
            limited = is_rate_limit(status, headers, detail + " " + payload)
            retryable, reset_at = failure_retry(status, headers, detail + " " + payload if limited else detail)
            error = GitHubError(method, endpoint, detail, retryable=retryable, reset_at=reset_at,
                                rate_limited=limited)
            error.probe_reason = f"exit {result.returncode}"
            raise error
        return status, headers, payload

    def actor(self):
        data = self.request("user")
        if not isinstance(data.get("login"), str) or not data["login"]:
            raise GitHubError("GET", "user", "GitHub authentication returned no actor")
        return data["login"]

    def role(self, login):
        """Unknown/unreadable roles never grant input or approval authority."""
        if not isinstance(login, str) or not login:
            return None
        try:
            raw = self.request(f"{self.prefix}/collaborators/{quote(login, safe='')}/permission")
        except AgentError as exc:
            if isinstance(exc, GitHubError) and exc.rate_limited:
                raise
            return None
        role = raw.get("role_name") if isinstance(raw, dict) else None
        if role == "":
            return "none"
        return role if isinstance(role, str) and role in {"admin", "maintain", "write", "triage", "read", "none"} else None

    def visibility(self):
        return repository_visibility(self.request(self.prefix))

    def timeline(self, number):
        return self.request(f"{self.prefix}/issues/{number}/timeline", paginate=True)

    def issue_content(self, number):
        return self._content(number, "issue")

    def pr_content(self, number):
        return self._content(number, "pullRequest")

    def _content(self, number, kind):
        """Read current content and every body revision, including its editor.

        GitHub's userContentEdits.diff contains the body at that revision, despite
        its name. Deleted revisions have a null diff and cannot prove a digest.
        """
        owner, name = self.repository.split("/", 1)
        query = """query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
          repository(owner:$owner, name:$name) { issue(number:$number) {
            title body createdAt lastEditedAt author { login __typename }
            userContentEdits(first:100, after:$cursor) {
              nodes { editedAt editor { login __typename } diff deletedAt }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        if kind == "pullRequest":
            query = query.replace("issue(number:", "pullRequest(number:").replace(
                "author { login __typename }",
                "author { login __typename } headRefOid headRepository { nameWithOwner }")
        edits, cursor, content, seen = [], None, None, set()
        while True:
            raw = self.request("graphql", "POST", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            try:
                if raw.get("errors"):
                    raise ValueError("GraphQL errors")
                issue = raw["data"]["repository"][kind]
                current = {key: issue[key] for key in ("title", "body", "createdAt", "lastEditedAt")}
                current["author"] = issue.get("author")
                if kind == "pullRequest":
                    head_repository = issue["headRepository"]
                    head_repository = head_repository["nameWithOwner"] if head_repository is not None else None
                    if head_repository is not None and (not isinstance(head_repository, str)
                                                       or not re.fullmatch(REPOSITORY, head_repository)):
                        raise ValueError("invalid PR head repository")
                    current |= {"author": issue["author"], "head": issue["headRefOid"],
                                "head_repository": head_repository}
                    if not isinstance(current["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", current["head"]):
                        raise ValueError("invalid PR head")
                if content is not None and current != content:
                    raise ValueError("issue changed while reading history")
                content = current
                connection = issue["userContentEdits"]
                if not isinstance(connection["nodes"], list):
                    raise ValueError("invalid edit history")
                edits.extend(connection["nodes"])
                page = connection["pageInfo"]
                if type(page["hasNextPage"]) is not bool:
                    raise ValueError("invalid pagination")
                if not page["hasNextPage"]:
                    return content | {"edits": edits}
                if not isinstance(page["endCursor"], str) or page["endCursor"] in seen:
                    raise ValueError("invalid edit cursor")
                cursor = page["endCursor"]
                seen.add(cursor)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("POST", "graphql", f"Unreadable {'issue' if kind == 'issue' else 'PR'} content history") from exc

    def labels(self):
        rows = self.request(f"{self.prefix}/labels", paginate=True)
        if any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
               or not row["name"].strip() for row in rows):
            raise GitHubError("GET", f"{self.prefix}/labels", "Unreadable GitHub labels")
        return [row["name"] for row in rows]

    def create_label(self, name, description, color):
        return self.request(f"{self.prefix}/labels", "POST",
                            {"name": name, "description": description, "color": color})

    def observe(self, details=True):
        # Repository issues include PRs. Do not use indexed search or a fixed --limit.
        endpoint = f"{self.prefix}/issues?state=open&sort=created&direction=asc&per_page=100"
        data = self.request(endpoint, paginate=True)
        items = []
        for raw in data:
            if not isinstance(raw, dict):
                raise GitHubError("GET", endpoint, "Unreadable GitHub issue list")
            item = parse_item(raw, "issue", endpoint)
            if "pull_request" in raw:
                items.append(self.item(item.number, "pr") if details else
                             replace(item, kind="pr", total_blocked_by=None, open_blocked_by=None))
            else:
                items.append(item)
        return sorted(items, key=lambda item: item.number)

    def item(self, number, kind=None):
        endpoint = f"{self.prefix}/{'pulls' if kind == 'pr' else 'issues'}/{number}"
        raw = self.request(endpoint)
        try:
            if kind == "pr":
                repository = ((raw.get("base") or {}).get("repo") or {}).get("full_name")
            else:
                url = raw.get("repository_url")
                repository = url.rsplit("/repos/", 1)[-1] if url is not None else None
            if repository is not None and repository.casefold() != self.repository.casefold():
                raise GitHubError("GET", endpoint, "Item is outside the configured repository")
        except (TypeError, AttributeError) as exc:
            raise GitHubError("GET", endpoint, "Unreadable GitHub work item scope") from exc
        if kind != "pr" and "pull_request" in raw:
            return self.item(number, "pr")
        return parse_item(raw, kind or "issue", endpoint)

    def active_milestone(self):
        milestones = self.milestone_order()
        return milestones[0] if milestones else None

    def milestone_order(self):
        endpoint = f"{self.prefix}/milestones?state=open&per_page=100"
        data = self.request(endpoint, paginate=True)
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
            except (KeyError, TypeError, ValueError, AgentError) as exc:
                raise GitHubError("GET", endpoint, "Unreadable GitHub milestone") from exc
        return tuple(number for _, number in sorted(active))

    def dependency_graph(self):
        """List open issues and their links together for cold queue ranking."""
        owner, name = self.repository.split("/", 1)
        query = """query($owner:String!, $name:String!, $cursor:String) {
          repository(owner:$owner, name:$name) {
            issues(first:100, states:OPEN, after:$cursor) {
              nodes { number blockedBy(first:100) {
                nodes { number state repository { nameWithOwner } }
                pageInfo { hasNextPage }
              } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }"""
        graph, cursor, seen = {}, None, set()
        while True:
            data = self.graphql(query, {"owner": owner, "name": name, "cursor": cursor})
            try:
                connection = data["repository"]["issues"]
                if not isinstance(connection["nodes"], list) or len(connection["nodes"]) > 100:
                    raise ValueError("invalid dependency graph page")
                for issue in connection["nodes"]:
                    number, links = issue["number"], issue["blockedBy"]
                    if not positive_int(number) or number in graph:
                        raise ValueError("invalid dependency graph issue")
                    if (not isinstance(links["nodes"], list) or len(links["nodes"]) > 100
                            or type(links["pageInfo"]["hasNextPage"]) is not bool):
                        raise ValueError("invalid dependency connection")
                    blockers = []
                    for node in links["nodes"]:
                        repository = node["repository"]["nameWithOwner"]
                        if (not positive_int(node["number"]) or node["state"] not in {"OPEN", "CLOSED"}
                                or not isinstance(repository, str) or not re.fullmatch(REPOSITORY, repository)):
                            raise ValueError("invalid graph dependency")
                        blockers.append(Dependency(repository, node["number"], node["state"].lower()))
                    # Never truncate a large dependency connection.
                    graph[number] = self.blocked_by(number) if links["pageInfo"]["hasNextPage"] else blockers
                page = connection["pageInfo"]
                if type(page["hasNextPage"]) is not bool:
                    raise ValueError("invalid graph pagination")
                if not page["hasNextPage"]:
                    return graph
                cursor = page["endCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("invalid graph cursor")
                seen.add(cursor)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("POST", "graphql", "Unreadable GitHub dependency graph") from exc

    def blocked_by(self, number):
        endpoint = f"{self.prefix}/issues/{number}/dependencies/blocked_by"
        data = self.request(endpoint, paginate=True)
        if not isinstance(data, list):
            raise GitHubError("GET", endpoint, f"Unreadable GitHub dependencies for #{number}")
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
                raise GitHubError("GET", endpoint, f"Unreadable GitHub dependency for #{number}") from exc
        return blockers

    def comments(self, number):
        return self.request(f"{self.prefix}/issues/{number}/comments?per_page=100", paginate=True)

    def review_comments(self, number):
        return self.request(f"{self.prefix}/pulls/{number}/comments?per_page=100", paginate=True)

    def reviews(self, number):
        """Include review edit times, which REST's submitted_at alone cannot prove."""
        owner, name = self.repository.split("/", 1)
        query = """query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
          repository(owner:$owner, name:$name) { pullRequest(number:$number) {
            reviews(first:100, after:$cursor) {
              nodes { databaseId body author { login __typename } submittedAt lastEditedAt state commit { oid } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        rows, cursor, seen = [], None, set()
        while True:
            data = self.graphql(query, {"owner": owner, "name": name, "number": number, "cursor": cursor})
            try:
                connection = data["repository"]["pullRequest"]["reviews"]
                if not isinstance(connection["nodes"], list):
                    raise ValueError("invalid reviews")
                for node in connection["nodes"]:
                    # Pending reviews are private, unsubmitted input.
                    if node["state"] == "PENDING":
                        continue
                    seconds(node["submittedAt"])
                    updated = max((node["lastEditedAt"] or node["submittedAt"], node["submittedAt"]),
                                  key=seconds)
                    if not positive_int(node["databaseId"]) or not isinstance(node["body"], str):
                        raise ValueError("invalid review")
                    rows.append({"id": node["databaseId"], "body": node["body"], "user": node["author"],
                                 "created_at": node["submittedAt"], "updated_at": updated,
                                 "state": node["state"], "commit_id": node["commit"]["oid"]})
                page = connection["pageInfo"]
                if type(page["hasNextPage"]) is not bool:
                    raise ValueError("invalid pagination")
                if not page["hasNextPage"]:
                    return rows
                if not isinstance(page["endCursor"], str) or page["endCursor"] in seen:
                    raise ValueError("invalid review cursor")
                cursor = page["endCursor"]
                seen.add(cursor)
            except (KeyError, TypeError, ValueError, AgentError) as exc:
                raise GitHubError("POST", "graphql", "Unreadable PR reviews") from exc

    def repository_comments(self, lookback_seconds=None):
        # Bound discovery's first scan; cleanup leaves lookback unset for a full
        # history. Later scans read edits/new comments incrementally. Capture the
        # cursor BEFORE scanning, with overlap.
        now = timestamp()
        cursor = iso(int(now) - 60)
        since, comments = self._comment_since, {}
        if since is None and lookback_seconds is not None:
            since = iso(now - lookback_seconds)
        while True:
            query = {"sort": "updated", "direction": "asc", "per_page": 100}
            if since is not None:
                query["since"] = since
            # Page offsets over update order skip rows when earlier comments move
            # or disappear. Always read the first page beyond this timestamp.
            endpoint = f"{self.prefix}/issues/comments?{urlencode(query)}"
            batch = self.request(endpoint, array=True)
            updated = []
            for comment in batch:
                if not isinstance(comment, dict) or type(comment.get("id")) is not int:
                    raise GitHubError("GET", endpoint, "Unreadable repository comment")
                try:
                    updated.append(seconds(comment.get("updated_at")))
                except AgentError as exc:
                    raise GitHubError("GET", endpoint, str(exc)) from exc
                comments[comment["id"]] = comment
            if len(batch) > 100 or updated != sorted(updated):
                raise GitHubError("GET", endpoint, "Repository comment page has invalid size or update ordering")
            if len(batch) < 100:
                break
            boundary = int(updated[-1]) - 1
            if since is not None and boundary <= seconds(since):
                raise GitHubError("GET", endpoint,
                                  "Cannot safely paginate a full comment page within one update second; "
                                  "discovery cursor retained")
            since = iso(boundary)
        # Commit the index and cursor only after the entire scan completes.
        self._comment_cache.update(comments)
        if lookback_seconds is not None:
            cutoff = now - lookback_seconds
            self._comment_cache = {comment_id: comment for comment_id, comment in self._comment_cache.items()
                                   if seconds(comment["updated_at"]) >= cutoff}
        self._comment_since = cursor
        # Cached comments invalidate discovery reads. Claims and writes reread
        # the item, so a deleted cached record never supplies authority.
        return sorted(self._comment_cache.values(), key=lambda comment: comment["id"])

    def create_comment(self, number, body):
        return self.request(f"{self.prefix}/issues/{number}/comments", "POST", {"body": body})

    def update_comment(self, comment_id, body):
        return self.request(f"{self.prefix}/issues/comments/{comment_id}", "PATCH", {"body": body})

    def delete_comment(self, comment_id):
        return self.request(f"{self.prefix}/issues/comments/{comment_id}", "DELETE")

    def graphql(self, query, variables):
        result = self.request("graphql", "POST", {"query": query, "variables": variables})
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise GitHubError("POST", "graphql", f"Unreadable GraphQL response: {result.get('errors')}")
        return result["data"]

    def discussion(self, number):
        owner, name = self.repository.split("/")
        data = self.graphql(
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "discussion(number:$number){id url}}}",
            {"owner": owner, "name": name, "number": number})
        expected = f"https://github.com/{self.repository}/discussions/{number}"
        try:
            discussion = data["repository"]["discussion"]
            if not isinstance(discussion["id"], str) or not discussion["id"].strip():
                raise ValueError("missing discussion ID")
            url = discussion["url"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(f"Discussion #{number} in {self.repository} is missing or unreadable") from exc
        if url != expected:
            raise AgentError(f"Discussion #{number} URL mismatch; expected {expected}; refusing to post")
        return discussion

    def create_discussion_comment(self, number, body):
        discussion = self.discussion(number)
        data = self.graphql(
            "mutation($discussionId:ID!,$body:String!){addDiscussionComment("
            "input:{discussionId:$discussionId,body:$body}){comment{url}}}",
            {"discussionId": discussion["id"], "body": body})
        try:
            url = data["addDiscussionComment"]["comment"]["url"]
            if not isinstance(url, str) or not url.strip():
                raise ValueError("missing comment URL")
            return url
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("POST", "graphql", "Unreadable discussion comment response") from exc

    def unminimized_comments(self, comments):
        """REST comments omit minimization state; read it in bounded GraphQL batches."""
        pending = []
        for offset in range(0, len(comments), 100):
            batch = comments[offset:offset + 100]
            ids = [comment.get("node_id") for comment in batch]
            if any(not isinstance(node, str) or not node for node in ids):
                raise GitHubError("POST", "graphql", "Comment has no node ID")
            data = self.graphql(
                "query($ids: [ID!]!) { nodes(ids: $ids) { "
                "... on IssueComment { id isMinimized } } }", {"ids": ids})
            try:
                nodes = data["nodes"]
                if not isinstance(nodes, list) or len(nodes) != len(batch):
                    raise ValueError("missing comment nodes")
                for comment, node in zip(batch, nodes):
                    if node["id"] != comment["node_id"] or type(node["isMinimized"]) is not bool:
                        raise ValueError("invalid comment minimization state")
                    if not node["isMinimized"]:
                        pending.append(comment)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("POST", "graphql", "Unreadable comment minimization state") from exc
        return pending

    def minimize_comment(self, comment):
        node = comment.get("node_id")
        if not isinstance(node, str) or not node:
            raise GitHubError("POST", "graphql", "Comment has no node ID")
        data = self.graphql(
            "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) "
            "{ minimizedComment { isMinimized } } }", {"id": node})
        if not data.get("minimizeComment", {}).get("minimizedComment", {}).get("isMinimized"):
            raise GitHubError("POST", "graphql", "Comment minimization was not confirmed")

    def candidate_evidence(self, number, sha):
        owner, name = self.repository.split("/")
        data = self.graphql(
            "query($owner: String!, $name: String!, $number: Int!, $sha: GitObjectID!) { "
            "repository(owner: $owner, name: $name) { "
            "pullRequest(number: $number) { headRefOid reviewDecision } "
            "object(oid: $sha) { ... on Commit { oid statusCheckRollup { state } } } } }",
            {"owner": owner, "name": name, "number": number, "sha": sha})
        try:
            repository = data["repository"]
            candidate, pr = repository["object"], repository["pullRequest"]
            if candidate["oid"] != sha:
                raise ValueError("evidence names another candidate")
            review = pr["reviewDecision"] or "no decision"
            if pr["headRefOid"] != sha:
                review = f"unavailable for this SHA (current head {pr['headRefOid'][:7]})"
            rollup = candidate["statusCheckRollup"]
            ci = rollup["state"] if rollup is not None else "no checks or statuses"
            if not isinstance(review, str) or not isinstance(ci, str):
                raise ValueError("invalid review or CI state")
            return review, ci
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("POST", "graphql", "Unreadable candidate evidence") from exc

    def default_branch(self):
        raw = self.request(self.prefix)
        if not isinstance(raw.get("default_branch"), str):
            raise GitHubError("GET", self.prefix, "GitHub returned no default branch")
        return raw["default_branch"]

    def prs_for_branch(self, branch, state="open"):
        owner = self.repository.split("/", 1)[0]
        query = urlencode({"state": state, "head": f"{owner}:{branch}", "per_page": 100})
        endpoint = f"{self.prefix}/pulls?{query}"
        return [parse_item(raw, "pr", endpoint) for raw in self.request(endpoint, paginate=True)]

    def add_labels(self, number, labels):
        self.request(f"{self.prefix}/issues/{number}/labels", "POST", {"labels": list(labels)}, array=True)

    def remove_label(self, number, label):
        self.request(f"{self.prefix}/issues/{number}/labels/{quote(label, safe='')}", "DELETE", array=True)
