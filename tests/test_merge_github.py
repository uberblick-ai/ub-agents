import subprocess
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError
from ub_agents.github import Dependency, GitHub
from tests.support import RecordingRunner


class MergeGitHubTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub("org/project")
        self.sha = "a" * 40

    def test_checks_include_skipped_neutral_failures_pending_and_status_contexts(self):
        rows = [{"head_sha": self.sha, "name": name, "status": status, "conclusion": conclusion}
                for name, status, conclusion in (("success", "completed", "success"),
                                                  ("skipped", "completed", "skipped"),
                                                  ("neutral", "completed", "neutral"),
                                                  ("cancelled", "completed", "cancelled"),
                                                  ("failed", "completed", "failure"),
                                                  ("queued", "queued", None),
                                                  ("running", "in_progress", None))]
        statuses = [{"context": name, "state": state}
                    for name, state in (("external", "success"), ("waiting", "pending"), ("error", "error"))]
        with patch.object(self.github, "request", side_effect=[{"total_count": len(rows), "check_runs": rows},
                {"sha": self.sha, "total_count": len(statuses), "statuses": statuses}]):
            self.assertEqual(self.github.commit_checks(self.sha),
                             [("success", "pass"), ("skipped", "pass"), ("neutral", "pass"),
                              ("cancelled", "fail"), ("failed", "fail"), ("queued", "pending"),
                              ("running", "pending"), ("external", "pass"), ("waiting", "pending"), ("error", "fail")])

    def test_checks_read_every_page_and_do_not_accept_partial_or_other_head_data(self):
        run = {"head_sha": self.sha, "name": "build", "status": "completed", "conclusion": "success"}
        status = {"context": "ci", "state": "success"}
        responses = [{"total_count": 101, "check_runs": [run] * 100},
                     {"total_count": 101, "check_runs": [run | {"name": "last", "conclusion": "failure"}]},
                     {"sha": self.sha, "total_count": 101, "statuses": [status] * 100},
                     {"sha": self.sha, "total_count": 101, "statuses": [status | {"context": "last status", "state": "pending"}]}]
        with patch.object(self.github, "request", side_effect=responses) as request:
            checks = self.github.commit_checks(self.sha)
        self.assertEqual(len(checks), 202)
        self.assertIn(("last", "fail"), checks)
        self.assertIn(("last status", "pending"), checks)
        self.assertEqual([c.args[0] for c in request.call_args_list], [
            f"repos/org/project/commits/{self.sha}/check-runs?filter=latest&per_page=100&page=1",
            f"repos/org/project/commits/{self.sha}/check-runs?filter=latest&per_page=100&page=2",
            f"repos/org/project/commits/{self.sha}/status?per_page=100&page=1",
            f"repos/org/project/commits/{self.sha}/status?per_page=100&page=2"])
        for raw in ({"total_count": 1, "check_runs": []},
                    {"total_count": 1, "check_runs": [run | {"head_sha": "b" * 40}]},
                    {"total_count": 1, "check_runs": [run | {"status": "unknown"}]}, {}):
            with self.subTest(raw=raw), patch.object(self.github, "request", return_value=raw):
                with self.assertRaises(GitHubError):
                    self.github.commit_checks(self.sha)
        with patch.object(self.github, "request", side_effect=[{"total_count": 0, "check_runs": []},
                {"sha": "b" * 40, "total_count": 0, "statuses": []}]):
            with self.assertRaises(GitHubError):
                self.github.commit_checks(self.sha)

    def test_merge_uses_match_head_and_errors_are_not_retried(self):
        for method in ("squash", "merge", "rebase"):
            with self.subTest(method=method):
                runner = RecordingRunner("/synthetic")
                self.github.runner = runner
                command = ("gh", "pr", "merge", "23", "--repo", "org/project",
                           f"--{method}", "--match-head-commit", self.sha)
                runner.responses[command] = "merged"
                self.github.merge_pr(23, method, self.sha)
                runner.responses[command] = subprocess.CompletedProcess(command, 1, "", "denied")
                with self.assertRaisesRegex(GitHubError, "denied"):
                    self.github.merge_pr(23, method, self.sha)
                self.assertEqual(len(runner.calls), 2)

    def test_commit_parents_and_base_ancestry_are_bound_to_exact_commit(self):
        with patch.object(self.github, "request", return_value={"sha": self.sha, "parents": [{"sha": "b" * 40}]}):
            self.assertEqual(self.github.commit_parents(self.sha), ["b" * 40])
        with patch.object(self.github, "request", return_value={"sha": "b" * 40, "parents": []}):
            with self.assertRaises(GitHubError):
                self.github.commit_parents(self.sha)
        for status, ancestor, expected in (("ahead", self.sha, True), ("identical", self.sha, True),
                                           ("diverged", self.sha, False), ("behind", self.sha, False),
                                           ("ahead", "b" * 40, False)):
            with self.subTest(status=status, ancestor=ancestor), patch.object(self.github, "request", return_value={
                    "status": status, "merge_base_commit": {"sha": ancestor}}) as request:
                self.assertEqual(self.github.commit_on_base(self.sha, "release/next"), expected)
                request.assert_called_once_with(f"repos/org/project/compare/{self.sha}...release%2Fnext")

    def test_closing_issues_are_paginated_and_close_uses_own_repository(self):
        def page(number, repository, more, cursor):
            return {"repository": {"pullRequest": {"closingIssuesReferences": {
                "nodes": [{"number": number, "state": "OPEN", "repository": {"nameWithOwner": repository}}],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}
        with patch.object(self.github, "graphql", side_effect=[page(1, "org/project", True, "next"),
                                                               page(2, "org/other", False, None)]) as graphql:
            self.assertEqual(self.github.closing_issue_references(23),
                             [Dependency("org/project", 1, "open"), Dependency("org/other", 2, "open")])
            self.assertEqual(graphql.call_args_list[1].args[1]["cursor"], "next")
        with patch.object(self.github, "request") as request:
            self.github.close_issue(2, "org/other")
            request.assert_called_once_with("repos/org/other/issues/2", "PATCH", {"state": "closed"})
