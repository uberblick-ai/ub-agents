from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents import approvals
from ub_agents.approvals import approval_body
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.coordination import Coordinator
from ub_agents.errors import AgentError, GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.read_input import read_item, read_policy
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh
from tests.test_approvals import at


def feedback(identity, login="outsider", second=3, body="Outside feedback", **author):
    return {"id": identity, "body": body, "user": {"login": login, **author},
            "created_at": at(second), "updated_at": at(second),
            "state": "COMMENTED", "commit_id": "a" * 40}


class ReadInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.github = FakeGitHub(issue(), pr())
        self.config = config(self.root, agent(self.root))
        self.github.timelines = {1: [], 2: []}
        self.github.content_histories[1] = {"author": {"login": "operator"}}
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def read(self, number=1):
        return read_item(self.github, number, read_policy(self.config))

    def cli(self, number="1", *arguments):
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            try:
                code = main(["read", number, *arguments])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(self.github.writes, [])
        return code, stdout.getvalue(), stderr.getvalue()

    def start(self, number=1, second=5, actor=None):
        self.github.timelines[number].append({"event": "labeled", "created_at": at(second),
                                             "label": {"name": "ready"},
                                             "actor": actor or {"login": "maintainer"}})

    def approve(self, number=1, second=10, **groups):
        item = self.github.items[number]
        row = feedback(1000 + second, "maintainer", second,
                       approval_body(number, item.title, item.body, groups.get("comments", []),
                                     head=item.head, reviews=groups.get("reviews", []),
                                     review_comments=groups.get("review_comments", [])))
        self.github.store.setdefault(number, []).append(row)
        return row

    def test_trusted_issue_without_start_is_readable_but_not_pickup_authorized(self):
        for state in ("open", "closed"):
            self.github.change(1, state=state, labels=frozenset())
            result = self.read()
            self.assertEqual((result["number"], result["kind"], result["state"]), (1, "issue", state))
            self.assertEqual((result["title"], result["body"]), ("Requirements", "Acceptance criteria"))
            self.assertFalse(Loop(self.config, self.github, "operator").input_check(self.github.items[1]).allowed)
        self.assertEqual(self.github.writes, [])

    def test_outside_issue_without_start_withholds_content_until_valid_record(self):
        self.github.content_histories[1]["author"] = {"login": "outsider"}
        result = self.read()
        self.assertTrue(result["title"]["withheld"])
        self.assertTrue(result["body"]["withheld"])
        self.assertNotIn("Acceptance criteria", json.dumps(result))
        self.approve()
        self.assertEqual(self.read()["body"], "Acceptance criteria")
        self.assertFalse(Loop(self.config, self.github, "operator").input_check(self.github.items[1]).allowed)

    def test_all_feedback_clearance_matches_assignment_context(self):
        self.config = replace(self.config, trusted_bots=("COPILOT",))
        self.github.roles.update(member="triage", reader="read", **{"other-bot": None})
        for number in (1, 2):
            groups = {"comments": self.github.store}
            if number == 2:
                groups |= {"reviews": self.github.review_store,
                           "review_comments": self.github.review_comment_store}
            for store in groups.values():
                store[number] = [feedback(1, "operator"), feedback(2),
                                 feedback(3, "member"), feedback(4, "reader"),
                                 feedback(5, "copilot", type="Bot"),
                                 feedback(6, "copilot", type="User"),
                                 feedback(7, "other-bot", __typename="Bot"),
                                 feedback(8, "maintainer", body="<!-- ub-agents:v99 -->\nrecord"),
                                 feedback(9, "operator", body="<!-- ub-agents:action-needed old-run -->\nnotice")]
            self.approve(number, **{name: [store[number][1]] for name, store in groups.items()})
            loop = Loop(self.config, self.github, "operator")
            loop.approvals = "on"
            for store in groups.values():
                store[number].append(feedback(10, second=15))
            for edit in (False, True):
                if edit:
                    for store in groups.values():
                        store[number][1]["updated_at"] = at(20)
                read = self.read(number)
                assignment = loop.input_check(self.github.items[number]).snapshot
                for name in groups:
                    expected = [1, 5] if edit else [1, 2, 5]
                    self.assertEqual([r["id"] for r in read[name]], expected)
                    self.assertEqual(read[name], assignment[name])
                    self.assertEqual(read["withheld_counts"][name], 6 if edit else 5)
                self.assertEqual(read["withheld_counts"], assignment["withheld_counts"])
        self.assertEqual(self.github.writes, [])

    def test_unlisted_bots_with_unreadable_permission_roles_are_withheld_and_counted(self):
        self.github.roles.update({"dependabot[bot]": None, "dependabot": None, "copilot": None})
        for policy in ("on", "off"):
            self.config = replace(self.config, approvals=policy)
            for number in (1, 2):
                with self.subTest(policy=policy, number=number):
                    groups = {"comments": self.github.store}
                    if number == 2:
                        groups |= {"reviews": self.github.review_store,
                                   "review_comments": self.github.review_comment_store}
                    for name, store in groups.items():
                        login = "dependabot" if name == "reviews" else "dependabot[bot]"
                        identity = {"__typename": "Bot"} if name == "reviews" else {"type": "Bot"}
                        store[number] = [feedback(1, login, **identity),
                                         feedback(2, "Copilot", type="Bot"), feedback(3, "operator")]
                        self.assertIsNone(self.github.role(login))
                    result = self.read(number)
                    loop = Loop(self.config, self.github, "operator")
                    loop.approvals = policy
                    assignment = loop.input_check(self.github.items[number]).snapshot
                    for name in groups:
                        self.assertEqual([row["id"] for row in result[name]], [3])
                        self.assertEqual(result["withheld_counts"][name], 2)
                        self.assertEqual(result[name], assignment[name])
                    self.assertEqual(self.cli(str(number))[0], 0)
        self.assertEqual(self.github.writes, [])

    def test_bot_authors_starts_and_edits_need_no_permission_lookup(self):
        self.github.content_histories[1]["author"] = {"login": "dependabot", "__typename": "Bot"}
        original = self.github.pr_content
        with patch.object(self.github, "pr_content", side_effect=lambda n: original(n) | {
                "author": {"login": "dependabot", "__typename": "Bot"}}):
            for number in (1, 2):
                self.start(number, actor={"login": "dependabot[bot]", "type": "Bot"})
                self.github.timelines[number].append({"event": "renamed", "created_at": at(10),
                    "actor": {"login": "Copilot", "type": "Bot"},
                    "rename": {"from": "Old title", "to": self.github.items[number].title}})
                history = self.github.content_histories.setdefault(number, {})
                history |= {
                    "lastEditedAt": at(10), "edits": [{"editedAt": at(10),
                    "editor": {"login": "dependabot", "__typename": "Bot"},
                    "diff": self.github.items[number].body, "deletedAt": None}]}
                with patch.object(self.github, "role", side_effect=AssertionError("Bot permission lookup")):
                    result = self.read(number)
                self.assertTrue(result["title"]["withheld"])
                self.assertTrue(result["body"]["withheld"])

    def test_trusted_bots_match_rest_and_graphql_logins_in_every_feedback_group(self):
        for configured in ("COPILOT-PULL-REQUEST-REVIEWER", "copilot-pull-request-reviewer[bot]"):
            for policy in ("on", "off"):
                with self.subTest(configured=configured, policy=policy):
                    self.config = replace(self.config, approvals=policy,
                                          trusted_bots=(configured, "github-actions[bot]", "Copilot"))
                    self.github.store[2] = [feedback(1, "copilot-pull-request-reviewer[bot]", type="Bot"),
                                           feedback(2, "github-actions[bot]", type="Bot"),
                                           feedback(3, "copilot-pull-request-reviewer", type="User"),
                                           feedback(4, "dependabot[bot]", type="Bot")]
                    self.github.review_store[2] = [feedback(1, "copilot-pull-request-reviewer", __typename="Bot"),
                                                  feedback(2, "github-actions", __typename="Bot"),
                                                  feedback(3, "copilot-pull-request-reviewer", __typename="User"),
                                                  feedback(4, "dependabot", __typename="Bot")]
                    self.github.review_comment_store[2] = [feedback(1, "Copilot", type="Bot"),
                                                          feedback(2, "github-actions[bot]", type="Bot"),
                                                          feedback(3, "Copilot", type="User"),
                                                          feedback(4, "dependabot[bot]", type="Bot")]
                    result = self.read(2)
                    loop = Loop(self.config, self.github, "operator")
                    loop.approvals = policy
                    assignment = loop.input_check(self.github.items[2]).snapshot
                    for name in ("comments", "reviews", "review_comments"):
                        self.assertEqual([row["id"] for row in result[name]], [1, 2])
                        self.assertEqual(result["withheld_counts"][name], 2)
                        self.assertEqual(result[name], assignment[name])

    def test_copilot_inline_login_requires_its_own_trusted_entry(self):
        self.config = replace(self.config, trusted_bots=("copilot-pull-request-reviewer",))
        self.github.review_comment_store[2] = [feedback(1, "Copilot", type="Bot")]
        result = self.read(2)
        self.assertEqual(result["review_comments"], [])
        self.assertEqual(result["withheld_counts"]["review_comments"], 1)

    def test_suffix_alias_never_grants_bot_authority(self):
        self.config = replace(self.config, trusted_bots=("review-app",))
        self.github.roles.update({"review-app[bot]": "admin", "review-app": "admin"})
        bot = {"login": "review-app[bot]", "type": "Bot"}
        self.github.content_histories[1]["author"] = bot
        self.start(actor=bot)
        self.approve()["user"] = bot
        self.assertTrue(self.read()["body"]["withheld"])
        self.assertFalse(Loop(self.config, self.github, "operator").input_check(self.github.items[1]).allowed)
        trust = Coordinator(self.github, "operator", trusted_bots=self.config.trusted_bots).trust
        self.assertFalse(trust(bot))
        self.assertFalse(trust.observation()({"login": "review-app", "__typename": "Bot"}))
        self.assertTrue(trust({"login": "review-app", "type": "User"}))

    def test_start_clears_feedback_and_later_edit_removes_clearance_in_every_group(self):
        for number in (1, 2):
            stores = [self.github.store]
            if number == 2:
                stores += [self.github.review_store, self.github.review_comment_store]
            for store in stores:
                store[number] = [feedback(1)]
            self.start(number)
            result = self.read(number)
            for name in result["withheld_counts"]:
                self.assertEqual([r["id"] for r in result[name]], [1])
                self.assertEqual(result["withheld_counts"][name], 0)
            for store in stores:
                store[number][0]["updated_at"] = at(5)
            result = self.read(number)
            for name in result["withheld_counts"]:
                self.assertEqual(result[name], [])
                self.assertEqual(result["withheld_counts"][name], 1)

    def test_title_and_body_edits_withhold_only_affected_field_until_reapproved(self):
        for number in (1, 2):
            self.start(number)
            self.github.change(number, title="Uncleared title text", body="Uncleared body text")
            self.github.timelines[number].append({"event": "renamed", "created_at": at(15),
                "actor": {"login": "outsider"}, "rename": {"from": "Requirements" if number == 1 else "Candidate",
                                                            "to": "Uncleared title text"}})
            self.github.content_histories[number] = {"author": {"login": "operator"},
                "lastEditedAt": at(15), "edits": [{"editedAt": at(15), "editor": {"login": "outsider"},
                                                   "diff": "Uncleared body text", "deletedAt": None}]}
            result = self.read(number)
            self.assertTrue(result["title"]["withheld"])
            self.assertTrue(result["body"]["withheld"])
            self.assertNotIn("Uncleared title text", json.dumps(result))
            self.assertNotIn("Uncleared body text", json.dumps(result))
            self.approve(number, 20)
            result = self.read(number)
            self.assertEqual((result["title"], result["body"]), ("Uncleared title text", "Uncleared body text"))
            self.github.change(number, body="Another edit")
            self.github.content_histories[number]["lastEditedAt"] = at(25)
            self.github.content_histories[number]["edits"].append({"editedAt": at(25),
                "editor": {"login": "outsider"}, "diff": "Another edit", "deletedAt": None})
            result = self.read(number)
            self.assertEqual(result["title"], "Uncleared title text")
            self.assertTrue(result["body"]["withheld"])
            self.assertFalse(Loop(self.config, self.github, "operator").input_check(self.github.items[number]).allowed)

    def test_bot_cannot_start_approve_or_trust_title_body_edits(self):
        self.config = replace(self.config, trusted_bots=("copilot",))
        self.github.content_histories[1]["author"] = {"login": "outsider"}
        self.github.roles["copilot"] = "admin"
        bot = {"login": "copilot", "type": "Bot"}
        self.start(actor=bot)
        self.approve()["user"] = bot
        self.assertTrue(self.read()["body"]["withheld"])
        self.assertFalse(Loop(self.config, self.github, "operator").input_check(self.github.items[1]).allowed)

    def test_listed_bot_approving_review_and_coordination_records_grant_nothing(self):
        self.config = replace(self.config, trusted_bots=("copilot",))
        self.github.roles["copilot"] = "admin"
        self.github.review_store[2] = [feedback(1, "copilot", __typename="Bot") | {"state": "APPROVED"}]
        self.start(2)
        original = self.github.pr_content
        with patch.object(self.github, "pr_content", side_effect=lambda n: original(n) | {
                "author": {"login": "outsider"}}):
            check = Loop(self.config, self.github, "operator").input_check(self.github.items[2])
        self.assertFalse(check.allowed)
        self.assertEqual(check.gate, "head")
        self.assertEqual([row["id"] for row in self.read(2)["reviews"]], [1])
        coordinator = Coordinator(self.github, "operator")
        coordinator.claim(coordinator.plan(self.github.items[1], agent(self.root), ()))
        self.github.store[1][0]["user"] = {"login": "copilot", "type": "Bot"}
        listed = Coordinator(self.github, "operator", trusted_bots=self.config.trusted_bots)
        self.assertEqual(listed.history(1), [])
        self.assertFalse(listed.trust.observation()({"login": "copilot", "type": "Bot"}))
        self.assertTrue(listed.trust.observation()({"login": "copilot", "type": "User"}))

    def test_bot_title_body_edits_are_outside_even_with_write_permissions(self):
        self.config = replace(self.config, trusted_bots=("copilot",))
        self.github.roles["copilot"] = "write"
        self.github.change(1, body="Bot edit")
        self.github.content_histories[1] |= {"lastEditedAt": at(10), "edits": [{
            "editedAt": at(10), "editor": {"login": "copilot", "__typename": "Bot"},
            "diff": "Bot edit", "deletedAt": None}]}
        self.assertTrue(self.read()["body"]["withheld"])

    def test_stale_forged_edited_and_same_second_records_do_not_clear_input(self):
        self.github.content_histories[1]["author"] = {"login": "outsider"}
        comment = feedback(1)
        self.github.store[1] = [comment]
        for variant in ("stale", "forged", "edited", "same-second"):
            with self.subTest(variant=variant):
                self.github.store[1] = [comment]
                row = self.approve(second=3 if variant == "same-second" else 10, comments=[comment])
                if variant == "stale":
                    row["body"] = approval_body(1, "Stale title", "Acceptance criteria", [comment])
                elif variant == "forged":
                    row["user"] = {"login": "operator"}
                elif variant == "edited":
                    row["updated_at"] = at(11)
                result = self.read()
                self.assertEqual(result["comments"], [])
                self.assertEqual(result["withheld_counts"]["comments"], 1)
                if variant != "same-second":
                    self.assertTrue(result["body"]["withheld"])

    def test_off_uses_current_content_and_only_write_feedback_without_history_reads(self):
        self.config = replace(self.config, approvals="off", trusted_bots=("copilot",))
        self.github.content_histories[1] = {"author": {"login": "outsider"}}
        self.github.roles.update(member="triage", reader="read")
        for number in (1, 2):
            stores = {"comments": self.github.store}
            if number == 2:
                stores |= {"reviews": self.github.review_store, "review_comments": self.github.review_comment_store}
            for store in stores.values():
                store[number] = [feedback(1, "operator"), feedback(2, "copilot", __typename="Bot"),
                                 feedback(3, "copilot", type="User"), feedback(4, "member"),
                                 feedback(5, "reader"), feedback(6, "other", type="Bot"), feedback(7)]
            self.approve(number, **{name: [store[number][-1]] for name, store in stores.items()})
            with patch.object(self.github, "timeline", side_effect=AssertionError("history read")), \
                    patch.object(self.github, "issue_content", side_effect=AssertionError("history read")), \
                    patch.object(self.github, "pr_content", side_effect=AssertionError("history read")):
                result = self.read(number)
            self.assertEqual(result["body"], self.github.items[number].body)
            for name in stores:
                self.assertEqual([r["id"] for r in result[name]], [1, 2])
                self.assertEqual(result["withheld_counts"][name], 5)

    def test_cli_failures_emit_no_item_content_and_make_no_writes(self):
        for policy in ("on", "off"):
            self.config = replace(self.config, approvals=policy)
            self.github.store[1] = [feedback(1, "unknown")]
            for failure in (None, AgentError("Permission unavailable")):
                with patch.object(self.github, "role", **({"return_value": failure} if failure is None else
                                                         {"side_effect": failure})):
                    code, stdout, stderr = self.cli()
                self.assertEqual((code, stdout), (1, ""))
                self.assertTrue(stderr)
        self.config = replace(self.config, approvals="on")
        for method in ("timeline", "issue_content", "comments"):
            with patch.object(self.github, method, side_effect=AgentError("Unavailable")):
                self.assertEqual(self.cli()[:2], (1, ""))
        self.config = replace(self.config, approvals=None)
        for visibility in (None, "secret", []):
            self.github.repository_visibility = visibility
            self.assertEqual(self.cli()[:2], (1, ""))
        self.github.repository_visibility = "public"
        self.assertEqual(self.cli("999")[:2], (1, ""))
        for invalid in ("0", "-1", "other/repo#1", "https://github.com/other/repo/issues/1"):
            self.assertEqual(self.cli(invalid)[:2], (2, ""))

    def test_visibility_defaults_and_explicit_override(self):
        self.github.content_histories[1]["author"] = {"login": "outsider"}
        self.config = replace(self.config, approvals=None)
        for visibility, withheld in (("public", True), ("private", False), ("internal", False)):
            self.github.repository_visibility = visibility
            self.assertEqual(isinstance(self.read()["body"], dict), withheld)
        self.config = replace(self.config, approvals="on")
        with patch.object(self.github, "visibility", side_effect=AssertionError("visibility read")):
            self.assertTrue(self.read()["body"]["withheld"])

    def test_assignment_and_read_use_the_same_filter_entry_point(self):
        self.start()
        with patch("ub_agents.approvals.filter_input", wraps=approvals.filter_input) as filtering:
            Loop(self.config, self.github, "operator").input_check(self.github.items[1])
            self.read()
        self.assertEqual(filtering.call_count, 2)
        self.assertEqual(filtering.call_args_list[0].args[1:3], filtering.call_args_list[1].args[1:3])
        self.assertEqual(set(filtering.call_args_list[0].args[3]), set(filtering.call_args_list[1].args[3]))
        self.assertTrue(filtering.call_args_list[1].kwargs["read_only"])

    def test_supervised_read_uses_pinned_policy_and_ignores_worktree_config(self):
        policy_path = self.root / "read-config.json"
        policy_path.write_text(json.dumps(read_policy(replace(self.config, approvals="off"))))
        env = {"UB_AGENTS_RUN": "run", "UB_AGENTS_REPOSITORY": "org/project",
               "UB_AGENTS_READ_CONFIG": str(policy_path)}
        with patch.dict(os.environ, env), patch("ub_agents.cli.load_config", side_effect=AssertionError("worktree config")), \
                patch("ub_agents.cli.GitHub", return_value=self.github) as github, \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["read", "1", "--config", str(self.root / "changed.yaml")]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["body"], "Acceptance criteria")
        github.assert_called_once_with("org/project")
        with patch.dict(os.environ, {"UB_AGENTS_RUN": "run"}):
            self.assertEqual(self.cli()[:2], (1, ""))
        for malformed in ({}, [], None, read_policy(self.config) | {"triggers": ["issue", "pr"]},
                          read_policy(self.config) | {"repository": "other/repository"}):
            policy_path.write_text(json.dumps(malformed))
            with patch.dict(os.environ, env):
                self.assertEqual(self.cli()[:2], (1, ""))

    def test_actual_assignment_receives_bot_feedback_and_pinned_read_policy(self):
        stub_refresh(self)
        self.config = replace(self.config, trusted_bots=("copilot",))
        self.github.review_store[2] = [feedback(1, "copilot", __typename="Bot"), feedback(2)]
        loop = Loop(self.config, self.github, "operator", output=lambda *_: None)
        def execute(command, cwd, env, *args, **kwargs):
            context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
            pinned = json.loads(Path(env["UB_AGENTS_READ_CONFIG"]).read_text())
            self.assertEqual(pinned, read_policy(self.config))
            read = read_item(self.github, 2, pinned)
            self.assertEqual(context["reviews"], read["reviews"])
            self.assertEqual([r["id"] for r in context["reviews"]], [1])
            self.assertEqual(context["withheld_counts"], read["withheld_counts"])
            loop.coordinator.report(loop.coordinator.history(2)[-1], "blocked", "Verified")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute) as executed:
            self.assertTrue(loop.tick_item(2))
        executed.assert_called_once()

    def test_transferred_issue_and_foreign_base_pr_fail_closed(self):
        github = GitHub("org/project")
        for raw, kind in (({"repository_url": "https://api.github.com/repos/other/project"}, None),
                          ({"base": {"repo": {"full_name": "other/project"}}}, "pr")):
            with patch.object(github, "request", return_value=raw), self.assertRaisesRegex(GitHubError, "outside"):
                github.item(1, kind)

    def test_incomplete_or_changed_history_shows_no_json(self):
        self.github.content_histories[1]["lastEditedAt"] = at(10)
        self.assertEqual(self.cli()[:2], (1, ""))
        self.github.content_histories[1] = {"title": "Changed during read"}
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("changed while reading", stderr)


class TrustedBotConfigurationTests(unittest.TestCase):
    def test_valid_bot_lists_and_malformed_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ub-agents.yaml"
            base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
            for value, expected in (("[]", ()),
                                    ('["Copilot", "github-actions[BOT]"]', ("Copilot", "github-actions[BOT]"))):
                path.write_text(base + f"trusted-bots: {value}\n")
                self.assertEqual(load_config(path).trusted_bots, expected)
            for value in ('null', 'copilot', '{}', '[true]', '[1]', '[" "]', '[" copilot"]',
                          '["a/b"]', '["@copilot"]', '[copilot, Copilot]', '["--bad"]'):
                with self.subTest(value=value):
                    path.write_text(base + f"trusted-bots: {value}\n")
                    with self.assertRaisesRegex(AgentError, "trusted-bots"):
                        load_config(path)
                    with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                        self.assertEqual(main(["check", "--config", str(path)]), 1)
                    self.assertEqual(stdout.getvalue(), "")
