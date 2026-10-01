import json
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ub_agents.errors import AgentError
from ub_agents.github import GitHub


class GitHubTests(unittest.TestCase):
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
        initial = [{"id": 1, "body": "claiming"}, {"id": 2, "body": "old history"}]
        changed = [{"id": 1, "body": "released"}, {"id": 3, "body": "new outcome"}]
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
        first_page = subprocess.CompletedProcess([], 0, json.dumps([{"id": i} for i in range(100)]), "")
        with patch("ub_agents.github.subprocess.run", side_effect=[first_page, subprocess.TimeoutExpired("gh", 20)]):
            with self.assertRaises(AgentError):
                github.repository_comments()
        self.assertIsNone(github._comment_since)
        self.assertEqual(github._comment_cache, {})

    def test_bad_response_and_failed_auth_are_failures(self):
        for result in [subprocess.CompletedProcess([], 0, "not json", ""),
                       subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 1, "", "not authenticated")]:
            with patch("ub_agents.github.subprocess.run", return_value=result), self.assertRaises(AgentError):
                GitHub("org/project").comments(1)
