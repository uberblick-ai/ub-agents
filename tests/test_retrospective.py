from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Runtime
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.run_config import run_config
from tests.support import FakeGitHub, RecordingDiscussionRunner, agent, config, issue, run_environment, stub_refresh


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        role = agent(self.root, retrospectives=203)
        self.env = run_environment(config(self.root, role), role, {"run": "run", "assignment": 1, "id": 2})
        self.policy_path = Path(self.env["UB_AGENTS_RUN_CONFIG"])
        self.policy = json.loads(self.policy_path.read_text())
        self.body_path = self.root / "body.md"
        self.body = "Run #207 lost a review round. A configured board would have prevented it.\n"
        self.body_path.write_text(self.body)
        self.runner = RecordingDiscussionRunner()
        self.github = GitHub("org/project", self.runner)

    def cli(self, *arguments, env=None):
        calls = len(self.runner.calls)
        with patch.dict(os.environ, self.env if env is None else env, clear=True), \
                patch("ub_agents.cli.load_config", side_effect=AssertionError("worktree config")), \
                patch("ub_agents.cli.GitHub", return_value=self.github) as github, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            try:
                code = main(list(arguments) + ["retrospective", "--body-file", str(self.body_path)])
            except SystemExit as exc:
                code = exc.code
        if len(self.runner.calls) > calls:
            github.assert_called_once_with("org/project")
        else:
            github.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_posts_only_to_pinned_target_ignoring_worktree_and_explicit_config(self):
        changed = self.root / "ub-agents.yaml"
        changed.write_text("repository: stranger/repo\nagents:\n  worker:\n    retrospectives: 999\n")
        code, stdout, stderr = self.cli("--config", str(changed))
        self.assertEqual((code, stdout, stderr), (0, self.runner.comment_url + "\n", ""))
        self.assertEqual(len(self.runner.calls), 2)
        lookup, mutation = [json.loads(kwargs["input"]) for _, kwargs in self.runner.calls]
        self.assertEqual(lookup["variables"], {"owner": "org", "name": "project", "number": 203})
        self.assertEqual(mutation["variables"], {"discussionId": "configured-board", "body": self.body})
        self.assertNotIn("replyToId", mutation["query"])
        self.assertEqual(self.runner.writes, [mutation])

    def test_missing_or_invalid_run_config_never_reaches_github(self):
        for env in ({}, {"UB_AGENTS_RUN": "run"}, self.env | {"UB_AGENTS_RUN": "other"},
                    self.env | {"UB_AGENTS_REPOSITORY": "other/repo"},
                    self.env | {"UB_AGENTS_RUN_CONFIG": "relative.json"},
                    self.env | {"UB_AGENTS_RUN_CONFIG": str(self.root / "missing")}):
            with self.subTest(env=env):
                code, stdout, stderr = self.cli(env=env)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("requires a supervised", stderr)
        for value in ({}, [], None, "bad JSON", self.policy | {"retrospectives": True},
                      self.policy | {"retrospectives": 0}, self.policy | {"retrospectives": "203"},
                      self.policy | {"retrospectives": -1}, self.policy | {"retrospectives": 203.0},
                      self.policy | {"agent": ""}, self.policy | {"extra": 1}):
            with self.subTest(policy=value):
                self.policy_path.write_text(value if value == "bad JSON" else json.dumps(value))
                self.assertEqual(self.cli()[:2], (1, ""))
        self.assertEqual(self.runner.calls, [])

    def test_no_board_and_invalid_body_files_make_no_calls(self):
        self.policy_path.write_text(json.dumps(self.policy | {"retrospectives": None}))
        code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("No retrospective board configured for agent worker", stderr)
        self.policy_path.write_text(json.dumps(self.policy))
        for body in ("", " \n\t", None, b"\xff"):
            with self.subTest(body=body):
                if body is None:
                    self.body_path.unlink()
                elif isinstance(body, bytes):
                    self.body_path.write_bytes(body)
                else:
                    self.body_path.write_text(body)
                code, stdout, stderr = self.cli()
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("Retrospective body", stderr)
        self.body_path.unlink()
        self.body_path.mkdir()
        self.assertEqual(self.cli()[:2], (1, ""))
        self.assertEqual(self.runner.calls, [])

    def test_unreadable_body_file_fails_clearly(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.body_path:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            code, stdout, stderr = self.cli()
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("missing or unreadable", stderr)
        self.assertEqual(self.runner.calls, [])

    def test_missing_board_or_url_mismatch_refuses_mutation(self):
        for discussion in (None, {}, {"id": "", "url": self.runner.discussion["url"]}):
            self.runner.discussion = discussion
            self.assertEqual(self.cli()[:2], (1, ""))
        expected = "https://github.com/org/project/discussions/203"
        for url in ("https://github.com/stranger/repo/discussions/203", expected + "/",
                    expected + "?query=1", expected.replace("203", "204"),
                    expected.replace("https:", "http:"), expected.replace("org", "ORG"), None):
            with self.subTest(url=url):
                self.runner.discussion = {"id": "foreign-board", "url": url}
                code, stdout, stderr = self.cli()
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("URL mismatch", stderr)
                self.assertIn(expected, stderr)
        self.assertEqual(self.runner.writes, [])
        self.assertTrue(all(json.loads(kwargs["input"])["query"].startswith("query")
                            for _, kwargs in self.runner.calls))

    def test_failed_github_lookup_or_post_exits_nonzero_without_outcome_writes(self):
        for field in ("lookup_error", "post_error"):
            for error in ("Discussion unavailable", OSError("gh unavailable"),
                          subprocess.CompletedProcess([], 1, "", "GitHub call failed")):
                with self.subTest(field=field, error=error):
                    setattr(self.runner, field, error)
                    code, stdout, stderr = self.cli()
                    self.assertEqual((code, stdout), (1, ""))
                    self.assertIn("graphql", stderr)
                    setattr(self.runner, field, None)
        self.assertEqual(self.runner.writes, [])

    def test_caller_cannot_supply_target_options(self):
        for option in ("--repository", "--number", "--id", "--discussion-id", "--agent", "--config"):
            with self.subTest(option=option), patch.dict(os.environ, self.env, clear=True), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                main(["retrospective", "--body-file", str(self.body_path), option, "other"])
            self.assertEqual(exit.exception.code, 2)
        self.assertEqual(self.runner.calls, [])

    def test_launcher_pins_board_for_command_and_runtime_runs_and_post_preserves_outcome(self):
        stub_refresh(self)
        for cli in (None, "codex", "claude"):
            for board in (None, 203):
                with self.subTest(cli=cli, board=board):
                    role = agent(self.root, retrospectives=board)
                    if cli:
                        role = replace(role, command=(), runtimes=(Runtime(cli, "model", "high"),),
                                       runtime_args=("--allowedTools", "Bash({report_command} retrospective *)"))
                    coordination = FakeGitHub(issue())
                    loop = Loop(config(self.root, role), coordination, "operator", output=lambda *_: None)
                    def execute(command, cwd, env, run_dir, timeout, event, prompt, **kwargs):
                        pinned = json.loads(Path(env["UB_AGENTS_RUN_CONFIG"]).read_text())
                        self.assertEqual(pinned, run_config(loop.config, role, loop.coordinator.history(1)[-1]))
                        if cli:
                            self.assertEqual("Post a retrospective with" in prompt, board is not None)
                            self.assertIn(f"Bash({env['UB_AGENTS_REPORT']} retrospective *)", command)
                        else:
                            self.assertIsNone(prompt)
                        before = list(coordination.writes)
                        self.policy_path = Path(env["UB_AGENTS_RUN_CONFIG"])
                        self.runner.discussion = {"id": "configured-board",
                                                  "url": "https://github.com/org/project/discussions/203"}
                        code, _, _ = self.cli(env=env)
                        self.assertEqual(code, 0 if board is not None else 1)
                        self.runner.discussion["url"] = "https://github.com/stranger/repo/discussions/203"
                        self.assertEqual(self.cli(env=env)[0], 1)
                        self.assertEqual(coordination.writes, before)
                        self.assertFalse(any(record["kind"] == "outcome" for record in loop.coordinator.history(1)))
                        loop.coordinator.report(loop.coordinator.history(1)[-1], "blocked", "Verified")
                        return 0
                    self.runner.calls.clear()
                    # Plans need the runtime CLI on PATH; this machine may not have it.
                    with patch("ub_agents.loop.supervise", side_effect=execute) as executed, \
                            patch("ub_agents.coordination.shutil.which", return_value="/tools/runtime"):
                        self.assertTrue(loop.tick())
                    executed.assert_called_once()
