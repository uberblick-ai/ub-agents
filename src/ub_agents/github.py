"""Authenticated, unindexed GitHub reads through gh. Errors never mean no work."""

from dataclasses import dataclass
import json
import subprocess

from .errors import AgentError


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


def parse_item(data, kind):
    try:
        if (type(data["number"]) is not int or data["number"] < 1
                or not isinstance(data["title"], str)
                or not isinstance(data.get("body") or "", str)
                or data["state"] not in {"open", "closed"}
                or not isinstance(data["user"]["login"], str)
                or not isinstance(data["labels"], list)):
            raise ValueError("invalid work item fields")
        return Item(data["number"], kind, data["title"], data.get("body") or "",
                    frozenset(x["name"] for x in data["labels"]), data["state"],
                    data["user"]["login"], data["head"]["sha"] if kind == "pr" else None,
                    data["head"]["ref"] if kind == "pr" else None)
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("Unreadable GitHub work item") from exc


class GitHub:
    def __init__(self, repository):
        self.repository = repository
        self.prefix = f"repos/{repository}"

    def request(self, endpoint, method="GET", data=None, paginate=False):
        command = ["gh", "api", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", endpoint]
        if paginate:
            command += ["--paginate", "--slurp"]
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
            if paginate:
                if not isinstance(value, list) or any(not isinstance(p, list) for p in value):
                    raise ValueError("expected arrays of pages")
                return [item for page in value for item in page]
            if not isinstance(value, dict):
                raise ValueError("expected an object")
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

    def repository_records(self):
        from .records import records
        # Recovery must not depend on a trigger still being present or an item
        # still being open. No indexed search, time window, or result cap.
        comments = self.request(f"{self.prefix}/issues/comments?per_page=100", paginate=True)
        return records(comments)

    def create_comment(self, number, body):
        return self.request(f"{self.prefix}/issues/{number}/comments", "POST", {"body": body})

    def update_comment(self, comment_id, body):
        return self.request(f"{self.prefix}/issues/comments/{comment_id}", "PATCH", {"body": body})

    def default_branch(self):
        raw = self.request(self.prefix)
        if not isinstance(raw.get("default_branch"), str):
            raise AgentError("GitHub returned no default branch")
        return raw["default_branch"]
