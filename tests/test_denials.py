from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.denials import collect_denials, denial_count, denial_fields
from ub_agents.errors import LostOwnership, RetryableExecutionError
from ub_agents.loop import Loop
from ub_agents.records import attempts, seconds
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


def denied(tool="Bash", tool_input=None):
    return {"tool_name": tool, "tool_use_id": "call-id",
            "tool_input": {"command": "python -m tests"} if tool_input is None else tool_input}


def result(denials=()):
    return {"type": "result", "permission_denials": list(denials)}


class DenialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "process.log"

    def collect(self, *events, cli="claude"):
        self.path.write_text("not json\n[]\n" + "\n".join(json.dumps(event) for event in events))
        return collect_denials(cli, self.path)

    def test_structured_result_preserves_order_and_uses_command_path_or_compact_json(self):
        fields = self.collect(result([
            denied(tool_input={"command": "echo first", "file_path": "ignored"}),
            denied("Write", {"file_path": "/scratch/report.md", "content": "ignored"}),
            denied("Read", {"command": "not Bash", "offset": 2}),
            denied("Other", ["a", "b"]),
        ]))
        self.assertEqual(fields, {"denials": [
            {"tool": "Bash", "command": "echo first"},
            {"tool": "Write", "command": "/scratch/report.md"},
            {"tool": "Read", "command": '{"command":"not Bash","offset":2}'},
            {"tool": "Other", "command": '["a","b"]'},
        ]})

    def test_limit_and_command_truncation(self):
        fields = self.collect(result([denied(tool_input={"command": str(n) * 500}) for n in range(12)]))
        self.assertEqual(len(fields["denials"]), 10)
        self.assertEqual(fields["denials"][0], {"tool": "Bash", "command": "0" * 200})
        self.assertEqual(fields["denials_omitted"], 2)
        self.assertEqual(denial_count(fields), 12)
        for count in (0, 2, 10):
            fields = self.collect(result([denied()] * count))
            self.assertNotIn("denials_omitted", fields)
            self.assertEqual(denial_count(fields), count)

    def test_only_claude_result_events_record_denials_including_empty_lists(self):
        self.assertEqual(collect_denials("claude", self.path), {})
        self.assertEqual(self.collect({"type": "assistant", "permission_denials": [denied()]}), {})
        self.assertEqual(self.collect(result([denied()]), cli="codex"), {})
        self.assertEqual(self.collect({"type": "result"}), {"denials": []})
        self.assertEqual(self.collect(result([denied()]), result()), {"denials": []})

    def test_malformed_source_entries_are_ignored(self):
        self.assertEqual(self.collect({"type": "result", "permission_denials": "bad"}), {"denials": []})
        self.assertEqual(self.collect(result([None, {}, {"tool_name": 3}, denied()])),
                         {"denials": [{"tool": "Bash", "command": "python -m tests"}]})

    def test_display_readers_ignore_malformed_optional_fields(self):
        for value in (None, "bad", {}, 1, [None], [{}], [{"tool": "Bash", "command": []}]):
            with self.subTest(value=value):
                self.assertEqual(denial_fields({"denials": value, "denials_omitted": 2}), {})
                self.assertEqual(denial_count({"denials": value, "denials_omitted": 2}), 0)
        for omitted in (None, "2", -1, 0, True, [], {}):
            with self.subTest(omitted=omitted):
                fields = {"denials": [{"tool": "Bash", "command": "test"}], "denials_omitted": omitted}
                self.assertEqual(denial_count(fields), 1)
                self.assertNotIn("denials_omitted", denial_fields(fields))


class DenialsLoopTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.role = agent(self.root, command=(), kind="issue", runtimes=(Runtime("claude", "opus", "high"),))
        self.enterContext(patch("ub_agents.coordination.shutil.which", return_value="installed"))

    def run_agent(self, events, *, report=None, handoff=None, cli="claude", failure=None):
        github = FakeGitHub(issue(), pr())
        role = replace(self.role, runtimes=(Runtime(cli, "model", "high"),))
        loop = Loop(config(self.root, role), github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: seconds("2026-10-05T12:00:00Z")

        def execute(command, cwd, env, run_dir, *args, **kwargs):
            (run_dir / "process.log").write_text("\n".join(json.dumps(event) for event in events))
            lease = loop.coordinator.history(1)[0]
            if report:
                loop.coordinator.report(lease, report, "Agent report", handoff=handoff,
                                        outcome="done" if report == "success" else None,
                                        action="Maintainer: choose A or B; recommend A." if report == "blocked" else None)
            if failure:
                raise failure
            return 0

        accept = loop.coordinator.accept

        def accept_with_denials(lease, outcome):
            fields = collect_denials(cli, self.root / ".ub-agents" / "runs" / lease["run"] / "process.log")
            self.assertEqual(denial_fields(outcome), fields)
            self.assertFalse(outcome["accepted"])
            accept(lease, outcome)

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch.object(loop.coordinator, "accept", side_effect=accept_with_denials):
            if isinstance(failure, (KeyboardInterrupt, LostOwnership)):
                with self.assertRaises(type(failure)):
                    loop.tick()
            else:
                self.assertTrue(loop.tick())
        return loop, github, loop.coordinator.history(1)

    def test_reported_and_unreported_runs_keep_denials_without_changing_verdicts(self):
        for count in (2, 12):
            for report in (None, "success", "retry", "blocked"):
                with self.subTest(count=count, report=report):
                    loop, github, history = self.run_agent([result([denied()] * count)], report=report)
                    lease, outcome = history
                    self.assertEqual(len(outcome["denials"]), min(count, 10))
                    self.assertEqual(denial_count(outcome), count)
                    self.assertEqual(outcome.get("denials_omitted"), 2 if count == 12 else None)
                    expected = report or "retry"
                    self.assertEqual((outcome["status"], lease["result"]), (expected, expected))
                    self.assertEqual(outcome["accepted"], report == "success")
                    self.assertEqual(len(attempts(history, self.role.name, loop.coordinator.clock())),
                                     int(expected == "retry"))
                    label_writes = [write for write in github.writes if write[0] in {"add-labels", "remove-label"}]
                    self.assertEqual(label_writes, [("remove-label", 1, "ready")] if report == "success" else [])

    def test_handoff_copy_has_denials_before_acceptance(self):
        fields = {"denials": [{"tool": "Bash", "command": "python -m tests"}] * 10, "denials_omitted": 2}
        loop, github, history = self.run_agent([result([denied()] * 12)], report="success", handoff=2)
        copy = loop.coordinator.history(2)[0]
        self.assertEqual(denial_fields(history[1]), fields)
        self.assertEqual(denial_fields(copy), fields)
        self.assertTrue(copy["accepted"])
        self.assertEqual(copy["run"], history[1]["run"])

    def test_no_result_codex_and_aborted_runs_have_no_denial_fields(self):
        for events, cli, failure in (([], "claude", None), ([result([denied()])], "codex", None),
                ([], "claude", RetryableExecutionError("timed out")),
                ([], "claude", KeyboardInterrupt()), ([], "claude", LostOwnership("claim lost"))):
            with self.subTest(cli=cli, failure=type(failure).__name__):
                loop, github, history = self.run_agent(events, cli=cli, failure=failure)
                self.assertTrue(all("denials" not in record for record in history))
                self.assertTrue(all("denials_omitted" not in record for record in history))

    def test_result_without_denials_records_empty_list(self):
        _, _, history = self.run_agent([result()])
        self.assertEqual(history[1]["denials"], [])
        self.assertNotIn("denials_omitted", history[1])
