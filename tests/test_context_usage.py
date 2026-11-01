import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.context_usage import collect_context, context_fields
from ub_agents.denials import collect_denials
from ub_agents.errors import ExecutionStopped, LostOwnership, RetryableExecutionError
from ub_agents.loop import Loop
from ub_agents.records import MARKER, attempts, body, payload, records, seconds
from ub_agents.usage_output import collect_usage
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


FIXTURES = Path(__file__).parent / "fixtures" / "runtime_logs"
THREAD = "11111111-1111-4111-8111-111111111111"
EXPECTED = {"window": 1000000, "first": 35210, "call_10": 70114, "peak": 162480, "compactions": 2}


def fixture_events(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def write_events(path, events):
    path.write_text("diagnostic\n[]\n" + "\n".join(json.dumps(event) for event in events))


class ContextUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "process.log"
        self.env = {"CODEX_HOME": str(self.root / "codex")}
        directory = self.root / "codex" / "sessions" / "2026" / "11" / "01"
        directory.mkdir(parents=True)
        self.session = directory / f"rollout-date-{THREAD}.jsonl"

    def collect(self, cli, events):
        if cli == "claude":
            write_events(self.log, events)
        else:
            write_events(self.log, [{"type": "thread.started", "thread_id": THREAD},
                                    {"type": "turn.completed", "usage": {"input_tokens": 999, "output_tokens": 1}}])
            write_events(self.session, events)
        return collect_context(cli, self.log, self.env)

    def test_claude_fixture_deduplicates_main_calls_and_uses_main_model_window(self):
        self.assertEqual(collect_context("claude", FIXTURES / "claude-context.log", {}), {"context": EXPECTED})

    def test_fewer_than_ten_calls_use_last_call_including_one_call_and_zero_tokens(self):
        events = fixture_events("claude-context.log")
        for count in (1, 2, 9, 10, 12):
            calls = [event for event in events if event.get("type") == "assistant"
                     and event.get("parent_tool_use_id") is None][1:count + 1]
            result = self.collect("claude", calls + [events[-1]])["context"]
            self.assertEqual(result["call_10"], 70114 if count >= 10 else (35210 if count == 1 else 38000 + count * 1000))
            self.assertEqual(result["compactions"], 0)
        zero = copy.deepcopy(events[1])
        zero["message"]["usage"] = dict.fromkeys(zero["message"]["usage"], 0)
        self.assertEqual(self.collect("claude", [zero, events[-1]]),
                         {"context": {"window": 1000000, "first": 0, "call_10": 0, "peak": 0, "compactions": 0}})

    def test_incomplete_claude_context_never_suppresses_usage_or_denials(self):
        events = fixture_events("claude-context.log")
        invalid = [events[:-1], [events[-1]], [events[3], events[-1]]]
        for model_usage in (None, [], {}, {"subagent": {"contextWindow": 1000000}}, {"main": None}):
            changed = copy.deepcopy(events)
            changed[-1]["modelUsage"] = model_usage
            invalid.append(changed)
        for value in (None, True, 0, -1, "1000000", 1.5):
            changed = copy.deepcopy(events)
            changed[-1]["modelUsage"]["main"]["contextWindow"] = value
            invalid.append(changed)
        changed = copy.deepcopy(events)
        changed[4]["message"]["model"] = "another-main-model"
        invalid.append(changed)
        for key in ("id", "model", "usage"):
            changed = copy.deepcopy(events)
            del changed[1]["message"][key]
            invalid.append(changed)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            for value in (None, -1, True, "1", 1.5):
                changed = copy.deepcopy(events)
                changed[1]["message"]["usage"][key] = value
                invalid.append(changed)
        for index, changed in enumerate(invalid):
            with self.subTest(case=index):
                self.assertEqual(self.collect("claude", changed), {})
                # Context collection has no effect on either existing diagnostic collector.
                has_result = changed[-1].get("type") == "result"
                self.assertEqual(collect_usage("claude", self.log), {"usage": {"tokens": 1000}} if has_result else {})
                self.assertEqual(collect_denials("claude", self.log),
                                 {"denials": [{"tool": "Bash", "command": "example"}]} if has_result else {})

    def test_codex_fixture_skips_snapshots_and_counts_only_top_level_compactions(self):
        self.assertEqual(self.collect("codex", fixture_events("codex-context.jsonl")), {"context": EXPECTED})

    def test_codex_tenth_distinct_call_and_last_reported_window(self):
        template = fixture_events("codex-context.jsonl")[2]
        events = []
        for call in range(1, 13):
            event = copy.deepcopy(template)
            event["payload"]["info"]["total_token_usage"]["input_tokens"] = call * 100
            event["payload"]["info"]["last_token_usage"]["input_tokens"] = call * 10
            events.extend([event, copy.deepcopy(event), {"type": "event_msg", "payload": {"type": "token_count"}}])
        self.assertEqual(self.collect("codex", events),
                         {"context": {"window": 200000, "first": 10, "call_10": 100, "peak": 120, "compactions": 0}})

    def test_missing_or_invalid_codex_values_omit_context_but_keep_process_usage(self):
        events = fixture_events("codex-context.jsonl")
        invalid = [[], [events[1]], [{"type": "compacted"}]]
        no_window = copy.deepcopy(events)
        for event in no_window:
            info = event.get("payload", {}).get("info")
            if isinstance(info, dict):
                info.pop("model_context_window", None)
        invalid.append(no_window)
        for value in (None, True, 0, -1, "1000000", 1.5):
            changed = copy.deepcopy(events)
            changed[-1]["payload"]["info"]["model_context_window"] = value
            invalid.append(changed)
        for key in ("last_token_usage", "total_token_usage"):
            changed = copy.deepcopy(events)
            del changed[2]["payload"]["info"][key]
            invalid.append(changed)
        for value in (None, True, -1, "10", 1.5):
            changed = copy.deepcopy(events)
            changed[2]["payload"]["info"]["last_token_usage"]["input_tokens"] = value
            invalid.append(changed)
        for index, changed in enumerate(invalid):
            with self.subTest(case=index):
                self.assertEqual(self.collect("codex", changed), {})
                self.assertEqual(collect_usage("codex", self.log), {"usage": {"tokens": 1000}})

    def test_codex_never_uses_another_runs_session_or_process_context(self):
        events = fixture_events("codex-context.jsonl")
        self.collect("codex", events)
        unrelated = self.session.with_name("rollout-date-22222222-2222-4222-8222-222222222222.jsonl")
        self.session.rename(unrelated)
        with self.log.open("a") as stream:
            stream.write("\n" + "\n".join(json.dumps(event) for event in events))
        self.assertEqual(collect_context("codex", self.log, self.env), {})
        write_events(self.session, events)
        self.assertEqual(collect_context("codex", self.log, self.env), {"context": EXPECTED})
        self.session.with_name(f"rollout-duplicate-{THREAD}.jsonl").write_text(self.session.read_text())
        self.assertEqual(collect_context("codex", self.log, self.env), {})
        for thread in (None, "bad", "../*", True, {}):
            write_events(self.log, [{"type": "thread.started", "thread_id": thread}])
            self.assertEqual(collect_context("codex", self.log, self.env), {})
        write_events(self.log, [])
        self.assertEqual(collect_context("codex", self.log, self.env), {})

    def test_missing_files_and_command_agents_have_no_context(self):
        for cli in (None, "command", "claude", "codex"):
            self.assertEqual(collect_context(cli, self.log, self.env), {})
        write_events(self.log, [{"type": "thread.started", "thread_id": THREAD}])
        self.assertEqual(collect_context("codex", self.log, self.env), {})
        with patch.object(Path, "open", side_effect=OSError("unreadable")):
            self.assertEqual(collect_context("claude", self.log, self.env), {})

    def test_optional_context_is_ignored_when_malformed_and_preserved_on_rewrite(self):
        record = {"kind": "outcome", "agent": "implementer", "actor": "operator", "assignment": 1,
                  "created": "2026-11-01T12:00:00Z", "lease_id": 1, "status": "success",
                  "summary": "Ready", "accepted": True}
        invalid = [None, "bad", [], {}, EXPECTED | {"window": 0}, EXPECTED | {"peak": 1}]
        for key in EXPECTED:
            invalid.append({name: value for name, value in EXPECTED.items() if name != key})
            for value in (None, True, -1, "1", 1.5):
                invalid.append(EXPECTED | {key: value})
        for value in invalid + [EXPECTED | {"future_detail": "preserved"}]:
            with self.subTest(value=value):
                written = body(record | {"context": value})
                self.assertTrue(written.startswith(MARKER))
                parsed = records([{"body": written, "id": 2, "user": {"login": "operator"}}])[0]
                self.assertTrue(parsed["accepted"])
                self.assertEqual(context_fields(parsed), {"context": EXPECTED} if value not in invalid else {})
                self.assertEqual(body(payload(parsed)), written)
                self.assertEqual(written.splitlines()[1], body(record).splitlines()[1])


class ContextUsageLoopTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch("ub_agents.coordination.shutil.which", return_value="installed"))

    def run_agent(self, cli, *, context=True, report="success", handoff=None, failure=None):
        github = FakeGitHub(issue(), pr())
        role = agent(self.root, command=(), kind="issue", runtimes=(Runtime(cli, "model", "high"),))
        loop = Loop(config(self.root, role), github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: seconds("2026-11-01T12:00:00Z")
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")}))

        def execute(command, cwd, env, run_dir, *args, **kwargs):
            if cli == "claude":
                events = fixture_events("claude-context.log")
                write_events(run_dir / "process.log", events if context else [events[-1]])
            else:
                write_events(run_dir / "process.log", [{"type": "thread.started", "thread_id": THREAD},
                             {"type": "turn.completed", "usage": {"input_tokens": 999, "output_tokens": 1}}])
                if context:
                    sessions = Path(env["CODEX_HOME"]) / "sessions" / "2026" / "11" / "01"
                    sessions.mkdir(parents=True, exist_ok=True)
                    (sessions / f"rollout-date-{THREAD}.jsonl").write_text((FIXTURES / "codex-context.jsonl").read_text())
            lease = loop.coordinator.history(1)[0]
            if report:
                loop.coordinator.report(lease, report, "Agent report", handoff=handoff,
                                        outcome="done" if report == "success" else None,
                                        action="Maintainer: inspect the run." if report == "blocked" else None)
            if failure:
                raise failure
            return 0

        accept = loop.coordinator.accept
        update = github.update_comment
        edits = []

        def record_update(comment_id, text):
            edits.append((comment_id, json.loads(text.rsplit("```json\n", 1)[1].split("\n```", 1)[0])))
            return update(comment_id, text)

        def accept_with_context(lease, outcome):
            self.assertEqual(context_fields(outcome), {"context": EXPECTED} if context else {})
            # Usage and context arrive in the same update, before acceptance or the handoff copy.
            self.assertEqual(outcome["usage"], {"tokens": 1000})
            with_usage = [edit for comment_id, edit in edits if comment_id == outcome["id"] and "usage" in edit]
            self.assertTrue(with_usage)
            self.assertTrue(all(("context" in edit) == context for edit in with_usage))
            accept(lease, outcome)

        with patch("ub_agents.assignment_run.supervise", side_effect=execute), \
                patch.object(github, "update_comment", side_effect=record_update), \
                patch.object(loop.coordinator, "accept", side_effect=accept_with_context):
            if isinstance(failure, LostOwnership):
                with self.assertRaises(LostOwnership):
                    loop.tick()
            else:
                self.assertTrue(loop.tick())
        return loop, github, loop.coordinator.history(1)

    def test_both_runtime_outcomes_and_handoffs_keep_context_off_leases(self):
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                loop, _, (lease, outcome) = self.run_agent(cli, handoff=2)
                copy = loop.coordinator.history(2)[0]
                self.assertNotIn("context", lease)
                self.assertEqual(lease["usage"], {"tokens": 1000})
                for record in (outcome, copy):
                    self.assertEqual(record["context"], EXPECTED)
                    self.assertTrue(record["accepted"])

    def test_context_changes_no_verdict_attempt_transition_or_limit(self):
        for report in (None, "success", "retry", "blocked"):
            results = []
            for context in (False, True):
                loop, github, (lease, outcome) = self.run_agent("claude", context=context, report=report)
                self.assertNotIn("context", lease)
                self.assertEqual(context_fields(outcome), {"context": EXPECTED} if context else {})
                self.assertEqual(outcome["usage"], {"tokens": 1000})
                self.assertEqual(outcome["denials"], [{"tool": "Bash", "command": "example"}])
                results.append((lease["result"], lease["attempt_effect"], outcome["accepted"],
                                len(attempts(loop.coordinator.history(1), lease["agent"], loop.coordinator.clock())),
                                [write for write in github.writes if write[0] in {"add-labels", "remove-label"}],
                                loop.usage.paused("claude")))
            self.assertEqual(results[0], results[1])

    def test_missing_codex_session_keeps_usage_and_lost_ownership_forbids_context_writes(self):
        _, _, (lease, outcome) = self.run_agent("codex", context=False)
        self.assertNotIn("context", outcome)
        self.assertEqual(outcome["usage"], {"tokens": 1000})
        self.assertNotIn("context", lease)
        _, _, history = self.run_agent("claude", failure=LostOwnership("lost"))
        self.assertTrue(all("context" not in record for record in history))

    def test_timeout_retains_available_context_without_accepting_early_report(self):
        _, _, (lease, outcome) = self.run_agent("claude", failure=RetryableExecutionError("timed out"))
        self.assertEqual(outcome["context"], EXPECTED)
        self.assertFalse(outcome["accepted"])
        self.assertEqual(lease["result"], "retry")
        self.assertNotIn("context", lease)

    def test_stopped_execution_keeps_available_context_on_early_outcome_only(self):
        _, _, (lease, outcome) = self.run_agent("claude", failure=ExecutionStopped("Item closed", item_closed=True))
        self.assertEqual(outcome["context"], EXPECTED)
        self.assertEqual(outcome["usage"], {"tokens": 1000})
        self.assertFalse(outcome["accepted"])
        self.assertEqual(lease["result"], "blocked")
        self.assertNotIn("context", lease)
