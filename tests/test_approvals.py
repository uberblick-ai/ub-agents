import unittest
from unittest.mock import patch

from ub_agents.approvals import check_issue
from ub_agents.errors import AgentError
from tests.support import FakeGitHub, issue, legacy_approval_body


def at(second):
    return f"2026-01-01T00:00:{second:02d}Z"


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub(issue())
        self.github.roles.update(maintainer="maintain", admin="admin", teammate="write",
                                 outsider="read", triager="triage")
        self.github.timelines[1] = []
        self.github.content_histories[1] = {"author": {"login": "outsider"},
                                           "edits": [{"editedAt": at(0), "editor": {"login": "outsider"},
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

    def legacy_record(self, login="maintainer", second=15, comments=(), title=None, body=None):
        item = self.github.item(1)
        return self.comment(login, legacy_approval_body(1, title if title is not None else item.title,
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

    def test_record_cannot_clear_outside_edits_even_after_trusted_rewrites(self):
        self.label()
        self.body_edit()
        self.rename(second=11)
        self.legacy_record()
        self.assertFalse(self.check().allowed)
        self.body_edit("operator", 20, "Prepared requirements")
        self.rename("teammate", 21, "Prepared title")
        self.assertFalse(self.check().allowed)
        self.body_edit("outsider", 25, "Outside changes")
        self.assertFalse(self.check().allowed)
        self.legacy_record(second=30)
        self.assertFalse(self.check().allowed)
        self.label(second=35)
        self.assertTrue(self.check().allowed)

    def test_stale_sha_never_becomes_valid_after_a_trusted_revert(self):
        self.label()
        self.body_edit()
        self.legacy_record(body="Acceptance criteria")
        self.body_edit("operator", 20, "Acceptance criteria")
        self.assertFalse(self.check().allowed)

    def test_approval_record_alone_cannot_start_issue(self):
        self.legacy_record()
        self.assertFalse(self.check().allowed)

    def test_forged_malformed_wrong_issue_and_edited_approvals_are_ignored(self):
        for variant in ("operator", "outsider", "malformed", "wrong-issue", "edited"):
            with self.subTest(variant=variant):
                self.setUp()
                self.label()
                self.body_edit()
                c = self.legacy_record(login=variant if variant in {"operator", "outsider"} else "maintainer")
                if variant == "malformed":
                    c["body"] = "<!-- ub-agents:approval:v1 -->\nInvalid record"
                elif variant == "wrong-issue":
                    c["body"] = legacy_approval_body(2, self.github.item(1).title, self.github.item(1).body, [])
                elif variant == "edited":
                    c["updated_at"] = at(16)
                self.assertFalse(self.check().allowed)

    def test_records_cannot_clear_listed_outside_comments(self):
        self.label()
        c = self.comment()
        other = self.comment(text="Not cleared", second=9)
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.legacy_record(comments=[c])
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.assertNotIn(other["id"], self.check().cleared_comment_ids)
        c.update(body="Changed input", updated_at=at(20))
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        c.update(body="More requirements", updated_at=at(21))
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.legacy_record(second=25, comments=[c, other])
        self.assertEqual(self.check().cleared_comment_ids, frozenset())
        self.label(second=30)
        self.assertEqual(self.check().cleared_comment_ids, {c["id"], other["id"]})

    def test_old_coordination_markers_are_excluded_without_parsing(self):
        self.label()
        for version in (1, 2, 3):
            self.comment(text=f'<!-- ub-agents:v{version} -->\nMalformed old record')
        self.assertTrue(self.check().allowed)

    def test_start_clears_existing_comments_but_later_edit_removes_clearance(self):
        c = self.comment(second=3)
        self.label()
        self.assertEqual(self.check().cleared_comment_ids, {c["id"]})
        c["updated_at"] = at(6)
        self.assertEqual(self.check().cleared_comment_ids, frozenset())

    def test_stale_or_forged_record_cannot_clear_outside_comments(self):
        self.label()
        c = self.comment()
        self.legacy_record(comments=[c], body="Stale body")
        self.legacy_record(login="operator", comments=[c])
        self.assertEqual(self.check().cleared_comment_ids, frozenset())

    def test_missing_history_blocks_and_hidden_revision_does_not_clear_outside_edits(self):
        self.label()
        self.body_edit()
        self.legacy_record()
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
        self.legacy_record(second=10)
        self.assertFalse(self.check().allowed)
