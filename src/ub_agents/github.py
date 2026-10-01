"""Authenticated, unindexed GitHub reads through gh. Errors never mean no work."""

from dataclasses import dataclass
import json
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import AgentError
from .records import iso, seconds, timestamp


@dataclass(frozen=True)
class Item:
    number: int
    kind: str
    title: str
    body: str
    labels: frozenset[str]
    state: str
    author: str
    head: str | None = None
    branch: str | None = None
    draft: bool = False


def parse_item(data, kind):
    try:
        if (type(data["number"]) is not int or data["number"] < 1
                or not isinstance(data["title"], str)
                or not isinstance(data.get("body") or "", str)
                or data["state"] not in {"open", "closed"}
                or not isinstance(data["user"]["login"], str)
                or not isinstance(data["labels"], list)):
            raise ValueError("invalid work item fields")
        if kind == "pr" and type(data["draft"]) is not bool:
            raise ValueError("invalid PR draft field")
        return Item(data["number"], kind, data["title"], data.get("body") or "",
                    frozenset(x["name"] for x in data["labels"]), data["state"],
                    data["user"]["login"], data["head"]["sha"] if kind == "pr" else None,
                    data["head"]["ref"] if kind == "pr" else None,
                    data["draft"] if kind == "pr" else False)
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("Unreadable GitHub work item") from exc


class GitHub:
    def __init__(self, repository):
        self.repository = repository
        self.prefix = f"repos/{repository}"
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
                   "-H", "Accept: application/vnd.github+json", endpoint]
        if data is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                    capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError(f"GitHub {method} {endpoint} failed: {exc}") from exc
        if result.returncode:
            raise AgentError(f"GitHub {method} {endpoint} failed: {result.stderr.strip()}")
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, list if array else dict):
                raise ValueError("expected an array" if array else "expected an object")
            return value
        except (ValueError, TypeError) as exc:
            raise AgentError(f"Unreadable GitHub response for {endpoint}: {exc}") from exc

    def actor(self):
        data = self.request("user")
        if not isinstance(data.get("login"), str) or not data["login"]:
            raise AgentError("GitHub authentication returned no actor")
        return data["login"]

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

    def prs_for_branch(self, branch):
        owner = self.repository.split("/", 1)[0]
        query = urlencode({"state": "open", "head": f"{owner}:{branch}", "per_page": 100})
        return [parse_item(raw, "pr") for raw in
                self.request(f"{self.prefix}/pulls?{query}", paginate=True)]
