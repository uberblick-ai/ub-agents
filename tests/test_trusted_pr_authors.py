from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import check_pr
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.coordination import Coordinator
from ub_agents.discovery import Discovery
from ub_agents.errors import AgentError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.read_input import read_item, read_policy
from tests.support import FakeGitHub, RecordingRunner, agent, config, issue, pr, run_environment, stub_refresh
from tests.test_approvals import at
from tests.test_approval_batch import ApprovalFixtureRunner, fixture
from tests.test_read_input import feedback


BOT = {"login": "dependabot", "__typename": "Bot"}
REST_BOT = {"login": "dependabot[bot]", "type": "Bot"}
WITHHELD = {"withheld": True, "reason": "Body by a trusted bot author is not input"}
TITLE = "Bump package<!-- hidden -->\u200b"
CLEAN_TITLE = "Bump package[hidden content removed][hidden content removed]"


class TrustedPRAuthorTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worker = agent(self.root, kind="pr", triggers=("needs-review",))
        self.cfg = replace(config(self.root, self.worker), trusted_pr_authors=("dependabot",))
        self.github = FakeGitHub(replace(pr(labels=("needs-review",)), title=TITLE,
                                         body="Upstream release notes"))
        self.github.content_histories[2] = {"author": BOT}
        self.github.timelines[2] = [dict(event="labeled", actor={"login": "triager"},
                                        label={"name": "needs-review"}, created_at=at(5))]
        self.github.roles["triager"] = "triage"

    def check(self, number=2):
        return Loop(self.cfg, self.github, "operator").input_check(self.github.item(number))

    def read(self, number=2):
        return read_item(self.github, number, read_policy(self.cfg))

    def start(self, number=2, second=20, actor=None):
        self.github.timelines[number].append(dict(event="labeled", actor=actor or {"login": "maintainer"},
            label={"name": "needs-review"}, created_at=at(second)))

    def edit(self, field, editor, second=10, number=2):
        if field == "title":
            self.github.timelines[number].append(dict(event="renamed", actor=editor,
                rename={"from": "Old title", "to": TITLE}, created_at=at(second)))
        else:
            history = self.github.content_histories[number]
            history["lastEditedAt"] = at(second)
            history.setdefault("edits", []).append(dict(editedAt=at(second), editor=editor, deletedAt=None))

    def test_bot_matching_and_new_heads_need_no_start_or_review(self):
        for configured in ("dependabot", "DEPENDABOT[BOT]"):
            self.cfg = replace(self.cfg, trusted_pr_authors=(configured,))
            for author in (BOT, REST_BOT):
                self.github.content_histories[2]["author"] = author
                for head in ("a" * 40, "b" * 40):
                    with self.subTest(configured=configured, author=author, head=head):
                        self.github.change(2, head=head, head_repository="ORG/Project")
                        result = self.check()
                        self.assertTrue(result.allowed, result.reason)
                        self.assertIsNone(result.gate)
                        self.assertTrue(result.outside_input)
                        self.assertEqual(result.snapshot["head"], head)
                        self.assertEqual(result.snapshot["title"], CLEAN_TITLE)
                        self.assertEqual(result.snapshot["body"], WITHHELD)
                        self.assertEqual(result.snapshot, {k: v for k, v in self.read().items()
                                                           if k not in {"number", "kind", "state"}})
                        direct = check_pr(self.github, 2, ("needs-review",), trusted_pr_authors=(configured,))
                        self.assertEqual(direct, result)
        self.assertEqual(self.github.writes, [])

    def test_body_stays_withheld_after_starts_and_trusted_rewrites(self):
        self.start()
        self.edit("body", {"login": "operator"}, second=25)
        result = self.check()
        self.assertTrue(result.allowed)
        self.assertEqual(result.snapshot["body"], WITHHELD)
        self.assertEqual(self.read()["body"], WITHHELD)
        self.assertNotIn("Upstream release notes", json.dumps(result.snapshot))
        self.assertNotIn("Upstream release notes", json.dumps(self.read()))

    def test_only_the_author_bots_own_edits_are_exempt(self):
        for editor in (BOT, REST_BOT, {"login": "DEPENDABOT", "type": "Bot"}):
            for field in ("title", "body"):
                with self.subTest(editor=editor, field=field):
                    self.edit(field, editor)
                    result = self.check()
                    self.assertTrue(result.allowed, result.reason)
                    self.assertEqual(result.snapshot["title"], CLEAN_TITLE)
                    self.assertEqual(result.snapshot["body"], WITHHELD)
        # Even a start predating a later author edit does not create a gate.
        self.start(second=5)
        self.assertTrue(self.check().allowed)

    def test_other_outside_edits_gate_until_a_maintainer_start(self):
        for editor in ({"login": "outsider", "type": "User"},
                       {"login": "dependabot", "type": "User"},
                       {"login": "preview-bot", "type": "Bot"}, None):
            for field in ("title", "body"):
                with self.subTest(editor=editor, field=field):
                    self.setUp()
                    self.cfg = replace(self.cfg, trusted_pr_authors=("dependabot", "preview-bot"),
                                       trusted_bots=("preview-bot",))
                    self.edit(field, editor)
                    result = self.check()
                    self.assertFalse(result.allowed)
                    self.assertEqual(result.gate, "input")
                    self.assertIn(f"Outside {field} edit", result.reason)
                    self.assertTrue(self.read()[field]["withheld"])
                    self.start()
                    self.assertTrue(self.check().allowed)
                    self.assertEqual(self.read()["title"], CLEAN_TITLE)
                    self.assertEqual(self.read()["body"], WITHHELD)
                    self.edit(field, editor, second=25)
                    self.assertIn(f"Outside {field} edit after approval", self.check().reason)

    def test_outside_and_author_feedback_needs_clearance_without_suspension(self):
        stores = {"comments": self.github.store, "reviews": self.github.review_store,
                  "review_comments": self.github.review_comment_store}
        for store in stores.values():
            store[2] = [feedback(1, "dependabot[bot]", type="Bot"), feedback(2),
                        feedback(3, "operator")]
        for trusted_bots, expected in (((), [3]), (("dependabot",), [1, 3])):
            self.cfg = replace(self.cfg, trusted_bots=trusted_bots)
            result = self.check()
            self.assertTrue(result.allowed)
            for name in stores:
                self.assertEqual([row["id"] for row in result.snapshot[name]], expected)
                self.assertEqual(result.snapshot[name], self.read()[name])
                self.assertEqual(result.snapshot["withheld_counts"][name], 3 - len(expected))
        self.cfg = replace(self.cfg, trusted_bots=())
        self.start()
        for store in stores.values():
            store[2].append(feedback(4, second=25))
        result = self.check()
        self.assertTrue(result.allowed)
        for name in stores:
            self.assertEqual([row["id"] for row in result.snapshot[name]], [1, 2, 3])
            self.assertEqual(result.snapshot["withheld_counts"][name], 1)

    def test_forks_humans_and_unlisted_bots_keep_existing_gates_and_input_rules(self):
        for author, repository in ((BOT, "other/project"), (BOT, None),
                                   ({"login": "dependabot", "__typename": "User"}, "org/project"),
                                   ({"login": "other-bot", "__typename": "Bot"}, "org/project")):
            with self.subTest(author=author, repository=repository):
                self.setUp()
                self.github.content_histories[2]["author"] = author
                self.github.change(2, head_repository=repository)
                self.assertEqual(self.check().gate, "start")
                self.assertTrue(self.read()["body"]["withheld"])
                self.start()
                self.assertEqual(self.check().gate, "head")
                self.assertEqual(self.read()["body"], "Upstream release notes")
                self.github.review_store[2] = [feedback(1, "maintainer", second=25) | {"state": "APPROVED"}]
                self.assertTrue(self.check().allowed)

    def test_missing_key_and_approvals_off_keep_existing_behavior(self):
        self.cfg = replace(self.cfg, trusted_pr_authors=())
        self.assertEqual(self.check().gate, "start")
        self.start()
        self.assertEqual(self.check().gate, "head")
        self.assertEqual(self.read()["body"], "Upstream release notes")
        self.cfg = replace(self.cfg, approvals="off", trusted_pr_authors=("dependabot",))
        with patch.object(self.github, "pr_content", side_effect=AssertionError("history read")), \
                patch.object(self.github, "timeline", side_effect=AssertionError("history read")):
            result = self.check()
            self.assertTrue(result.allowed)
            self.assertFalse(result.outside_input)
            self.assertEqual(result.snapshot["body"], "Upstream release notes")
            self.assertEqual(self.read()["title"], TITLE)
            self.assertEqual(self.read()["body"], "Upstream release notes")

    def test_no_issue_start_head_approval_edit_clearance_or_launcher_authority(self):
        self.cfg = replace(self.cfg, agents=(replace(self.worker, kind="either"),))
        self.github.roles.update(dependabot="admin", **{"dependabot[bot]": "admin"})
        self.github.items[1] = issue(labels=("needs-review",))
        self.github.timelines[1] = []
        self.github.content_histories[1] = {"author": BOT}
        self.start(1, actor=REST_BOT)
        self.assertEqual(self.check(1).gate, "start")
        self.assertTrue(self.read(1)["body"]["withheld"])
        self.start(1)
        self.edit("body", BOT, second=25, number=1)
        self.assertIn("Outside body edit after approval", self.check(1).reason)
        self.github.content_histories[2]["author"] = {"login": "outsider"}
        self.start(actor=REST_BOT)
        self.assertEqual(self.check().gate, "start")
        self.start()
        self.github.review_store[2] = [feedback(1, "dependabot", type="Bot") | {"state": "APPROVED"}]
        self.assertEqual(self.check().gate, "head")
        coordinator = Coordinator(self.github, "operator", trusted_pr_authors=self.cfg.trusted_pr_authors)
        lease = coordinator.claim(coordinator.plan(self.github.item(2), self.worker, ()))
        self.github.store[2][0]["user"] = REST_BOT
        self.assertEqual(coordinator.history(2), [])
        self.assertFalse(coordinator.trust.observation()(BOT))
        self.assertIsNotNone(lease)

    def test_supervised_read_pins_the_author_list(self):
        env = run_environment(self.cfg, self.worker, {"run": "run", "assignment": 2, "id": 1})
        pinned = json.loads(Path(env["UB_AGENTS_RUN_CONFIG"]).read_text())
        self.assertEqual(pinned["trusted-pr-authors"], ["dependabot"])
        with patch.dict(os.environ, env, clear=True), \
                patch("ub_agents.cli.load_config", side_effect=AssertionError("worktree config")), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["read", "2", "--config", str(self.root / "changed.yaml")]), 0)
        self.assertEqual(json.loads(output.getvalue())["body"], WITHHELD)

    def test_batched_and_individual_reads_match_for_author_and_other_edits(self):
        for own_edit in (True, False):
            editor = BOT if own_edit else {"login": "outsider", "__typename": "User"}
            item, node, timeline = fixture({"start": False, "content": {
                "author": BOT, "title": TITLE, "lastEditedAt": at(10),
                "userContentEdits": {"totalCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None},
                                     "nodes": [{"editedAt": at(10), "editor": editor, "deletedAt": None}]}},
                "events": [{
                    "rest": {"id": 502, "event": "renamed", "created_at": at(10),
                             "actor": REST_BOT if own_edit else {"login": "outsider", "type": "User"},
                             "rename": {"from": "Old title", "to": TITLE}},
                    "graphql": {"__typename": "RenamedTitleEvent", "createdAt": at(10), "actor": editor,
                                "previousTitle": "Old title", "currentTitle": TITLE}}]}, "pr")
            results = []
            for bulk in (False, True):
                runner = ApprovalFixtureRunner(item, deepcopy(node), deepcopy(timeline))
                github = GitHub("org/project", runner)
                github.reviews = lambda _: []
                github.review_comments = lambda _: []
                loop = Loop(self.cfg, github, "operator")
                discovery = Discovery(github)
                if bulk:
                    discovery.prepare_approval_inputs([item])
                results.append(loop.input_check(item, discovery))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0].allowed, own_edit)
            self.assertEqual(results[0].snapshot["body"], WITHHELD)

    def test_approvals_off_does_not_label_or_change_launcher_trust(self):
        for policy in ("off", None):
            with self.subTest(policy=policy):
                self.setUp()
                self.cfg = replace(self.cfg, approvals=policy)
                self.github.repository_visibility = "private"
                self.github.roles["dependabot"] = "admin"
                loop = Loop(self.cfg, self.github, "operator", output=lambda *_: None)

                def execute(command, cwd, env, *args, **kwargs):
                    context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
                    self.assertEqual(context["title"], TITLE)
                    self.assertEqual(context["body"], "Upstream release notes")
                    self.assertNotIn("outside-input", self.github.item(2).labels)
                    loop.coordinator.report(loop.coordinator.history(2)[-1], "success", "Done", outcome="done")
                    return 0

                with patch("ub_agents.loop.supervise", side_effect=execute) as executed:
                    self.assertTrue(loop.tick())
                executed.assert_called_once()
                self.assertTrue(loop.coordinator.trust.observation()(BOT))

    def test_claim_context_and_label_survive_new_head_and_author_edits(self):
        for targeted in (False, True):
            with self.subTest(targeted=targeted):
                self.setUp()
                for head in ("a" * 40, "b" * 40):
                    self.github.change(2, head=head, labels=self.github.item(2).labels | {"needs-review"})
                    self.edit("title", REST_BOT)
                    self.edit("body", BOT)
                    output = []
                    loop = Loop(self.cfg, self.github, "operator", output=output.append)

                    def execute(command, cwd, env, *args, **kwargs):
                        context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
                        pinned = json.loads(Path(env["UB_AGENTS_RUN_CONFIG"]).read_text())
                        read = read_item(self.github, 2, pinned)
                        self.assertEqual(context["title"], CLEAN_TITLE)
                        self.assertEqual(context["body"], WITHHELD)
                        self.assertEqual(context["body"], read["body"])
                        self.assertEqual(context["candidate_sha"], head)
                        self.assertNotIn("Upstream release notes", json.dumps(context))
                        self.assertIn("outside-input", self.github.item(2).labels)
                        loop.coordinator.report(loop.coordinator.history(2)[-1], "success", "Done", outcome="done")
                        return 0

                    with patch("ub_agents.loop.supervise", side_effect=execute) as executed:
                        self.assertTrue(loop.tick_item(2) if targeted else loop.tick(), (head, output))
                    executed.assert_called_once()
                    self.assertNotIn("needs-human", self.github.item(2).labels)


class TrustedPRAuthorConfigurationTests(unittest.TestCase):
    def test_lists_and_offline_doctor_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "ub-agents.yaml"
            base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
            path.write_text(base)
            self.assertEqual(load_config(path).trusted_pr_authors, ())
            for value, expected in (("[]", ()), ('["Dependabot", "github-actions[BOT]"]',
                                                 ("Dependabot", "github-actions[BOT]"))):
                path.write_text(base + f"trusted-pr-authors: {value}\n")
                self.assertEqual(load_config(path).trusted_pr_authors, expected)
            for value in ("null", "dependabot", "{}", "[true]", "[1]", '[" "]', '[" dependabot"]',
                          '["a/b"]', '["@dependabot"]', "[dependabot, DEPENDABOT]", '["--bad"]'):
                with self.subTest(value=value):
                    path.write_text(base + f"trusted-pr-authors: {value}\n")
                    with self.assertRaisesRegex(AgentError, "trusted-pr-authors"):
                        load_config(path)
                    with patch("ub_agents.doctor.GitHub") as github, \
                            patch("ub_agents.doctor.subprocess.run", RecordingRunner(root)), \
                            patch("ub_agents.doctor.shutil.which", return_value="/tools/git"), \
                            redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                        self.assertEqual(main(["--config", str(path), "doctor", "--offline"]), 1)
                    github.assert_not_called()
                    self.assertIn("trusted-pr-authors", output.getvalue())
