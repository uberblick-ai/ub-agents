from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.run_config import supervised_run
from tests.support import agent, config, run_environment


class RunConfigTests(unittest.TestCase):
    commands = (["read", "1"], ["retrospective", "--body-file", "unused.md"],
                ["report", "--status", "retry", "--summary", "Retry"])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        role = agent(root, retrospectives=203)
        self.env = run_environment(config(root, role), role, {"run": "run", "assignment": 1, "id": 2})
        self.path = Path(self.env["UB_AGENTS_RUN_CONFIG"])
        self.context = json.loads(self.path.read_text())

    def assert_refused(self, env=None, commands=None):
        for command in self.commands if commands is None else commands:
            with self.subTest(command=command), patch.dict(os.environ, self.env if env is None else env, clear=True), \
                    patch("ub_agents.cli.GitHub") as github, \
                    patch("ub_agents.cli.load_config", side_effect=AssertionError("local config")), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(command), 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("run.json (UB_AGENTS_RUN_CONFIG)", stderr.getvalue())
                github.assert_not_called()

    def test_missing_relative_and_unreadable_run_config(self):
        for value in (None, "", "relative.json", str(self.path.parent / "missing"), str(self.path.parent)):
            env = self.env | {"UB_AGENTS_RUN_CONFIG": value}
            if value is None:
                env.pop("UB_AGENTS_RUN_CONFIG")
            with self.subTest(path=value):
                self.assert_refused(env)
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assert_refused()

    def test_invalid_json_and_encoding(self):
        for content in (b"{", b"\xff", b"[]", b"null", b"true", b'"text"'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assert_refused()

    def test_missing_and_extra_keys(self):
        for field in self.context:
            with self.subTest(missing=field):
                self.path.write_text(json.dumps({k: v for k, v in self.context.items() if k != field}))
                self.assert_refused()
        self.path.write_text(json.dumps(self.context | {"extra": 1}))
        self.assert_refused()

    def test_invalid_field_types_and_values(self):
        invalid = {
            "repository": (None, 1, [], {}, "", "org", "org/project/other"),
            "run": (None, 1, [], {}, "", "../run"),
            "assignment": (None, True, "1", 1.0, 0, -1, [], {}),
            "lease_id": (None, True, "2", 2.0, 0, -1, [], {}),
            "agent": (None, True, 1, [], {}, "", "Worker", "../worker"),
            "approvals": (True, 1, [], {}, "invalid"),
            "trusted-bots": (None, True, "copilot", {}, [1], [" "], ["@copilot"], ["copilot", "Copilot"]),
            "triggers": (None, [], {}, {"issue": []}, {"issue": [], "pr": [], "extra": []},
                         {"issue": "ready", "pr": []}, {"issue": [], "pr": [1]},
                         {"issue": [" "], "pr": []}),
            "retrospectives": (True, "203", 203.0, 0, -1, [], {}),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.path.write_text(json.dumps(self.context | {field: value}))
                    env = self.env | {f"UB_AGENTS_{field.upper()}": value} if (
                        field in {"repository", "run"} and isinstance(value, str)) else self.env
                    self.assert_refused(env)

    def test_repository_and_run_must_match_environment(self):
        for field, mismatch in (("UB_AGENTS_REPOSITORY", "other/repo"), ("UB_AGENTS_RUN", "other")):
            for value in (None, "", mismatch):
                env = self.env | {field: value}
                if value is None:
                    env.pop(field)
                with self.subTest(field=field, value=value):
                    # Outside a run, read intentionally uses local configuration.
                    self.assert_refused(env, self.commands[1:] if field == "UB_AGENTS_RUN" and value is None else None)

    def test_optional_board_and_visibility_default_are_valid(self):
        self.path.write_text(json.dumps(self.context | {"approvals": None, "retrospectives": None}))
        with patch.dict(os.environ, self.env, clear=True):
            loaded = supervised_run()
        self.assertIsNone(loaded["approvals"])
        self.assertIsNone(loaded["retrospectives"])
