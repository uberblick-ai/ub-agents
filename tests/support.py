from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading

from ub_agents.config import Agent, Config
from ub_agents.github import Item
from ub_agents.records import MARKER, records


def agent(root, **overrides):
    defaults = dict(name="worker", triggers=("ready", "needs-changes"), instructions=None,
                    runtimes=(), command=(sys.executable, "-c", "pass"), runtime_args=(),
                    different_from=None, kind="either", cwd=Path(root), worktree=False,
                    lease_seconds=60, renewal_seconds=10, timeout_seconds=10,
                    max_attempts=3, backoff_seconds=0, max_backoff_seconds=0)
    return Agent(**(defaults | overrides))


def config(root, *agents):
    return Config(Path(root), "org/project", agents or (agent(root),), 1, ("needs-human",))


def issue(number=1, labels=("ready",)):
    return Item(number, "issue", "Requirements", "Acceptance criteria", frozenset(labels), "open", "operator")


def pr(number=2, labels=("needs-changes",), head="a" * 40, body="Closes #1"):
    return Item(number, "pr", "Candidate", body, frozenset(labels), "open", "operator", head, "feature/test")


class FakeGitHub:
    """Shared durable store, intentionally without compare-and-swap semantics."""
    repository = "org/project"

    def __init__(self, *items):
        self.items = {item.number: item for item in items}
        self.store = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.claim_barrier = None
        self.claim_read_barrier = None
        self.unreadable = False
        self.writes = []
        self.login = "operator"

    def actor(self):
        return self.login

    def default_branch(self):
        return "main"

    def prs_for_branch(self, branch):
        return [item for item in self.items.values()
                if item.kind == "pr" and item.branch == branch and item.state == "open"]

    def observe(self):
        return [item for item in sorted(self.items.values(), key=lambda i: i.number) if item.state == "open"]

    def item(self, number, kind=None):
        return self.items[number]

    def change(self, number, **changes):
        self.items[number] = replace(self.items[number], **changes)

    def add_labels(self, number, labels):
        self.writes.append(("add-labels", number, tuple(labels)))
        self.change(number, labels=self.items[number].labels.union(labels))

    def remove_label(self, number, label):
        self.writes.append(("remove-label", number, label))
        self.change(number, labels=self.items[number].labels.difference({label}))

    def comments(self, number):
        if self.unreadable:
            from ub_agents.errors import AgentError
            raise AgentError("GitHub unavailable")
        with self.lock:
            snapshot = deepcopy(self.store.get(number, []))
        if self.claim_read_barrier and not snapshot:
            self.claim_read_barrier.wait(timeout=5)
        return snapshot

    def repository_comments(self):
        with self.lock:
            return deepcopy([comment for comments in self.store.values() for comment in comments])

    def create_comment(self, number, body):
        data = json.loads(body.rsplit("\n```json\n", 1)[1].removesuffix("\n```\n")) if body.startswith(MARKER) else {}
        with self.lock:
            comment = {"id": self.next_id, "body": body, "user": {"login": self.login},
                       "issue_url": f"https://api.github.com/repos/org/project/issues/{number}",
                       "html_url": f"https://github.com/org/project/issues/{number}#issuecomment-{self.next_id}"}
            self.next_id += 1
            self.store.setdefault(number, []).append(comment)
            self.writes.append(("create", comment["id"]))
        if self.claim_barrier and data.get("kind") == "lease":
            self.claim_barrier.wait(timeout=5)
        return deepcopy(comment)

    def update_comment(self, comment_id, body):
        with self.lock:
            for comments in self.store.values():
                for comment in comments:
                    if comment["id"] == comment_id:
                        comment["body"] = body
                        self.writes.append(("update", comment_id))
                        return deepcopy(comment)
        raise AssertionError(f"Unknown comment {comment_id}")


class RecordingRunner:
    """Read-only doctor probes: explicit responses, no real tool execution."""
    def __init__(self, root):
        import os
        self.root = Path(root)
        self.calls = []
        self.responses = {
            ("git", "--version"): "git version 2.49.0\n",
            ("gh", "--version"): "gh version 2.80.0\n",
            ("git", "-C", str(root), "rev-parse", "--show-toplevel"): str(root),
            ("git", "-C", str(root), "remote", "get-url", "origin"): "git@github.com:org/project.git",
            ("git", "-C", str(root), "check-ignore", "-q", ".ub-agent/"): "",
            ("git", "-C", str(root), "worktree", "list", "--porcelain"): "worktree fixture\n",
            ("ps", "-axo", "pid=,pgid=,stat="): f"{os.getpid()} {os.getpgrp()} S\n",
            ("codex", "--version"): "codex-cli 0.120.0\n",
            ("claude", "--version"): "2.1.268 (Claude Code)\n",
            ("codex", "login", "status"): "sk-auth-secret\n",
            ("claude", "auth", "status"): "sk-auth-secret\n",
        }

    def __call__(self, command, **kwargs):
        import subprocess
        self.calls.append((tuple(command), kwargs))
        response = self.responses[tuple(command)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(command, 0, response, "ghp-private-stderr")


class DoctorGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.auth_error = None
        self.repository_error = None
        self.metadata = {"full_name": "org/project", "permissions": {"triage": True}}

    def actor(self):
        self.reads.append("user")
        if self.auth_error:
            raise self.auth_error
        return self.login

    def request(self, endpoint):
        self.reads.append(endpoint)
        if self.repository_error:
            raise self.repository_error
        return self.metadata
