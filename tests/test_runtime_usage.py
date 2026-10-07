from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from ub_agents.cli import main
from ub_agents.config import Runtime
from ub_agents.execution import supervise
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.loop import Loop, _GracefulStop
from ub_agents.records import attempts, iso, seconds
from ub_agents.runtime_usage import MAX_RESET_SECONDS, RuntimeUsage
from ub_agents.usage_output import UsageOutput
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


def claude(reset, used=1, status="rejected", window="five_hour"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": reset, "rateLimitType": window,
        "unifiedWindows": {window: {"utilization": used, "resetsAt": reset}}}}


def codex(reset, used=100, reached="rate_limit_reached"):
    return {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": reset},
        "rate_limit_reached_type": reached}}


class RuntimeUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = seconds("2026-10-03T12:00:00Z")
        self.start = self.now
        self.lines = []
        self.usage = RuntimeUsage(lambda: self.now, self.lines.append)

    def test_reset_margin_expiry_and_seven_day_boundary(self):
        for reset in (self.start + 100, self.start + MAX_RESET_SECONDS):
            with self.subTest(reset=reset):
                self.now = self.start
                self.usage.limit("claude", reset)
                self.assertEqual(self.usage.paused("claude")["ends_at"], iso(reset + 60))
                self.now = reset + 59
                self.assertTrue(self.usage.paused("claude"))
                self.now += 1
                self.assertIsNone(self.usage.paused("claude"))

    def test_unusable_resets_fall_back_fifteen_minutes_from_each_report(self):
        for reset in (None, "bad", float("nan"), float("inf"), True,
                      self.start, self.start - 1, self.start + MAX_RESET_SECONDS + 1):
            with self.subTest(reset=reset):
                self.now = self.start
                self.usage.reset()
                self.usage.limit("codex", reset)
                self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.start + 900))
                self.now += 100
                later_reset = self.now + MAX_RESET_SECONDS + 1 if reset == self.start + MAX_RESET_SECONDS + 1 else reset
                self.usage.limit("codex", later_reset)
                self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 900))
                self.now += 900
                self.assertIsNone(self.usage.paused("codex"))

    def test_later_report_replaces_deadline_and_logs_only_start_or_change(self):
        self.usage.limit("claude", self.now + 300)
        self.usage.limit("claude", self.now + 300)
        self.assertEqual(self.lines, [
            f"claude usage limit reached; pausing claude runs until {iso(self.start + 360)}"])
        self.now += 10
        self.usage.limit("claude", self.now + 100)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 170))
        self.usage.limit("claude", None)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 910))
        self.usage.limit("claude", self.now + 1200)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 1270))
        self.assertEqual(len(self.lines), 4)
        self.assertIn(iso(self.start + 1270), self.lines[-1])

    def test_other_cli_and_launcher_are_independent_and_restart_clears_pauses(self):
        self.usage.limit("claude", self.now + 100)
        self.assertIsNone(self.usage.paused("codex"))
        other = RuntimeUsage(lambda: self.now, self.lines.append)
        self.assertIsNone(other.paused("claude"))
        other.limit("codex", self.now + 200)
        self.usage.reset()
        self.assertIsNone(self.usage.paused("claude"))
        self.assertTrue(other.paused("codex"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wait_uses_earliest_cli_and_discards_expired_pauses(self):
        self.assertEqual(self.usage.bound_wait(5000), 5000)
        self.usage.limit("claude", self.now + 100)
        self.usage.limit("codex", self.now + 200)
        self.assertEqual(self.usage.bound_wait(50), 50)
        self.assertEqual(self.usage.bound_wait(5000), 160)
        self.now += 160
        self.assertEqual(self.usage.bound_wait(5000), 100)
        self.now += 100
        self.assertEqual(self.usage.bound_wait(5000), 5000)


class UsageOutputTests(unittest.TestCase):
    def setUp(self):
        RuntimeUsageTests.setUp(self)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.log = self.run_dir / "process.log"

    def test_claude_rejection_and_each_error_form(self):
        for event in (claude(self.now + 300),
                {"type": "rate_limit_event", "rate_limit_info": {
                    "status": "rejected", "resetsAt": self.now + 300}},
                {"type": "assistant", "error": "rate_limit"},
                {"type": "error", "error": "rate_limit"},
                {"type": "result", "api_error_status": 429}):
            with self.subTest(event=event):
                self.usage.reset()
                output = UsageOutput("claude", self.run_dir, self.usage, {})
                output.event(event)
                self.assertTrue(output.reached)
                end = self.now + (360 if event["type"] == "rate_limit_event" else 900)
                self.assertEqual(self.usage.paused("claude")["ends_at"], iso(end))

    def test_claude_rejected_window_reset_and_warning_reset_before_error(self):
        for status in ("rejected", "allowed_warning"):
            with self.subTest(status=status):
                event = claude(self.now + 100, status=status)
                del event["rate_limit_info"]["resetsAt"]
                output = UsageOutput("claude", self.run_dir, self.usage, {})
                output.event(event)
                if status != "rejected":
                    self.assertFalse(output.reached)
                    output.event({"type": "assistant", "error": "rate_limit"})
                self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.now + 160))

    def test_percentages_never_pause_either_cli(self):
        for cli, make in (("claude", claude), ("codex", codex)):
            with self.subTest(cli=cli):
                output = UsageOutput(cli, self.run_dir, self.usage, {})
                for used in (89.9, 90, 100, 1000):
                    event = (make(self.now + 100, used, "allowed_warning") if cli == "claude"
                             else make(self.now + 100, used, None))
                    output.event(event)
                self.assertIsNone(self.usage.paused(cli))
                self.assertFalse(output.reached)
        self.assertEqual(self.lines, [])

    def test_codex_structured_error_uses_reset_only(self):
        output = UsageOutput("codex", self.run_dir, self.usage, {})
        snapshot = codex(self.now + 100, reached=None)
        del snapshot["rate_limits"]["primary"]["used_percent"]
        del snapshot["rate_limits"]["primary"]["window_minutes"]
        output.event(snapshot)
        self.assertIsNone(self.usage.paused("codex"))
        output.event({"type": "error", "codex_error_info": "usage_limit_exceeded"})
        self.assertTrue(output.reached)
        self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 160))
        output.event({"type": "error", "codex_error_info": "usage_limit_exceeded",
                      "resets_at": self.now + 200})
        self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 260))

    def test_codex_limit_snapshot_uses_latest_reset_without_percentages(self):
        event = {"type": "token_count", "rate_limits": {
            "primary": {"resets_at": self.now + 100},
            "rate_limit_reached_type": "rate_limit_reached"}}
        event["rate_limits"]["secondary"] = {"used_percent": 0, "resets_at": self.now + 200}
        output = UsageOutput("codex", self.run_dir, self.usage, {})
        output.event(event)
        self.assertTrue(output.reached)
        self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 260))

    def test_incremental_partial_json_and_unrelated_tool_output(self):
        output = UsageOutput("claude", self.run_dir, self.usage, {})
        event = json.dumps(claude(self.now + 100))
        self.log.write_text('not json\n[]\n{"type":[]}\n{"type":"user","content":"rate_limit"}\n' + event[:40])
        output.poll()
        self.assertIsNone(self.usage.paused("claude"))
        with self.log.open("a") as stream:
            stream.write(event[40:])
        output.poll()
        self.assertFalse(output.reached)
        output.poll(final=True)
        self.assertTrue(output.reached)
        output.poll(final=True)
        self.assertEqual(len(self.lines), 1)

    def codex_output(self):
        thread = str(uuid.uuid4())
        sessions = self.root / "codex" / "sessions" / "2026" / "10" / "03"
        sessions.mkdir(parents=True)
        self.log.write_text(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
        output = UsageOutput("codex", self.run_dir, self.usage, {"CODEX_HOME": str(self.root / "codex")})
        return output, sessions / f"rollout-date-{thread}.jsonl"

    def test_codex_reads_only_fresh_session_after_stream_without_limit_finishes(self):
        output, session = self.codex_output()
        unrelated = session.with_name(f"rollout-date-{uuid.uuid4()}.jsonl")
        record = json.dumps({"type": "event_msg", "payload": codex(self.now + 100)}) + "\n"
        unrelated.write_text(record)
        output.poll(final=True)
        self.assertFalse(output.reached)
        session.write_text(record)
        output.poll()
        self.assertFalse(output.reached)
        output.poll(final=True)
        self.assertTrue(output.reached)
        self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 160))

    def test_codex_never_reads_session_when_json_stream_has_limit(self):
        for event in (codex(self.now + 100),
                      {"type": "error", "codex_error_info": "usage_limit_exceeded"}):
            with self.subTest(event=event):
                output = UsageOutput("codex", self.run_dir, self.usage, {})
                output.thread = str(uuid.uuid4())
                self.log.write_text(json.dumps(event) + "\n")
                with patch.object(Path, "glob", side_effect=AssertionError("session lookup")):
                    output.poll()
                    output.poll(final=True)
                self.assertTrue(output.reached)

    def test_supervision_observes_limit_without_ending_active_process(self):
        output = UsageOutput("claude", self.run_dir, self.usage, {})
        script = f"import time; print({json.dumps(claude(self.now + 100))!r}, flush=True); time.sleep(.4); print('finished')"
        seen = []

        def observe(final=False):
            output.poll(final)
            if self.usage.paused("claude"):
                seen.append(final)

        self.assertEqual(supervise([sys.executable, "-c", script], self.root, os.environ.copy(),
                                  self.run_dir, 3, threading.Event(), observe_output=observe), 0)
        self.assertIn(False, seen)
        self.assertIn("finished", self.log.read_text())


class UsageLoopTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = seconds("2026-10-03T12:00:00Z")
        self.start = self.now
        self.lines = []
        self.role = agent(self.root, command=(), kind="issue", backoff_seconds=60,
                          max_backoff_seconds=3600, runtimes=(Runtime("claude", "opus", "high"),))
        self.github = FakeGitHub(issue())
        self.loop = Loop(config(self.root, self.role), self.github, "operator", output=self.lines.append,
                         stop_event=threading.Event(), interrupt_event=threading.Event())
        self.loop.coordinator.clock = lambda: self.now
        self.enterContext(patch("ub_agents.coordination.shutil.which", return_value="installed"))

    def runtime(self, event, report=None):
        def run(command, cwd, env, run_dir, *args, **kwargs):
            (run_dir / "process.log").write_text(json.dumps(event) + "\n")
            kwargs["observe_output"]()
            if report:
                lease = next(r for r in reversed(self.loop.coordinator.history(1)) if r["kind"] == "lease")
                self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
            return 1
        return run

    def test_each_cli_limit_retries_without_attempt_or_backoff_until_margin(self):
        for cli, make in (("claude", claude), ("codex", codex)):
            with self.subTest(cli=cli):
                self.role = replace(self.role, runtimes=(Runtime(cli, "model", "high"),))
                self.loop.config = config(self.root, self.role)
                self.github.store.clear()
                self.loop.usage.reset()
                self.now = self.start
                with patch("ub_agents.loop.supervise", side_effect=self.runtime(make(self.now + 300))) as run:
                    self.assertTrue(self.loop.tick())
                    history = self.loop.coordinator.history(1)
                    lease = history[0]
                    self.assertEqual((lease["result"], lease["attempt_effect"], lease.get("retry_after")),
                                     ("retry", "unchanged", None))
                    self.assertEqual(lease["summary"], f"{cli} usage limit reached; resets {iso(self.start + 300)}")
                    self.assertEqual(attempts(history, self.role.name, self.now), [])
                    writes = list(self.github.writes)
                    self.now += 359
                    self.assertFalse(self.loop.tick())
                    self.assertEqual(self.loop.plans()[0].state, "waiting")
                    self.assertEqual(self.github.writes, writes)
                    run.assert_called_once()
                    self.now += 1
                    self.assertTrue(self.loop.tick())
                    self.assertEqual(run.call_count, 2)

    def test_untrusted_limit_allows_one_probe_then_uses_new_reset(self):
        resets = iter([None, self.start + 1200])
        calls = []

        def run(*args, **kwargs):
            calls.append(self.now)
            return self.runtime(claude(next(resets)))(*args, **kwargs)

        with patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
            self.now += 899
            self.assertFalse(self.loop.tick())
            self.now += 1
            self.assertTrue(self.loop.tick())
            self.now = self.start + 1259
            self.assertFalse(self.loop.tick())
        self.assertEqual(calls, [self.start, self.start + 900])
        self.assertEqual(self.loop.usage.paused("claude")["ends_at"], iso(self.start + 1260))
        self.assertEqual(attempts(self.loop.coordinator.history(1), self.role.name, self.now), [])

    def test_limit_preserves_existing_failure_count_and_backoff(self):
        with patch("ub_agents.loop.supervise", return_value=1):
            self.loop.tick()
        earlier = self.loop.coordinator.history(1)[0]
        self.assertEqual(earlier["retry_after"], iso(self.start + 60))
        self.now += 60
        with patch("ub_agents.loop.supervise", side_effect=self.runtime(claude(self.now + 100))):
            self.loop.tick()
        history = self.loop.coordinator.history(1)
        self.assertEqual(history[0]["retry_after"], earlier["retry_after"])
        self.assertEqual(len(attempts(history, self.role.name, self.now)), 1)
        self.assertEqual((self.loop.plans()[0].state, self.loop.plans()[0].attempt), ("waiting", 2))
        self.assertIsNone(next(r for r in reversed(history) if r["kind"] == "lease").get("retry_after"))

    def test_fallback_starts_at_report_without_being_extended_at_completion(self):
        def run(*args, **kwargs):
            code = self.runtime(claude(None))(*args, **kwargs)
            self.now += 10
            return code

        with patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.loop.usage.paused("claude")["ends_at"], iso(self.start + 900))
        self.assertEqual(len([line for line in self.lines if "pausing claude" in line]), 1)

    def test_warning_does_not_pause_after_accepted_run_or_change_outcome(self):
        with patch("ub_agents.loop.supervise", side_effect=self.runtime(
                claude(self.now + 300, .9, "allowed_warning"), report=True)):
            self.loop.tick()
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])
        self.assertIsNone(self.loop.usage.paused("claude"))

    def test_accepted_outcome_survives_rejected_event_without_utilization(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": self.now + 300}}
        with patch("ub_agents.loop.supervise", side_effect=self.runtime(event, report=True)):
            self.loop.tick()
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "success")
        self.assertEqual(self.loop.usage.paused("claude")["ends_at"], iso(self.now + 360))

    def test_alternatives_other_agents_and_waiting_items_leave_labels_alone(self):
        alternative = Runtime("codex", "other", "high")
        roles = (replace(self.role, name="only-claude", triggers=("a",)),
                 replace(self.role, name="alternatives", triggers=("b",),
                         runtimes=self.role.runtimes + (alternative,)),
                 replace(self.role, name="only-codex", triggers=("c",), runtimes=(alternative,)))
        github = FakeGitHub(issue(1, labels=("a",)), issue(2, labels=("b",)), issue(3, labels=("c",)))
        loop = Loop(config(self.root, *roles), github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        loop.usage.limit("claude", self.now + 300)
        plans = loop.plans()
        self.assertEqual([(p.state, p.runtime.cli if p.runtime else None) for p in plans],
                         [("waiting", None), ("ready", "codex"), ("ready", "codex")])
        with patch("ub_agents.loop.supervise", return_value=1) as run:
            self.assertTrue(loop.tick())
        run.assert_called_once()
        self.assertEqual(github.items[1].labels, frozenset({"a"}))
        self.assertEqual(loop.coordinator.history(1), [])
        self.assertEqual(loop.coordinator.history(2)[0]["runtime"], alternative.name)

    def test_independence_first_runtime_rule_still_applies(self):
        independent = replace(self.role, kind="pr", different_from="author",
                              runtimes=self.role.runtimes + (Runtime("codex", "other", "high"),))
        github = FakeGitHub(pr(labels=("ready",)))
        loop = Loop(config(self.root, independent), github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        loop.usage.limit("claude", self.now + 100)
        self.assertEqual(loop.plans()[0].state, "waiting")

    def test_paused_independent_runtime_cannot_fall_back_to_authors_cli(self):
        author = replace(self.role, name="author", kind="pr", runtimes=(Runtime("codex", "author-model", "high"),))
        reviewer = replace(self.role, name="reviewer", kind="pr", different_from="author",
                           runtimes=self.role.runtimes + (Runtime("codex", "other-model", "high"),))
        github = FakeGitHub(pr(labels=("ready",)))
        loop = Loop(config(self.root, author, reviewer), github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        coordinator = loop.coordinator
        plan = coordinator.plan(github.item(2), author, ())
        lease = coordinator.claim(plan)
        coordinator.update(lease, state="running", started=True)
        outcome = coordinator.report(lease, "success", "Authored", outcome="done")
        coordinator.accept(lease, outcome)
        coordinator.release(lease, "success", "Authored")
        loop.usage.limit("claude", self.now + 100)
        self.assertEqual(next(p for p in loop.plans() if p.agent.name == "reviewer").state, "waiting")

    def test_all_paused_keeps_polling_and_wakes_by_earliest_expiry(self):
        self.loop.config = replace(self.loop.config, poll_seconds=5000)
        starts, waits = [], []

        def tick():
            starts.append(self.now)
            if len(starts) == 1:
                self.loop.usage.limit("claude", self.now + 100)
                self.loop.usage.limit("codex", self.now + 200)
            elif len(starts) == 2:
                self.assertIsNone(self.loop.usage.paused("claude"))
                self.assertTrue(self.loop.usage.paused("codex"))
            else:
                self.loop.stop_event.set()
            return False

        def wait(delay):
            waits.append(delay)
            self.now += delay

        with patch.object(self.loop, "tick", side_effect=tick), \
                patch.object(self.loop.stop_event, "wait", side_effect=wait), \
                patch("ub_agents.loop.monotonic", side_effect=lambda: self.now):
            self.loop.launch()
        self.assertEqual(waits, [160, 100])
        self.assertEqual(starts, [self.start, self.start + 160, self.start + 260])
        self.assertFalse(any("Skipped GitHub poll" in line for line in self.lines))

    def test_launch_restart_clears_pauses_for_once_stop_and_errors(self):
        for ending in ("once", "stop", _GracefulStop(), KeyboardInterrupt(),
                       AgentError("poll failed"), LostOwnership("lost claim"), RuntimeError("unexpected")):
            with self.subTest(ending=ending):
                self.loop.stop_event.clear()
                self.loop.usage.limit("claude", self.now + 100)

                def tick():
                    self.assertIsNone(self.loop.usage.paused("claude"))
                    if isinstance(ending, BaseException):
                        raise ending
                    if ending == "stop":
                        self.loop.stop_event.set()
                    return True

                with patch.object(self.loop, "tick", side_effect=tick):
                    if isinstance(ending, BaseException) and not isinstance(ending, _GracefulStop):
                        with self.assertRaises(type(ending)):
                            self.loop.launch(once=True)
                    else:
                        self.loop.launch(once=ending == "once")

    def test_status_json_and_text_have_no_pauses_with_empty_or_populated_queue(self):
        self.loop.usage.limit("claude", self.now + 100)
        for github in (FakeGitHub(), FakeGitHub(issue())):
            with self.subTest(items=github.items), \
                    patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                    patch("ub_agents.cli.GitHub", return_value=github), \
                    patch("ub_agents.cli.timestamp", return_value=self.now):
                for json_output in (False, True):
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(["status"] + (["--json"] if json_output else [])), 0)
                    if json_output:
                        result = json.loads(output.getvalue())
                        self.assertEqual(set(result), {"assignments"})
                        self.assertEqual(len(result["assignments"]), len(github.items))
                        if github.items:
                            self.assertEqual(result["assignments"][0]["state"], "ready")
                    self.assertNotIn("paused", output.getvalue())
                    self.assertNotIn("pause ends", output.getvalue())
            self.assertEqual(github.writes, [])

    def test_signals_interrupt_runtime_pause_wait(self):
        from ub_agents.records import timestamp
        (self.root / "ub-agents.yaml").touch()
        settings = replace(self.loop.config, poll_seconds=5000)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                handler = signal.getsignal(sig)

                def tick(loop):
                    loop.usage.limit("claude", timestamp() + 100)
                    return False

                def wait(delay):
                    self.assertLessEqual(delay, 160)
                    signal.raise_signal(sig)

                with patch("ub_agents.cli.load_config", return_value=settings), \
                        patch("ub_agents.cli.launch_checks"), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(Loop, "tick", autospec=True, side_effect=tick), \
                        patch("threading.Event.wait", side_effect=wait), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.root / "ub-agents.yaml"), "launch"]),
                                     0 if sig == signal.SIGTERM else 130)
                self.assertEqual(signal.getsignal(sig), handler)
