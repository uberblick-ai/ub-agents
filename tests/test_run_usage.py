import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.errors import ExecutionStopped, LostOwnership, RetryableExecutionError
from ub_agents.loop import Loop
from ub_agents.records import MARKER, attempts, body, payload, records, seconds
from ub_agents.run_usage import short_tokens, usage_fields, usage_suffix
from ub_agents.usage_output import collect_usage
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


FIXTURES = Path(__file__).parent / "fixtures" / "runtime_logs"


def claude(tokens=10, **fields):
    return {"type": "result", "usage": {
        "input_tokens": tokens, "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30, "output_tokens": 40}, **fields}


def codex(tokens=100, **fields):
    return {"type": "turn.completed", "usage": {
        "input_tokens": tokens, "cached_input_tokens": 80, "output_tokens": 10}, **fields}


class RunUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "process.log"

    def collect(self, cli, *events):
        self.path.write_text("diagnostic\n[]\n" + "\n".join(json.dumps(event) for event in events))
        return collect_usage(cli, self.path)

    def test_recorded_runtime_logs(self):
        self.assertEqual(collect_usage("claude", FIXTURES / "claude.log"),
                         {"usage": {"tokens": 31833, "total_cost_usd": 0.1456374}})
        self.assertEqual(collect_usage("codex", FIXTURES / "codex.log"), {"usage": {"tokens": 109400}})
        self.assertEqual(collect_usage("codex", FIXTURES / "codex-error.log"), {})

    def test_claude_uses_last_result_and_does_not_sum_message_or_nested_usage(self):
        self.assertEqual(self.collect("claude", claude(),
                         {"type": "assistant", "usage": claude()["usage"]},
                         claude(50, total_cost_usd=1.84, modelUsage={"model": {"inputTokens": 9999}})),
                         {"usage": {"tokens": 140, "total_cost_usd": 1.84}})
        self.assertEqual(self.collect("claude", claude(), {"type": "result"}), {})

    def test_codex_sums_completed_turns_and_counts_cached_input_once(self):
        self.assertEqual(self.collect("codex", codex(), {"type": "turn.started"}, codex(200)),
                         {"usage": {"tokens": 320}})

    def test_no_completed_event_missing_file_and_command_agents_have_no_usage(self):
        self.assertEqual(collect_usage("claude", self.path), {})
        for cli in ("claude", "codex"):
            self.assertEqual(self.collect(cli, {"type": "assistant", "usage": claude()["usage"]},
                                          {"type": "turn.failed"}), {})
        self.assertEqual(self.collect(None, claude(), codex()), {})
        self.assertEqual(self.collect("command", claude(), codex()), {})

    def test_missing_or_malformed_token_components_do_not_record_partial_usage(self):
        for cli, make in (("claude", claude), ("codex", codex)):
            keys = ("input_tokens", "output_tokens") + (
                ("cache_creation_input_tokens", "cache_read_input_tokens") if cli == "claude" else ())
            for usage in (None, [], "bad", {}):
                with self.subTest(cli=cli, usage=usage):
                    self.assertEqual(self.collect(cli, make(usage=usage)), {})
            for key in keys:
                for value in (None, -1, True, "10", 1.5, float("inf"), float("nan")):
                    with self.subTest(cli=cli, key=key, value=value):
                        event = make()
                        event["usage"][key] = value
                        self.assertEqual(self.collect(cli, make(), event), {})
                event = make()
                del event["usage"][key]
                self.assertEqual(self.collect(cli, event), {})

    def test_zero_is_only_recorded_when_explicit_and_cost_is_optional(self):
        event = claude(usage=dict.fromkeys(claude()["usage"], 0), total_cost_usd=0)
        self.assertEqual(self.collect("claude", event), {"usage": {"tokens": 0, "total_cost_usd": 0}})
        self.assertEqual(self.collect("claude", claude()), {"usage": {"tokens": 100}})
        for cost in (None, True, -1, "1.84", float("inf"), float("nan"), 10 ** 400):
            self.assertEqual(self.collect("claude", claude(total_cost_usd=cost)), {})

    def test_short_counts_and_cost_formatting(self):
        for tokens, expected in ((0, "0"), (999, "999"), (1000, "1k"), (1499, "1k"),
                                 (1500, "2k"), (412000, "412k"), (999999, "1000k"),
                                 (1000000, "1.0M"), (1249999, "1.2M"), (1250000, "1.3M")):
            with self.subTest(tokens=tokens):
                self.assertEqual(short_tokens(tokens), expected)
                self.assertEqual(usage_suffix({"usage": {"tokens": tokens}}), f" · {expected} tokens")
        self.assertEqual(usage_suffix({"usage": {"tokens": 412000, "total_cost_usd": 1.84}}),
                         " · 412k tokens · $1.84")
        self.assertEqual(usage_suffix({"usage": {"tokens": 0, "total_cost_usd": 0}}), " · 0 tokens · $0.00")

    def test_optional_usage_is_ignored_when_malformed_and_record_rewrites_preserve_it(self):
        record = {"kind": "outcome", "agent": "implementer", "actor": "operator", "assignment": 1,
                  "created": "2026-10-05T12:00:00Z", "lease_id": 1, "status": "success",
                  "summary": "Ready", "accepted": True, "candidate_sha": "a" * 40}
        line = body(record).splitlines()[1]
        for value in (None, "bad", [], {}, {"tokens": True}, {"tokens": -1}, {"tokens": 1.0},
                      {"tokens": "1"}, {"tokens": 10, "total_cost_usd": "bad"}):
            with self.subTest(value=value):
                written = body(record | {"usage": value})
                self.assertEqual(written.splitlines()[1], line)
                self.assertTrue(written.startswith(MARKER))
                parsed = records([{"body": written, "id": 2, "user": {"login": "operator"}}])[0]
                self.assertEqual(usage_fields(parsed), {})
                self.assertEqual(body(payload(parsed)).splitlines()[1], line)
                if value is not None:
                    self.assertEqual(parsed["usage"], value)
        usage = {"tokens": 412000, "total_cost_usd": 1.84, "future_detail": "preserved"}
        parsed = records([{"body": body(record | {"usage": usage}), "id": 2,
                           "user": {"login": "operator"}}])[0]
        rewritten = body(payload(parsed))
        self.assertEqual(parsed["usage"], usage)
        self.assertIn('"future_detail": "preserved"', rewritten)
        self.assertIn(" aaaaaaa · 412k tokens · $1.84 — Ready", rewritten)


class RunUsageLoopTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch("ub_agents.coordination.shutil.which", return_value="installed"))

    def run_agent(self, cli="claude", *, log=None, report="success", handoff=None, failure=None):
        github = FakeGitHub(issue(), pr())
        role = agent(self.root, command=(), kind="issue", runtimes=(Runtime(cli, "model", "high"),)) \
            if cli else agent(self.root)
        loop = Loop(config(self.root, role), github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: seconds("2026-10-05T12:00:00Z")

        def execute(command, cwd, env, run_dir, *args, **kwargs):
            if log is not None:
                (run_dir / "process.log").write_text(log)
            lease = loop.coordinator.history(1)[0]
            if report:
                loop.coordinator.report(lease, report, "Agent report", handoff=handoff,
                                        outcome="done" if report == "success" else None,
                                        action="Maintainer: inspect this run." if report == "blocked" else None)
            if failure:
                raise failure
            return 0

        with patch("ub_agents.loop.supervise", side_effect=execute):
            if isinstance(failure, (KeyboardInterrupt, LostOwnership)):
                with self.assertRaises(type(failure)):
                    loop.tick()
            else:
                self.assertTrue(loop.tick())
        return loop, github, loop.coordinator.history(1)

    def test_completed_fixture_usage_reaches_lease_outcome_and_handoff_lines(self):
        for cli, tokens, cost, formatted in (("claude", 31833, 0.1456374, "32k tokens · $0.15"),
                                            ("codex", 109400, None, "109k tokens")):
            with self.subTest(cli=cli):
                loop, github, history = self.run_agent(cli, log=(FIXTURES / f"{cli}.log").read_text(), handoff=2)
                expected = {"tokens": tokens} | ({"total_cost_usd": cost} if cost is not None else {})
                lease, outcome = history
                copy = loop.coordinator.history(2)[0]
                for record in (lease, outcome, copy):
                    self.assertEqual(record["usage"], expected)
                    line = body(payload(record)).splitlines()[1]
                    self.assertIn(f" · {formatted} — Agent report", line)
                    self.assertEqual("$" in line, cost is not None)
                self.assertIn(f" · {cli}:model:high · {formatted}", body(payload(lease)))
                self.assertIn(f" aaaaaaa · {formatted}", body(payload(outcome)))
                self.assertTrue(outcome["accepted"])
                self.assertTrue(copy["accepted"])
                self.assertEqual(lease["attempt_effect"], "reset")

    def test_usage_changes_no_verdict_attempt_transition_or_limit(self):
        for report in (None, "success", "retry", "blocked"):
            results = []
            for event in ({"type": "result"}, claude(total_cost_usd=1.84)):
                loop, github, history = self.run_agent(log=json.dumps(event), report=report)
                lease, outcome = history
                results.append((lease["result"], lease["attempt_effect"], outcome["accepted"],
                                len(attempts(history, lease["agent"], loop.coordinator.clock())),
                                [write for write in github.writes if write[0] in {"add-labels", "remove-label"}],
                                loop.usage.paused("claude")))
                self.assertEqual("usage" in lease, "usage" in event)
                self.assertEqual("usage" in outcome, "usage" in event)
            self.assertEqual(results[0], results[1])

    def test_missing_final_usage_and_command_agents_record_no_field(self):
        for cli, log, failure in (("claude", None, None), ("codex", "not json", None),
                ("claude", json.dumps(claude(usage={})), None),
                (None, json.dumps(claude()), None),
                ("claude", None, RetryableExecutionError("timed out")),
                ("codex", None, ExecutionStopped("Item closed")),
                ("claude", None, KeyboardInterrupt()), ("codex", None, LostOwnership("lost"))):
            with self.subTest(cli=cli, failure=type(failure).__name__):
                _, _, history = self.run_agent(cli, log=log, failure=failure)
                self.assertTrue(all("usage" not in record for record in history))
                self.assertTrue(all("tokens" not in body(payload(record)).splitlines()[1] for record in history))
