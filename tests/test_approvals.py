from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest.mock import patch

from ub_agents.approvals import (MARKER, approval_body, approve_issue, body_sha,
                                check_issue, content_sha, parse_approval)
from ub_agents.cli import main
from ub_agents.errors import AgentError
from tests.support import FakeGitHub, agent, config, issue


def at(second):
    return f"2026-01-01T00:00:{second:02d}Z"


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub(issue())
        self.github.roles.update(maintainer="maintain", admin="admin", teammate="write",
                                 outsider="read", triager="triage")
        self.github.timelines[1] = []
        self.github.content_histories[1] = {"edits": [{"editedAt": at(0), "editor": {"login": "outsider"},
                                                    "diff": "Acceptance criteria", "deletedAt": None}]}
        self.comments = self.github.store.setdefault(1, [])

    def label(self, login="maintainer", label="needs-preparation", second=5):
        self.github.timelines[1].append({"event": "labeled", "actor": {"login": login},
                                         "label": {"name": label}, "created_at": at(second)})

    def comment(self, login="outsider", text="More requirements", second=8):
        comment = {"id": len(self.comments) + 1, "user": {"login": login}, "body": text,
                   "created_at": at(second), "updated_at": at(second)}
        self.comments.append(comment)
        return comment

    def body_edit(self, login="outsider", second=10, text="Edited requirements"):
        self.github.change(1, body=text)
        self.github.content_histories[1]["lastEditedAt"] = at(second)
        self.github.content_histories[1]["edits"].insert(0, {
            "editedAt": at(second), "editor": {"login": login} if login else None,
            "diff": text, "deletedAt": None})

    def rename(self, login="outsider", second=10, title="New requirements"):
        old = self.github.item(1).title
        self.github.change(1, title=title)
        self.github.timelines[1].append({"event": "renamed", "actor": {"login": login} if login else None,
                                         "rename": {"from": old, "to": title}, "created_at": at(second)})

    def approval(self, login="maintainer", second=15, comments=(), title=None, body=None):
        item = self.github.item(1)
        return self.comment(login, approval_body(1, title if title is not None else item.title,
                                                body if body is not None else item.body, comments), second)

    def check(self):
        return check_issue(self.github, 1, {"needs-preparation", "ready"})

    def test_maintainer_start_survives_agent_label_transitions_and_unlabeling(self):
        for role in ("maintainer", "admin"):
            with self.subTest(role=role):
                self.github.timelines[1] = []
                self.label(role)
                self.label("operator", "ready", 7)
                self.github.timelines[1].append({"event": "unlabeled"})
                self.github.change(1, labels=frozenset({"ready"}))
                self.assertTrue(self.check().allowed)
                self.assertEqual(self.github.writes, [])

    def test_non_maintainer_labels_and_non_issue_triggers_do_not_start(self):
        for login in ("operator", "teammate", "triager", "outsider", "unknown"):
            with self.subTest(login=login):
                self.github.timelines[1] = []
                self.label(login)
                self.label("maintainer", "needs-review")
                result = self.check()
                self.assertFalse(result.allowed)
                self.assertIn("No maintainer", result.reason)

    def test_title_and_body_edits_by_role(self):
        for edit in ("rename", "body_edit"):
            for login, allowed in (("outsider", False), ("triager", False), (None, False),
                                   ("unknown", False), ("teammate", True), ("operator", True),
                                   ("maintainer", True), ("admin", True)):
                with self.subTest(edit=edit, login=login):
                    self.setUp()
                    self.label()
                    getattr(self, edit)(login)
                    self.assertEqual(self.check().allowed, allowed)

    def test_later_maintainer_trigger_approves_outside_edits_again(self):
        self.label()
        self.body_edit()
        self.rename(second=11)
        self.assertFalse(self.check().allowed)
        self.label("admin", "ready", 15)
        self.assertTrue(self.check().allowed)

    def test_record_restores_approval_and_survives_later_trusted_title_and_body_edits(self):
        self.label()
        self.body_edit()
        self.rename(second=11)
        self.approval()
        self.assertTrue(self.check().allowed)
        self.body_edit("operator", 20, "Prepared requirements")
        self.rename("teammate", 21, "Prepared title")
        self.assertTrue(self.check().allowed)
        self.body_edit("outsider", 25, "Outside changes")
        self.assertFalse(self.check().allowed)
        self.approval(second=30)
        self.assertTrue(self.check().allowed)

    def test_stale_sha_never_becomes_valid_after_a_trusted_revert(self):
        self.label()
        self.body_edit()
        self.approval(body="Acceptance criteria")
        self.body_edit("operator", 20, "Acceptance criteria")
        self.assertFalse(self.check().allowed)

    def test_approval_record_alone_cannot_start_issue(self):
        self.approval()
        self.assertFalse(self.check().allowed)

    def test_forged_malformed_wrong_issue_and_edited_approvals_are_ignored(self):
        for variant in ("operator", "outsider", "malformed", "wrong-issue", "edited"):
            with self.subTest(variant=variant):
                self.setUp()
                self.label()
                self.body_edit()
                c = self.approval(login=variant if variant in {"operator", "outsider"} else "maintainer")
                if variant == "malformed":
                    c["body"] = MARKER + "\nInvalid record"
                elif variant == "wrong-issue":
                    c["body"] = approval_body(2, self.github.item(1).title, self.github.item(1).body, [])
                elif variant == "edited":
                    c["updated_at"] = at(16)
                self.assertFalse(self.check().allowed)

    def test_outside_comments_clear_only_when_listed_and_unchanged(self):
        self.label()
        c = self.comment()
        other = self.comment(text="Not cleared", second=9)
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.approval(comments=[c])
        self.assertEqual(self.check().cleared_comment_ids, {c["id"]})
        self.assertNotIn(other["id"], self.check().cleared_comment_ids)
        c.update(body="Changed input", updated_at=at(20))
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        c.update(body="More requirements", updated_at=at(21))
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.approval(second=25, comments=[c, other])
        self.assertEqual(self.check().cleared_comment_ids, {c["id"], other["id"]})

    def test_start_clears_existing_comments_but_later_edit_removes_clearance(self):
        c = self.comment(second=3)
        self.label()
        self.assertEqual(self.check().cleared_comment_ids, {c["id"]})
        c["updated_at"] = at(6)
        self.assertEqual(self.check().cleared_comment_ids, frozenset())

    def test_stale_or_forged_record_cannot_clear_outside_comments(self):
        self.label()
        c = self.comment()
        self.approval(comments=[c], body="Stale body")
        self.approval(login="operator", comments=[c])
        self.assertEqual(self.check().cleared_comment_ids, frozenset())

    def test_missing_history_blocks_and_hidden_revision_cannot_validate_record(self):
        self.label()
        self.body_edit()
        self.approval()
        self.body_edit("operator", 20)
        self.github.content_histories[1]["edits"][1].update(diff=None, deletedAt=at(21))
        self.assertFalse(self.check().allowed)
        self.github.content_histories[1]["edits"] = []
        self.assertIn("unreadable", self.check().reason)
        with patch.object(self.github, "timeline", side_effect=AgentError("Unavailable")):
            self.assertFalse(self.check().allowed)

    def test_same_second_outside_edit_is_not_silently_approved(self):
        self.label(second=10)
        self.body_edit(second=10)
        self.assertFalse(self.check().allowed)
        self.approval(second=10)
        self.assertFalse(self.check().allowed)

    def test_same_second_body_revisions_cannot_validate_an_approval_record(self):
        for reverse in (False, True):
            for body in ("Reviewed requirements", "Unseen requirements"):
                with self.subTest(reverse=reverse, body=body):
                    self.setUp()
                    self.label()
                    comment = self.comment()
                    self.body_edit(second=10, text="Reviewed requirements")
                    self.body_edit(second=10, text="Unseen requirements")
                    if reverse:
                        self.github.content_histories[1]["edits"].reverse()
                    self.assertFalse(self.check().allowed)
                    self.approval(second=15, body=body, comments=[comment])
                    result = self.check()
                    self.assertFalse(result.allowed)
                    self.assertEqual(result.cleared_comment_ids, frozenset())
                    self.body_edit("operator", 20, "Prepared requirements")
                    self.assertFalse(self.check().allowed)
                    # A later unambiguous revision can be approved normally.
                    self.approval(second=25, comments=[comment])
                    result = self.check()
                    self.assertTrue(result.allowed)
                    self.assertEqual(result.cleared_comment_ids, {comment["id"]})

    def test_unicode_encoding_and_record_format(self):
        title, body = "Café ☕", "Line 1\r\nLine 2\n"
        self.assertEqual(content_sha(title, body), body_sha('["Café ☕","Line 1\\r\\nLine 2\\n"]'))
        record = parse_approval(approval_body(1, title, body, []), 1)
        self.assertEqual(record, {"issue": 1, "content_sha256": content_sha(title, body), "comments": []})
        for bad in ("[]", "null", '{"issue":true}', '{"issue":1,"content_sha256":"bad","comments":[]}'):
            self.assertIsNone(parse_approval(f"{MARKER}\n\n```json\n{bad}\n```\n", 1))

    def test_approve_refuses_all_non_maintainers_without_posting(self):
        for login in ("operator", "outsider", "triager", "unknown"):
            with self.subTest(login=login), self.assertRaisesRegex(AgentError, "maintainer"):
                approve_issue(self.github, 1, login)
        self.assertEqual(self.github.writes, [])

    def test_approve_prints_input_and_posts_one_record(self):
        self.github.login = "maintainer"
        c = self.comment()
        self.comment("operator", "Agent note", 9)
        with redirect_stdout(io.StringIO()) as output:
            created = approve_issue(self.github, 1, "maintainer")
        text = output.getvalue()
        self.assertIn("Requirements", text)
        self.assertIn("Acceptance criteria", text)
        self.assertIn("More requirements", text)
        self.assertNotIn("Agent note", text)
        record = parse_approval(created["body"], 1)
        self.assertEqual(record["comments"], [{"id": c["id"], "body_sha256": body_sha(c["body"])}])
        self.assertEqual(len(self.github.writes), 1)

    def test_approve_refuses_concurrent_edit_without_posting(self):
        original = self.github.item
        reads = 0

        def item(number):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.github.change(1, body="Raced")
            return original(number)

        with patch.object(self.github, "item", side_effect=item), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(AgentError, "changed"):
                approve_issue(self.github, 1, "maintainer")
        self.assertEqual(self.github.writes, [])

    def test_cli_command_posts_or_refuses_with_the_authenticated_account(self):
        cfg = config(".", agent("."))
        for login, expected in (("operator", 1), ("admin", 0)):
            self.github.login = login
            with patch("ub_agents.cli.load_config", return_value=cfg), \
                    patch("ub_agents.cli.GitHub", return_value=self.github), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(["approve", "1"]), expected)
            self.assertIn("maintainer" if expected else "Approval posted", error.getvalue() if expected else output.getvalue())
