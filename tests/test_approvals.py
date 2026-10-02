from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ub_agents.approvals import (INPUT_VERSION, MARKER, IssueSnapshot, approval_body,
                                canonical_input, has_approval, input_comments,
                                input_digest, parse_approval)
from ub_agents.cli import main
from ub_agents.errors import AgentError
from ub_agents.records import MARKER as COORDINATION_MARKER
from tests.support import FakeGitHub, config, issue, pr


APPROVERS = ("Alice", "Bob", "launcher")
LAUNCHER = "Launcher"


def comment(number, author, body, issue_number=1, repository="org/project"):
    return {"id": number, "user": {"login": author}, "body": body,
            "issue_url": f"https://api.github.com/repos/{repository}/issues/{issue_number}"}


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = IssueSnapshot("org/project", 1, "Requirements", "Acceptance criteria",
                                      (comment(10, "writer", "Please add tests."),))

    def digest(self, snapshot=None):
        return input_digest(snapshot or self.snapshot, APPROVERS, LAUNCHER)

    def approved(self, **changes):
        body = approval_body(1, "implementation", self.digest())
        record = comment(20, "aLiCe", body)
        record.update(changes)
        return replace(self.snapshot, comments=self.snapshot.comments + (record,))

    def valid(self, snapshot):
        return has_approval(snapshot, "implementation", APPROVERS, LAUNCHER)

    def test_valid_approval_ignores_later_coordination_and_trusted_approvals(self):
        snapshot = self.approved()
        self.assertTrue(self.valid(snapshot))
        later = (comment(21, "lAuNcHeR", COORDINATION_MARKER + "\nlease or outcome"),
                 comment(22, "Bob", approval_body(1, "implementation", self.digest())),
                 comment(23, "Alice", approval_body(1, "implementation", "f" * 64)))
        snapshot = replace(snapshot, comments=tuple(reversed(snapshot.comments + later)))
        self.assertEqual(self.digest(snapshot), self.digest())
        self.assertTrue(self.valid(snapshot))
        self.assertEqual([c["id"] for c in input_comments(snapshot, APPROVERS, LAUNCHER)], [10])

    def test_untrusted_unlisted_issue_author_and_launcher_cannot_approve(self):
        for author in ("stranger", "writer", "LaUnChEr"):
            with self.subTest(author=author):
                snapshot = self.approved(user={"login": author})
                self.assertFalse(self.valid(snapshot))
                self.assertNotEqual(self.digest(snapshot), self.digest())
                self.assertEqual(len(input_comments(snapshot, APPROVERS, LAUNCHER)), 2)

    def test_payload_claims_do_not_supply_authority(self):
        text = approval_body(1, "implementation", self.digest())
        payload = parse_approval(text) | {"author": "Alice"}
        forged = f"{MARKER}\n```json\n{json.dumps(payload)}\n```\n"
        for author in ("stranger", "launcher"):
            with self.subTest(author=author):
                snapshot = self.approved(user={"login": author}, body=forged)
                self.assertFalse(self.valid(snapshot))
                self.assertEqual(len(input_comments(snapshot, APPROVERS, LAUNCHER)), 2)

    def test_title_body_and_new_edited_deleted_comments_stale_approval(self):
        snapshot = self.approved()
        edited = deepcopy(snapshot.comments[0])
        edited["body"] += " Changed."
        for changed in (replace(snapshot, title="Changed title"),
                        replace(snapshot, body="Changed body"),
                        replace(snapshot, comments=snapshot.comments + (comment(30, "Bob", "New requirements"),)),
                        replace(snapshot, comments=(edited, snapshot.comments[1])),
                        replace(snapshot, comments=(snapshot.comments[1],))):
            with self.subTest(snapshot=changed):
                self.assertNotEqual(self.digest(changed), self.digest())
                self.assertFalse(self.valid(changed))

    def test_wrong_stage_payload_issue_posting_issue_and_repository(self):
        cases = ({"body": approval_body(1, "preparation", self.digest())},
                 {"body": approval_body(2, "implementation", self.digest())},
                 {"issue_url": "https://api.github.com/repos/org/project/issues/2"},
                 {"issue_url": "https://api.github.com/repos/other/project/issues/1"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(self.valid(self.approved(**changes)))
        self.assertFalse(has_approval(self.approved(), "preparation", APPROVERS, LAUNCHER))
        self.assertFalse(self.valid(replace(self.approved(), repository="other/project")))
        self.assertFalse(self.valid(replace(self.approved(), number=2)))

    def test_marker_like_text_and_nonlauncher_coordination_remain_input(self):
        for author, text in (("Alice", COORDINATION_MARKER + "\nprepared"),
                             ("stranger", COORDINATION_MARKER + "\nready"),
                             ("Alice", MARKER + "\nnot a record"),
                             ("launcher", approval_body(1, "implementation", self.digest())),
                             ("Alice", approval_body(2, "implementation", self.digest()))):
            with self.subTest(author=author, text=text):
                snapshot = replace(self.snapshot, comments=self.snapshot.comments + (comment(25, author, text),))
                self.assertEqual(len(input_comments(snapshot, APPROVERS, LAUNCHER)), 2)
                self.assertNotEqual(self.digest(snapshot), self.digest())
                self.assertFalse(self.valid(snapshot))

    def test_approval_schema_rejects_malformed_and_ambiguous_json(self):
        valid = approval_body(1, "implementation", self.digest())
        for text in (valid.replace('"issue": 1', '"issue": true'),
                     valid.replace('"issue": 1', '"issue": 0'),
                     valid.replace('"issue": 1', '"issue": 1, "issue": 2'),
                     valid.replace(self.digest(), "no digest"),
                     valid.replace('"implementation"', '""'),
                     valid + "extra text", " " + valid,
                     f"{MARKER}\n```json\n[]\n```\n"):
            with self.subTest(text=text):
                self.assertIsNone(parse_approval(text))
                self.assertFalse(self.valid(self.approved(body=text)))

    def test_incomplete_comment_reads_fail_closed(self):
        for fields in ({"id": True}, {"body": 42}, {"user": {}}, {"issue_url": None}):
            broken = self.snapshot.comments[0] | fields
            with self.subTest(fields=fields), self.assertRaises(AgentError):
                self.digest(replace(self.snapshot, comments=(broken,)))
        with self.assertRaises(AgentError):
            self.digest(replace(self.snapshot, comments=self.snapshot.comments * 2))

    def test_canonical_vector_and_exact_text_preservation(self):
        snapshot = IssueSnapshot("org/project", 7, "Café", "Line 1\nLine 2",
                                 (comment(12, "Bob", "Second", issue_number=7),
                                  comment(3, "Alice", "First\n", issue_number=7)))
        serialized = ('{"body":"Line 1\\nLine 2","comments":[{"author":"Alice","body":"First\\n","id":3},'
                      '{"author":"Bob","body":"Second","id":12}],"number":7,"repository":"org/project",'
                      '"title":"Café","version":"ub-agent-issue-input:v1"}')
        self.assertEqual(canonical_input(snapshot, APPROVERS, LAUNCHER), serialized.encode("utf-8"))
        self.assertEqual(self.digest(snapshot), "fbb493a989883bee3b7041c52ae4c188c295a16f5f0a847091f2934b6071b762")
        self.assertIn(INPUT_VERSION, serialized)
        # Identity comparisons fold case; canonical source text does not.
        first = deepcopy(snapshot.comments[1])
        first["user"]["login"] = "alice"
        self.assertNotEqual(self.digest(snapshot), self.digest(replace(snapshot, comments=(snapshot.comments[0], first))))


class ApproveCommandTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub(issue())
        self.github.login = "Alice"
        self.github.store[1] = [comment(10, "writer", "Please add tests."),
                                comment(11, "Launcher", COORDINATION_MARKER + "\nlease")]
        self.github.next_id = 12
        self.config = replace(config(Path(".")), approvers=APPROVERS, launcher_account=LAUNCHER)

    def invoke(self, number=1):
        output, errors = io.StringIO(), io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main(["approve", "--number", str(number), "--stage", "implementation"])
        return code, output.getvalue(), errors.getvalue()

    def test_posts_valid_record_after_printing_digest_and_covered_comments(self):
        original = self.github.create_comment
        captured = []

        def post(number, body):
            import sys
            captured.append(sys.stdout.getvalue())
            return original(number, body)

        with patch.object(self.github, "create_comment", side_effect=post):
            code, output, errors = self.invoke()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("SHA-256:", captured[0])
        self.assertIn('"id": 10', captured[0])
        self.assertIn("Please add tests.", captured[0])
        self.assertNotIn('"id": 11', captured[0])
        self.assertNotIn("Approval posted:", captured[0])
        self.assertIn("Approval posted:", output)
        snapshot = IssueSnapshot("org/project", 1, "Requirements", "Acceptance criteria",
                                 tuple(self.github.comments(1)))
        self.assertTrue(has_approval(snapshot, "implementation", APPROVERS, LAUNCHER))
        self.assertEqual(self.github.writes, [("create", 12)])

    def test_refuses_unlisted_and_launcher_even_when_listed(self):
        for login in ("writer", "stranger", "LaUnChEr"):
            with self.subTest(login=login):
                self.github.login = login
                code, _, errors = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("listed approver other than the launcher", errors)
                self.assertEqual(self.github.writes, [])

    def test_missing_launcher_identity_pr_and_invalid_number_do_not_post(self):
        self.config = replace(self.config, launcher_account=None)
        self.assertIn("requires launcher-account", self.invoke()[2])
        self.config = replace(self.config, launcher_account=LAUNCHER)
        self.github.items[2] = pr()
        self.assertIn("requires an issue", self.invoke(2)[2])
        self.assertIn("positive issue number", self.invoke(0)[2])
        self.assertEqual(self.github.writes, [])

    def test_change_during_preview_refuses_without_posting(self):
        original = self.github.comments
        reads = 0

        def comments(number):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.github.store[1][0]["body"] = "Changed after preview"
            return original(number)

        with patch.object(self.github, "comments", side_effect=comments):
            code, output, errors = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("SHA-256:", output)
        self.assertIn("input changed", errors)
        self.assertEqual(self.github.writes, [])

    def test_read_failure_does_not_post(self):
        self.github.unreadable = True
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.github.writes, [])
