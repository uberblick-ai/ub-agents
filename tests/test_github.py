from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ub_agents.errors import AgentError
from ub_agents.github import GitHub, parse_item
from ub_agents.loop import Loop
from ub_agents.records import body, iso, seconds, timestamp
from tests.support import agent, config, issue


class GitHubTests(unittest.TestCase):
    def test_pr_draft_state_is_required_and_preserved(self):
        raw = {"number": 2, "title": "Candidate", "body": "Closes #1", "labels": [],
               "state": "open", "user": {"login": "operator"},
               "head": {"sha": "a" * 40, "ref": "feature/test"}}
        for draft in (True, False):
            self.assertEqual(parse_item(raw | {"draft": draft}, "pr").draft, draft)
        for fields in ({}, {"draft": None}, {"draft": "false"}, {"draft": 0}):
            with self.subTest(fields=fields), self.assertRaises(AgentError):
                parse_item(raw | fields, "pr")
        self.assertFalse(parse_item(raw, "issue").draft)

    def test_reads_all_pages_without_indexed_search(self):
        pages = [[{"id": i} for i in range(100)], [{"id": 100}]]
        results = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with patch("ub_agents.github.subprocess.run", side_effect=results) as run:
            self.assertEqual(len(GitHub("org/project").comments(1)), 101)
        self.assertEqual(run.call_count, 2)
        for page, call in enumerate(run.call_args_list, 1):
            argv = call.args[0]
            self.assertIn(f"page={page}", argv[-1])
            self.assertEqual(call.kwargs["timeout"], 20)
            self.assertNotIn("--paginate", argv)
            self.assertNotIn("search", argv)

    def test_repository_scan_is_incremental_with_overlapping_cursor_and_edit_replacement(self):
        github = GitHub("org/project")
        initial = [{"id": 1, "body": "claiming", "updated_at": iso(100)},
                   {"id": 2, "body": "old history", "updated_at": iso(200)}]
        changed = [{"id": 1, "body": "released", "updated_at": iso(1000)},
                   {"id": 3, "body": "new outcome", "updated_at": iso(1001)}]
        with patch("ub_agents.github.timestamp", side_effect=[1000, 1100, 1200]), \
                patch.object(github, "request", side_effect=[initial, changed, AgentError("network failed")]) as request:
            self.assertEqual(github.repository_comments(), initial)
            self.assertEqual(github.repository_comments(), [changed[0], initial[1], changed[1]])
            cursor = github._comment_since
            with self.assertRaises(AgentError):
                github.repository_comments()
            self.assertEqual(github._comment_since, cursor)
        first = parse_qs(urlsplit(request.call_args_list[0].args[0]).query)
        second = parse_qs(urlsplit(request.call_args_list[1].args[0]).query)
        self.assertNotIn("since", first)
        self.assertEqual(second["since"], ["1970-01-01T00:15:40Z"])
        self.assertEqual(second["sort"], ["updated"])
        restarted = GitHub("org/project")
        with patch.object(restarted, "request", return_value=[]) as request:
            restarted.repository_comments()
        self.assertNotIn("since=", request.call_args.args[0])

    def test_failed_page_does_not_advance_incremental_discovery(self):
        github = GitHub("org/project")
        comments = [{"id": i, "updated_at": iso(i + 1000)} for i in range(100)]
        first_page = subprocess.CompletedProcess([], 0, json.dumps(comments), "")
        with patch("ub_agents.github.subprocess.run", side_effect=[first_page, subprocess.TimeoutExpired("gh", 20)]):
            with self.assertRaises(AgentError):
                github.repository_comments()
        self.assertIsNone(github._comment_since)
        self.assertEqual(github._comment_cache, {})

    def test_edit_or_deletion_during_scan_cannot_hide_closed_item_failure(self):
        for mutation in ("edit", "delete"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                now = int(timestamp())
                comments = [{"id": i, "body": "Bot update", "user": {"login": "ci-bot"},
                             "updated_at": iso(now - 86400 + i)} for i in range(1, 151)]
                failure = {"kind": "lease", "run": "failed", "agent": "worker",
                           "actor": "operator", "runtime": "direct", "provider": "direct",
                           "assignment": 42, "assignment_sha": None,
                           "created": iso(now - 86400), "expires": iso(now - 86340),
                           "state": "released", "result": "retry", "summary": "Execution timed out",
                           "attempt": 1, "started": True}
                comments[100].update(body=body(failure), user={"login": "operator"},
                                     issue_url="https://api.github.com/repos/org/project/issues/42")
                reads = 0

                class MovingGitHub(GitHub):
                    def request(self, endpoint, method="GET", data=None, paginate=False, array=False):
                        nonlocal reads
                        if paginate:
                            return super().request(endpoint, method, data, paginate=True)
                        query = parse_qs(urlsplit(endpoint).query)
                        if urlsplit(endpoint).path.endswith("/issues/comments"):
                            reads += 1
                            if reads == 2:
                                if mutation == "edit":
                                    comments[4]["updated_at"] = iso(now)
                                else:
                                    comments.pop(4)
                            rows = sorted(comments, key=lambda c: seconds(c["updated_at"]))
                            if "since" in query:
                                rows = [c for c in rows if seconds(c["updated_at"]) > seconds(query["since"][0])]
                        else:
                            rows = [c for c in comments if c["id"] == 101]
                        offset = (int(query.get("page", [1])[0]) - 1) * 100
                        return deepcopy(rows[offset:offset + 100])

                github = MovingGitHub("org/project")
                closed = replace(issue(42, labels=()), state="closed")
                root = Path(directory)
                loop = Loop(config(root, agent(root)), github, "operator")
                with patch.object(github, "observe", return_value=[]), patch.object(github, "item", return_value=closed):
                    for _ in range(2):  # Both initial discovery and subsequent polls retain it.
                        plans = loop.plans()
                        self.assertEqual([(p.item.number, p.state) for p in plans], [(42, "blocked")])
                        self.assertIn("timed out", plans[0].reason)
                self.assertIn(101, github._comment_cache)

    def test_full_timestamp_bucket_fails_visibly_without_committing_cursor_or_cache(self):
        for total in (99, 100, 101):
            with self.subTest(comments_in_same_second=total):
                github = GitHub("org/project")
                github._comment_since = iso(1000)
                github._comment_cache = {1: {"id": 1, "updated_at": iso(900)}}
                batch = [{"id": i + 2, "updated_at": iso(2000)} for i in range(min(total, 100))]
                with patch.object(github, "request", return_value=batch) as request:
                    if total < 100:
                        self.assertEqual(len(github.repository_comments()), 100)
                    else:
                        with self.assertRaisesRegex(AgentError, "one update second"):
                            github.repository_comments()
                        self.assertEqual(github._comment_since, iso(1000))
                        self.assertEqual(github._comment_cache, {1: {"id": 1, "updated_at": iso(900)}})
                        self.assertEqual(request.call_count, 2)

    def test_bad_response_and_failed_auth_are_failures(self):
        for result in [subprocess.CompletedProcess([], 0, "not json", ""),
                       subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 1, "", "not authenticated")]:
            with patch("ub_agents.github.subprocess.run", return_value=result), self.assertRaises(AgentError):
                GitHub("org/project").comments(1)
