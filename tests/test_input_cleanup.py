from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents import approvals
from ub_agents.loop import Loop
from ub_agents.read_input import read_item, read_policy
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh
from tests.test_approvals import at
from tests.test_read_input import feedback


RAW = "Visible<!-- hidden\ntext -->\u200b\u202e text\U000e0001\U000e007f."
CLEAN = "Visible[hidden content removed][hidden content removed] text[hidden content removed]."


class HiddenTextTests(unittest.TestCase):
    def test_comments_and_format_runs(self):
        for raw, expected in (
                ("", ""),
                ("  Café\n\t<details>[link](target) a\u0301\ufe0f\x00",
                 "  Café\n\t<details>[link](target) a\u0301\ufe0f\x00"),
                (RAW, CLEAN),
                ("a<!-- one --><!-- two -->b", "a[hidden content removed][hidden content removed]b"),
                ("`<!-- inline -->`\n```\n<!-- code -->\n```",
                 "`[hidden content removed]`\n```\n[hidden content removed]\n```"),
                ("before<!-- unterminated\n\u200bafter", "before[hidden content removed]"),
                ("<!--", "[hidden content removed]"),
                ("\u200b<!-- \u202e -->\u200d", "[hidden content removed]" * 3),
                ("\ufeff\u00adA\u2066\u2069B\U000e0061",
                 "[hidden content removed]A[hidden content removed]B[hidden content removed]")):
            with self.subTest(raw=raw):
                self.assertEqual(approvals.clean_outside_text(raw), expected)


class InputCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = config(self.root, agent(self.root))
        self.github = FakeGitHub(issue(), pr())
        self.github.next_id = 1000
        self.github.roles.update(outsider="read", admin="admin")
        self.github.timelines = {1: [], 2: []}
        self.github.content_histories = {n: {"author": {"login": "operator"}} for n in (1, 2)}
        original = self.github.pr_content
        self.enterContext(patch.object(self.github, "pr_content", side_effect=lambda n:
            original(n) | {"author": self.github.content_histories[n]["author"]}))
        self.github.review_store[2] = [feedback(100, "maintainer", 2) | {"state": "APPROVED"}]

    def start(self, number, second=10):
        self.github.timelines[number].append({"event": "labeled", "created_at": at(second),
            "label": {"name": "ready"}, "actor": {"login": "maintainer"}})

    def edit(self, number, field, login="outsider", second=5):
        if field == "title":
            self.github.timelines[number].append({"event": "renamed", "created_at": at(second),
                "actor": {"login": login}, "rename": {"from": "Visible", "to": RAW}})
        else:
            self.github.content_histories[number] |= {"lastEditedAt": at(second), "edits": [{
                "editedAt": at(second), "editor": {"login": login}, "diff": RAW, "deletedAt": None}]}

    def inputs(self, number):
        check = Loop(self.config, self.github, "operator").input_check(self.github.items[number])
        read = read_item(self.github, number, read_policy(self.config))
        self.assertEqual(check.snapshot, {name: read[name] for name in check.snapshot})
        return check

    def test_title_and_body_cleanup_depends_on_author_and_each_fields_history(self):
        for number in (1, 2):
            self.github.change(number, title=RAW, body=RAW)
            for author in ("outsider", "operator", "maintainer", "admin"):
                for editor, fields in ((None, ()), ("operator", ("title", "body")),
                        ("outsider", ("title",)), ("outsider", ("body",)),
                        ("outsider", ("title", "body"))):
                    with self.subTest(number=number, author=author, editor=editor, fields=fields):
                        self.github.timelines[number] = []
                        self.github.content_histories[number] = {"author": {"login": author}}
                        for field in fields:
                            self.edit(number, field, editor)
                        self.start(number)
                        check = self.inputs(number)
                        self.assertTrue(check.allowed, check.reason)
                        for field in ("title", "body"):
                            outside = author == "outsider" or editor == "outsider" and field in fields
                            self.assertEqual(check.snapshot[field], CLEAN if outside else RAW)
                        self.assertEqual((self.github.items[number].title, self.github.items[number].body),
                                         (RAW, RAW))

    def test_feedback_cleanup_preserves_trusted_rows_metadata_and_withheld_counts(self):
        self.config = replace(self.config, trusted_bots=("copilot",))
        for number in (1, 2):
            groups = {"comments": self.github.store}
            if number == 2:
                groups |= {"reviews": self.github.review_store, "review_comments": self.github.review_comment_store}
            self.start(number)
            for name, store in groups.items():
                store[number] = [feedback(1, body=RAW), feedback(2, "operator", body=RAW),
                    feedback(3, "maintainer", body=RAW), feedback(4, "admin", body=RAW),
                    feedback(5, "copilot", body=RAW, type="Bot"), feedback(6, second=15, body=RAW),
                    feedback(7, "other-bot", body=RAW, type="Bot"),
                    feedback(8, "operator", body="<!-- ub-agents:v99 -->\nrecord")]
            before = deepcopy(groups)
            check = self.inputs(number)
            self.assertEqual(check.snapshot["withheld_counts"], {name: 1 for name in groups})
            for name, store in groups.items():
                expected = [row | {"body": CLEAN} if row["id"] in (1, 7) else row
                            for row in store[number] if row["id"] not in (6, 8)]
                self.assertEqual(check.snapshot[name], expected)
                self.assertEqual(store, before[name])

    def test_withheld_fields_and_raw_approval_verdicts_reasons_and_gate_keys_are_preserved(self):
        for number in (1, 2):
            self.github.change(number, title=RAW, body=RAW)
            for author in ("outsider", "operator"):
                self.github.content_histories[number] = {"author": {"login": author}}
                for gate in ("start", "approved", "head", "title", "body", "feedback"):
                    with self.subTest(number=number, author=author, gate=gate):
                        self.github.timelines[number] = []
                        self.github.content_histories[number] = {"author": {"login": author}}
                        self.github.store[number] = [feedback(1, body=RAW)]
                        self.github.review_store[2][0]["commit_id"] = ("b" if gate == "head" else "a") * 40
                        if gate != "start":
                            self.start(number)
                        if gate in ("title", "body"):
                            self.edit(number, gate, second=15)
                        if gate == "feedback":
                            self.github.store[number][0]["updated_at"] = at(15)
                        # Return shared rows to catch accidental mutation of raw gate evidence.
                        with patch.object(self.github, "comments", return_value=self.github.store[number]):
                            before = deepcopy((self.github.store, self.github.content_histories, self.github.timelines))
                            cleaned = self.inputs(number)
                            with patch("ub_agents.approvals.clean_outside_text", side_effect=lambda text: text):
                                raw = self.inputs(number)
                        self.assertEqual(replace(cleaned, snapshot={}), replace(raw, snapshot={}))
                        self.assertEqual((self.github.store, self.github.content_histories, self.github.timelines), before)
                        for field in ("title", "body"):
                            if isinstance(raw.snapshot[field], dict):
                                self.assertEqual(cleaned.snapshot[field], raw.snapshot[field])

    def test_approvals_off_preserves_hidden_text_and_reads_no_history(self):
        self.config = replace(self.config, approvals="off", trusted_bots=("copilot",))
        for number in (1, 2):
            self.github.change(number, title=RAW, body=RAW)
            groups = {"comments": self.github.store}
            if number == 2:
                groups |= {"reviews": self.github.review_store, "review_comments": self.github.review_comment_store}
            for store in groups.values():
                store[number] = [feedback(1, "operator", body=RAW),
                    feedback(2, "copilot", body=RAW, type="Bot"), feedback(3, body=RAW)]
            with patch.object(self.github, "timeline", side_effect=AssertionError("history read")), \
                    patch.object(self.github, "issue_content", side_effect=AssertionError("history read")), \
                    patch.object(self.github, "pr_content", side_effect=AssertionError("history read")):
                check = self.inputs(number)
            self.assertTrue(check.allowed)
            self.assertEqual((check.snapshot["title"], check.snapshot["body"]), (RAW, RAW))
            for name in groups:
                self.assertEqual([row["body"] for row in check.snapshot[name]], [RAW, RAW])
                self.assertEqual(check.snapshot["withheld_counts"][name], 1)

    def test_actual_assignment_and_read_receive_the_cleaned_snapshot(self):
        stub_refresh(self)
        for number in (1, 2):
            self.github.change(number, title=RAW, body=RAW)
            self.github.content_histories[number]["author"] = {"login": "outsider"}
            self.github.store[number] = [feedback(1, body=RAW)]
            self.start(number)
            loop = Loop(self.config, self.github, "operator", output=lambda *_: None)

            def execute(command, cwd, env, *args, **kwargs):
                context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
                read = read_item(self.github, number, read_policy(self.config))
                for field in ("title", "body"):
                    self.assertEqual(context[field], CLEAN)
                    self.assertEqual(context[field], read[field])
                self.assertEqual(context["comments"][0]["body"], CLEAN)
                self.assertEqual(context["comments"], read["comments"])
                loop.coordinator.report(loop.coordinator.history(number)[-1], "blocked", "Verified")
                return 0

            with patch("ub_agents.loop.supervise", side_effect=execute) as executed:
                self.assertTrue(loop.tick_item(number))
            executed.assert_called_once()
