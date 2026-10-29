import json
from pathlib import Path
import tempfile
import unittest

from ub_agents.config import Runtime, load_config
from ub_agents.doctor import Doctor, diagnose
from ub_agents.errors import AgentError
from ub_agents.execution import command_for
from ub_agents.log_format import MAX_TEXT
from ub_agents.log_reader import LogReader
from ub_agents.opencode_logs import OpencodeFormatter
from ub_agents.runtime_usage import RuntimeUsage
from ub_agents.usage_output import UsageOutput
from tests.support import RecordingRunner


class OpencodeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "ub-agents.yaml"
        (self.root / "instructions.md").write_text("Implement the assigned issue")
        self.content = '''repository: org/project
runtime-args:
  opencode: [--auto]
runtime-updates:
  opencode: auto
agents:
  worker:
    runtime: ["codex:gpt-6.1-sol:high", "opencode:ollama/qwen3.8:27b-mlx"]
    instructions: instructions.md
    trigger: ready
    outcomes: {done: {}}
'''
        self.path.write_text(self.content)

    def test_config_local_overrides_offline_doctor_and_exact_model(self):
        # The issue's example also works as a machine-local replacement.
        local = self.root / "ub-agents.local.yaml"
        self.path.write_text(self.content.replace(', "opencode:ollama/qwen3.8:27b-mlx"', ''))
        local.write_text('agents:\n  worker:\n    runtime: ["codex:gpt-6.1-sol:high", "opencode:ollama/qwen3.8:27b-mlx"]\n')
        config = load_config(self.path)
        runtime = config.agents[0].runtimes[1]
        self.assertEqual((runtime.cli, runtime.model, runtime.effort), ("opencode", "ollama/qwen3.8:27b-mlx", ""))
        self.assertEqual(runtime.name, "opencode:ollama/qwen3.8:27b-mlx")
        self.assertEqual(config.agents[0].runtime_args_for(runtime), ("--auto",))
        self.assertEqual(config.runtime_updates.policies["opencode"], "auto")
        result = diagnose(self.path, offline=True, runner=RecordingRunner(self.root))
        self.assertTrue(result["ok"], result)

    def test_rejects_missing_provider_or_model_with_the_expected_form(self):
        for value in ("opencode:qwen3.8", "opencode:", "opencode:/model", "opencode:ollama/", "opencode: /model"):
            with self.subTest(value=value), self.assertRaisesRegex(AgentError, "opencode:provider/model"):
                Runtime.parse(value)

    def test_runtime_arguments_cannot_override_provenance_format_or_session(self):
        for flag in ("--model", "-m", "--variant", "--format", "--continue", "-c", "--session", "-s",
                     "--resume", "--fork", "--no-format", "-mollama/other:tag", "-sses_id"):
            for arg in (flag, flag + "=value"):
                with self.subTest(arg=arg):
                    self.path.write_text(self.content.replace("[--auto]", json.dumps([arg])))
                    result = diagnose(self.path, offline=True, runner=RecordingRunner(self.root))
                    self.assertFalse(result["ok"])
                    self.assertIn("must not change", next(c for c in result["checks"] if c["id"] == "config")["message"])

    def test_independence_ignores_only_the_opencode_provider(self):
        codex = Runtime.parse("codex:gpt-6.1-sol:high")
        hosted = Runtime.parse("opencode:openai/gpt-6.1-sol")
        local = Runtime.parse("opencode:ollama/qwen3.8:27b-mlx")
        self.assertFalse(hosted.different_from(codex))
        self.assertFalse(codex.different_from(hosted))
        self.assertTrue(local.different_from(codex))
        self.assertTrue(codex.different_from(local))
        self.assertFalse(hosted.different_from(local))
        self.assertFalse(local.different_from(Runtime.parse("opencode:other/model")))

    def test_argv_preserves_tag_and_adds_only_explicit_permission_arguments(self):
        config = load_config(self.path)
        agent = config.agents[0]
        runtime = agent.runtimes[1]
        base = ["opencode", "run", "--format", "json", "--model", "ollama/qwen3.8:27b-mlx"]
        self.assertEqual(command_for(agent, runtime, self.root), base + ["--auto"])
        self.path.write_text(self.content.replace("[--auto]", "[]"))
        self.assertEqual(command_for(load_config(self.path).agents[0], runtime, self.root), base)

    def test_doctor_reports_missing_executable_model_and_failed_model_probe(self):
        self.path.write_text(self.content.replace('"codex:gpt-6.1-sol:high", ', ''))
        config = load_config(self.path)
        for installed, listed, expected in ((False, "", "runtime-executable"),
                                             (True, "ollama/other:tag\n", "runtime-model"),
                                             (True, AgentError("failed"), "runtime-model"),
                                             (True, "ollama/qwen3.8:27b-mlx\n", None)):
            with self.subTest(installed=installed, listed=listed):
                runner = RecordingRunner(self.root)
                runner.responses[("opencode", "models")] = listed
                doctor = Doctor(runner=runner, which=lambda cli: "/tools/opencode" if installed else None)
                doctor.agents(config)
                failures = [c for c in doctor.checks if c.status == "fail"]
                if expected:
                    check = next(c for c in failures if c.id.startswith(expected + ":"))
                    self.assertTrue(check.remedy)
                else:
                    self.assertEqual(failures, [])
                self.assertFalse(any("auth" in call[0] for call in runner.calls))

    def test_limits_require_explicit_error_markers_and_never_pause_ollama(self):
        limited = {"type": "error", "error": {"name": "APIError", "data": {"statusCode": 429, "message": "rate limit"}}}
        ordinary = {"type": "error", "error": {"name": "APIError", "data": {"statusCode": 500, "message": "usage rate limit"}}}
        marked = {"type": "error", "error": {"name": "APIError", "data": {
            "statusCode": 402, "responseBody": json.dumps({"error": {"code": "usage_limit_exceeded"}})}}}
        malformed = {"type": "error", "error": {"data": {"code": [], "responseBody": "invalid"}}}
        for model, event, pauses in (("openai/model", limited, True), ("ollama/model:tag", limited, False),
                                      ("openai/model", marked, True), ("ollama/model:tag", marked, False),
                                      ("openai/model", malformed, False),
                                      ("openai/model", ordinary, False),
                                      ("openai/model", {"type": "text", "part": {"text": "rate limit 429"}}, False)):
            with self.subTest(model=model, event=event):
                usage = RuntimeUsage(clock=lambda: 1000, output=lambda _: None)
                output = UsageOutput("opencode", self.root, usage, {}, model=model)
                (self.root / "process.log").write_text(json.dumps(event) + "\n")
                output.poll(final=True)
                self.assertEqual(bool(usage.paused("opencode")), pauses)

    def test_log_reader_formats_messages_tools_errors_and_usage(self):
        fixture = Path(__file__).parent / "fixtures/runtime_logs/opencode.log"
        reader = LogReader(fixture, "opencode", attach=False)
        snapshot = reader.update()
        text = "\n".join(entry.text for entry in snapshot.entries)
        self.assertIn("I will inspect the file.", text)
        self.assertIn("▸ Bash git status --short", text)
        self.assertIn("▸ Edit src/example.py", text)
        self.assertIn("✗ Permission denied", text)
        self.assertIn("input 1,234", text)
        self.assertIn("output 42", text)
        self.assertIn("cache read 100", text)
        self.assertIn("✗ Provider unavailable", text)
        self.assertTrue(all(entry.compact for entry in snapshot.entries))
        self.assertTrue(all(entry.event for entry in snapshot.entries))

    def test_log_projection_is_bounded_and_escapes_controls(self):
        formatter = OpencodeFormatter()
        raw = json.dumps({"type": "text", "part": {"text": "first\x1b[2J\n" + "x" * 200000 + "last"}}).encode()
        entry = formatter.decode(raw)
        self.assertTrue(entry.compact)
        self.assertTrue(entry.shortened)
        self.assertLessEqual(len(entry.text), MAX_TEXT)
        self.assertNotIn("\x1b", entry.text)
        path = self.root / "process.log"
        path.write_bytes(b"")
        reader = LogReader(path, "opencode", attach=False)
        reader.update()
        path.write_bytes(raw + b"\n")
        for _ in range(10):
            snapshot = reader.update()
            if not snapshot.unread_bytes:
                break
        text = "\n".join(entry.text for entry in snapshot.entries)
        self.assertIn("first", text)
        self.assertIn("last", text)
