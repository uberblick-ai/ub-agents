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
        for raw in ({"permission": "admin"}, {"role_name": "custom"}, {"role_name": None}, {"role_name": []}, [], None):
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

    def test_pr_content_pins_author_head_and_repository_across_history_pages(self):
        github = GitHub("org/project")
        first, second = self.page(True, "next"), self.page()
        for page in (first, second):
            item = page["data"]["repository"].pop("issue")
            item |= {"author": {"login": "outside"}, "headRefOid": "a" * 40,
                     "headRepository": {"nameWithOwner": "outside/project"}}
            page["data"]["repository"]["pullRequest"] = item
        with patch.object(github, "request", side_effect=[first, second]) as request:
            content = github.pr_content(42)
        self.assertEqual((content["head"], content["author"]), ("a" * 40, {"login": "outside"}))
        self.assertEqual(content["head_repository"], "outside/project")
        self.assertIn("pullRequest(number:", request.call_args.args[2]["query"])
        self.assertIn("headRepository { nameWithOwner }", request.call_args.args[2]["query"])
        for changed in ({"headRefOid": "b" * 40}, {"headRepository": {"nameWithOwner": "org/project"}}):
            altered = deepcopy(second)
            altered["data"]["repository"]["pullRequest"].update(changed)
            with patch.object(github, "request", side_effect=[first, altered]):
                with self.assertRaisesRegex(GitHubError, "Unreadable PR content history"):
                    github.pr_content(42)

    def test_pr_head_repository_may_be_deleted_but_malformed_data_fails(self):
        github = GitHub("org/project")
        page = self.page()
        item = page["data"]["repository"].pop("issue")
        item |= {"author": {"login": "outside"}, "headRefOid": "a" * 40, "headRepository": None}
        page["data"]["repository"]["pullRequest"] = item
        with patch.object(github, "request", return_value=page):
            self.assertIsNone(github.pr_content(42)["head_repository"])
        for repository in ({}, {"nameWithOwner": ""}, {"nameWithOwner": []}, "org/project"):
            item["headRepository"] = repository
            with self.subTest(repository=repository), patch.object(github, "request", return_value=page):
                with self.assertRaisesRegex(GitHubError, "Unreadable PR content history"):
                    github.pr_content(42)

    def review_page(self, more=False, cursor=None):
        return {"repository": {"pullRequest": {"reviews": {
            "nodes": [{"databaseId": 10, "body": "Review", "author": {"login": "outside"},
                       "submittedAt": "2026-01-01T00:00:10Z", "lastEditedAt": "2026-01-01T00:00:20Z",
                       "state": "COMMENTED", "commit": {"oid": "a" * 40}}],
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}

    def test_reviews_paginate_and_include_edit_times_and_reviewed_commits(self):
        github = GitHub("org/project")
        first, second = self.review_page(True, "next"), self.review_page()
        node = second["repository"]["pullRequest"]["reviews"]["nodes"][0]
        node.update(databaseId=11, lastEditedAt="2026-01-01T00:00:05Z", state="APPROVED")
        with patch.object(github, "graphql", side_effect=[first, second]) as request:
            reviews = github.reviews(42)
        self.assertEqual([r["id"] for r in reviews], [10, 11])
        self.assertEqual(reviews[0]["updated_at"], "2026-01-01T00:00:20Z")
        self.assertEqual(reviews[1]["updated_at"], reviews[1]["created_at"])
        self.assertEqual(reviews[1]["commit_id"], "a" * 40)
        self.assertEqual(request.call_args.args[1]["cursor"], "next")
        self.assertIn("lastEditedAt", request.call_args.args[0])
        self.assertIn("author { login __typename }", request.call_args.args[0])

    def test_review_errors_and_pending_reviews_cannot_supply_input(self):
        github = GitHub("org/project")
        pending = self.review_page()
        pending["repository"]["pullRequest"]["reviews"]["nodes"][0].update(state="PENDING", submittedAt=None)
        with patch.object(github, "graphql", return_value=pending):
            self.assertEqual(github.reviews(42), [])
        for pages in ([{"repository": {"pullRequest": None}}],
                      [self.review_page(True, None)],
                      [self.review_page(True, "same"), self.review_page(True, "same")]):
            with patch.object(github, "graphql", side_effect=pages):
                with self.assertRaisesRegex(GitHubError, "Unreadable PR reviews"):
                    github.reviews(42)

    def test_inline_review_comments_use_all_rest_pages(self):
        github = GitHub("org/project")
        with patch.object(github, "request", return_value=[]) as request:
            self.assertEqual(github.review_comments(42), [])
            request.assert_called_once_with("repos/org/project/pulls/42/comments?per_page=100", paginate=True)
