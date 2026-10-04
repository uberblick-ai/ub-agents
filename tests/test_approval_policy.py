from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import approval_body, trusted_input
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.errors import AgentError, GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from tests.support import PollGitHub, agent, config, issue, pr, stub_refresh
from tests.test_approval_enforcement import feedback


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.github = PollGitHub(issue())
        self.github.timelines[1] = []
        self.worker = agent(self.root)
        self.loop = Loop(replace(config(self.root, self.worker), approvals=None),
                         self.github, "operator", output=lambda _: None)

    def test_unset_defaults_and_visibility_changes_apply_each_pass(self):
        for visibility, policy, state in (("public", "on", "parked"), ("private", "off", "ready"),
                                          ("internal", "off", "ready"), ("public", "on", "parked")):
            with self.subTest(visibility=visibility):
                self.github.repository_visibility = visibility
                self.github.reads.clear()
                self.assertEqual(next(self.loop.iter_plans()).state, state)
                self.assertEqual(self.loop.approvals, policy)
                self.assertEqual(self.github.reads.count(("visibility", ())), 1)
                self.assertEqual(self.github.writes, [])

    def test_explicit_overrides_need_no_visibility_read(self):
        for visibility, policy, state in (("private", "on", "parked"), ("public", "off", "ready")):
            with self.subTest(policy=policy):
                self.github.repository_visibility = visibility
                self.loop.config = replace(self.loop.config, approvals=policy)
                with patch.object(self.github, "visibility", side_effect=AssertionError("unexpected read")):
                    self.assertEqual(self.loop.plans()[0].state, state)
                self.assertEqual(self.github.writes, [])

    def test_unreadable_visibility_fails_before_claims_or_parking(self):
        for targeted in (False, True):
            for failure in (GitHubError("GET", "repos/org/project", "Forbidden"),
                            AgentError("Repository visibility is unreadable")):
                with self.subTest(targeted=targeted, failure=failure):
                    self.github.read_results["visibility"] = [failure]
                    with patch.object(self.loop, "execute") as execute, self.assertRaises(AgentError):
                        self.loop.tick_item(1) if targeted else self.loop.tick()
                    execute.assert_not_called()
                    self.assertEqual(self.github.writes, [])
        for malformed in (None, "", "secret", [], True):
            self.github.repository_visibility = malformed
            with self.assertRaisesRegex(AgentError, "visibility is unreadable"):
                self.loop.plans()
            self.assertEqual(self.github.writes, [])

    def execute(self, verify, targeted=False):
        def run(command, cwd, env, run_dir, *args, **kwargs):
            context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
            verify(context)
            lease = self.loop.coordinator.history(context["assignment"])[-1]
            self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            return 0
        number = next(iter(self.github.items))
        with patch("ub_agents.loop.supervise", side_effect=run) as supervise:
            self.assertTrue(self.loop.tick_item(number) if targeted else self.loop.tick())
        supervise.assert_called_once()
        self.assertNotIn("needs-human", self.github.items[number].labels)
        self.assertFalse(any(c["body"].startswith(ACTION_MARKER) for c in self.github.store[number]))
        self.assertFalse(any(name == "add-labels" for name, *rest in self.github.writes))

    def test_off_filters_all_feedback_even_with_approval_and_runs_issue_and_pr(self):
        for kind in ("issue", "pr"):
            for targeted in (False, True):
                with self.subTest(kind=kind, targeted=targeted):
                    self.setUp()
                    item = issue() if kind == "issue" else pr()
                    self.github.items = {item.number: item}
                    # A triage-applied trigger must be enough; its history is never read.
                    self.github.timelines[item.number] = [{"event": "labeled", "actor": {"login": "triager"},
                        "label": {"name": next(iter(item.labels))}, "created_at": item.created_at}]
                    self.github.roles.update(writer="write", keeper="maintain", owner="admin",
                                             triager="triage", reader="read", unknown="custom", unreadable=None)
                    rows = [feedback(100 + i, login, login) for i, login in enumerate(
                        ("writer", "keeper", "owner", "triager", "reader", "unknown", "unreadable", "missing"))]
                    self.github.store[item.number] = rows[:]
                    self.github.store[item.number].append(feedback(200, "maintainer", approval_body(
                        item.number, item.title, item.body, rows, head=item.head,
                        reviews=rows if kind == "pr" else (), review_comments=rows if kind == "pr" else ())))
                    self.github.review_store[item.number] = rows[:]
                    self.github.review_comment_store[item.number] = rows[:]
                    self.github.change(item.number, title="Current outside title", body="Current outside body")
                    self.github.content_histories[item.number] = {"lastEditedAt": "unreadable", "edits": []}
                    self.loop.config = replace(self.loop.config, approvals="off")
                    def verify(context):
                        self.assertEqual((context["title"], context["body"]),
                                         ("Current outside title", "Current outside body"))
                        for name in ("comments", "reviews", "review_comments") if kind == "pr" else ("comments",):
                            self.assertEqual([row["id"] for row in context[name]], [100, 101, 102])
                        if kind == "pr":
                            self.assertEqual(context["candidate_sha"], item.head)
                    # Head ancestry, edit history and approval parsing are forbidden.
                    with patch.object(self.github, "timeline", side_effect=AssertionError("timeline read")), \
                            patch.object(self.github, "issue_content", side_effect=AssertionError("edit history read")), \
                            patch.object(self.github, "pr_content", side_effect=AssertionError("fork/approval read")), \
                            patch("ub_agents.approvals.parse_approval", side_effect=AssertionError("approval read")), \
                            patch("ub_agents.approvals.eligible_head", side_effect=AssertionError("ancestry read")):
                        self.execute(verify, targeted)
                    self.assertFalse({name for name, _ in self.github.reads} &
                                     {"timeline", "issue_content", "pr_content", "visibility"})

    def test_off_fork_head_is_ready_without_approval_reads(self):
        item = pr()
        raw = {"number": item.number, "title": item.title, "body": item.body,
               "state": "open", "draft": False, "labels": [{"name": "needs-changes"}],
               "created_at": item.created_at, "user": {"login": "outsider"},
               "head": {"sha": item.head, "ref": item.branch,
                        "repo": {"full_name": "outsider/fork"}}}
        github = GitHub("org/project")
        def request(endpoint, **kwargs):
            endpoint = endpoint.split("?", 1)[0]
            if endpoint == "repos/org/project/pulls/2":
                return raw
            if endpoint == "repos/org/project/collaborators/operator/permission":
                return {"role_name": "write"}
            if endpoint in {"repos/org/project/issues/2/comments", "repos/org/project/pulls/2/reviews",
                            "repos/org/project/pulls/2/comments"}:
                return []
            raise AssertionError(f"Unexpected approval read: {endpoint}")
        loop = Loop(replace(self.loop.config, approvals="off"), github, "operator", output=lambda _: None)
        with patch.object(github, "observe", return_value=[item]), \
                patch.object(github, "repository_comments", return_value=[]), \
                patch.object(github, "reviews", return_value=[]), \
                patch.object(github, "review_comments", return_value=[]), \
                patch.object(github, "request", side_effect=request):
            plan = loop.plans()[0]
        self.assertEqual((plan.state, plan.item.head), ("ready", item.head))

    def test_approve_still_posts_while_off(self):
        self.github.login = "maintainer"
        cfg = replace(self.loop.config, approvals="off")
        with patch("ub_agents.cli.load_config", return_value=cfg), \
                patch("ub_agents.cli.GitHub", return_value=self.github), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["approve", "--number", "1"]), 0)
        self.assertTrue(self.github.store[1][-1]["body"].startswith("<!-- ub-agents:approval:v1 -->"))

    def test_off_unreadable_author_lookup_excludes_input_without_parking(self):
        self.loop.config = replace(self.loop.config, approvals="off")
        self.github.store[1] = [feedback(100, "unreadable"), feedback(101, "operator")]
        original = self.github.role
        def role(login):
            if login == "unreadable":
                raise AgentError("Permission unavailable")
            return original(login)
        with patch.object(self.github, "role", side_effect=role):
            self.execute(lambda c: self.assertEqual([r["id"] for r in c["comments"]], [101]))

    def test_off_rechecks_comment_roles_on_unchanged_cached_items(self):
        self.loop.config = replace(self.loop.config, approvals="off")
        self.github.store[1] = [feedback(100, "commenter")]
        self.github.roles["commenter"] = "write"
        first = next(self.loop.iter_plans())
        self.assertEqual([r["id"] for r in self.loop.input_check(first.item, self.loop.discovery).snapshot["comments"]], [100])
        self.github.roles["commenter"] = "read"
        second = next(self.loop.iter_plans())
        self.assertEqual(self.loop.input_check(second.item, self.loop.discovery).snapshot["comments"], [])

    def test_off_permission_rate_limits_are_not_silently_filtered(self):
        self.github.store[1] = [feedback(100)]
        error = GitHubError("GET", "permission", "rate limit", rate_limited=True)
        with patch.object(self.github, "role", side_effect=error), self.assertRaises(GitHubError):
            trusted_input(self.github, self.github.items[1])


class PolicyConfigurationTests(unittest.TestCase):
    def test_policy_spellings_and_check_stays_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ub-agents.yaml"
            base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
            for value, expected in ((None, None), ("on", "on"), ("off", "off"), ("'on'", "on"), ('"off"', "off")):
                path.write_text(base + (f"approvals: {value}\n" if value is not None else ""))
                self.assertEqual(load_config(path).approvals, expected)
                with patch("ub_agents.cli.GitHub") as github, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["--config", str(path), "check"]), 0)
                github.assert_not_called()
                self.assertIn(f"Approvals: {expected} (config)" if expected else
                              "Approvals: from repository visibility", output.getvalue())
            for value in ("null", "true", "false", "yes", "no", "ON", "OFF", "0", "[]", "{}", "auto", "' '"):
                with self.subTest(value=value):
                    path.write_text(base + f"approvals: {value}\n")
                    with self.assertRaisesRegex(AgentError, "approvals must be on or off"):
                        load_config(path)

    def test_github_visibility_read_and_invalid_responses(self):
        github = GitHub("org/project")
        for value in ("public", "private", "internal"):
            with patch.object(github, "request", return_value={"visibility": value}) as request:
                self.assertEqual(github.visibility(), value)
                request.assert_called_once_with("repos/org/project")
        for raw in ({}, {"private": True}, {"visibility": None}, {"visibility": []}, [], None):
            with patch.object(github, "request", return_value=raw), \
                    self.assertRaisesRegex(AgentError, "visibility is unreadable"):
                github.visibility()
