from copy import deepcopy
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError
from ub_agents.github import GitHub


class ApprovalGitHubTests(unittest.TestCase):
    def test_roles_use_role_name_only_and_unreadable_is_outside(self):
        github = GitHub("org/project")
        for role in ("maintain", "admin", "write", "triage", "read", "none"):
            with self.subTest(role=role), patch.object(github, "request", return_value={"role_name": role}) as request:
                self.assertEqual(github.role("some-bot[bot]"), role)
                self.assertEqual(request.call_args.args, ("repos/org/project/collaborators/some-bot%5Bbot%5D/permission",))
        for raw in ({"permission": "admin"}, {"role_name": "custom"}, {"role_name": None}, {"role_name": []}):
            with patch.object(github, "request", return_value=raw):
                self.assertIsNone(github.role("operator"))
        with patch.object(github, "request", side_effect=GitHubError("GET", "permission", "Forbidden")):
            self.assertIsNone(github.role("operator"))
        with patch.object(github, "request") as request:
            self.assertIsNone(github.role(None))
            request.assert_not_called()

    def page(self, more=False, cursor=None):
        return {"data": {"repository": {"issue": {
            "title": "Title", "body": "Edited body", "createdAt": "2026-01-01T00:00:00Z",
            "lastEditedAt": "2026-01-01T00:00:10Z", "userContentEdits": {
                "nodes": [{"editedAt": "2026-01-01T00:00:10Z", "editor": {"login": "operator"},
                           "diff": "Edited body", "deletedAt": None}],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}}

    def test_content_history_paginates_all_edits_and_attributes_each(self):
        github = GitHub("org/project")
        first, second = self.page(True, "next"), self.page()
        second["data"]["repository"]["issue"]["userContentEdits"]["nodes"][0].update(
            editedAt="2026-01-01T00:00:00Z", editor={"login": "outsider"}, diff="Original body")
        with patch.object(github, "request", side_effect=[first, second]) as request:
            content = github.issue_content(42)
        self.assertEqual(len(content["edits"]), 2)
        self.assertEqual(content["edits"][1]["diff"], "Original body")
        calls = request.call_args_list
        self.assertEqual(calls[0].args[:2], ("graphql", "POST"))
        self.assertEqual(calls[0].args[2]["variables"], {"owner": "org", "name": "project", "number": 42, "cursor": None})
        self.assertEqual(calls[1].args[2]["variables"]["cursor"], "next")
        self.assertIn("editor { login }", calls[0].args[2]["query"])
        self.assertIn("lastEditedAt", calls[0].args[2]["query"])

    def test_errors_null_issue_bad_cursor_and_changes_between_pages_fail(self):
        github = GitHub("org/project")
        changed = self.page()
        changed["data"]["repository"]["issue"]["body"] = "Concurrent edit"
        for pages in ([{"errors": [{"message": "Forbidden"}], "data": None}],
                      [{"data": {"repository": {"issue": None}}}],
                      [self.page(True, None)], [self.page(True, "next"), changed],
                      [self.page(True, "next"), self.page(True, "next")]):
            with self.subTest(pages=pages), patch.object(github, "request", side_effect=deepcopy(pages)):
                with self.assertRaisesRegex(GitHubError, "Unreadable issue content history"):
                    github.issue_content(42)

    def test_timeline_uses_all_pages(self):
        github = GitHub("org/project")
        with patch.object(github, "request", return_value=[]) as request:
            self.assertEqual(github.timeline(42), [])
            request.assert_called_once_with("repos/org/project/issues/42/timeline", paginate=True)
