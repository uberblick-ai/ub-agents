from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
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
from ub_agents.runtime_usage import RuntimeUsage, local_pauses, process_started
from ub_agents.usage_output import UsageOutput
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


def claude(reset, used=1, status="rejected", window="five_hour"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": reset, "rateLimitType": window,
        "unifiedWindows": {window: {"utilization": used, "resetsAt": reset}}}}


def codex(reset, used=100, reached="rate_limit_reached"):
    return {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": reset},
        "secondary": {"used_percent": 48, "window_minutes": 10080, "resets_at": reset + 10000},
        "rate_limit_reached_type": reached}}


class RuntimeUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = seconds("2026-10-03T12:00:00Z")
        self.start = self.now
        self.lines = []
        self.usage = RuntimeUsage(self.root, lambda: self.now, self.lines.append)

    def test_window_margin_latest_readings_and_clock_only_expiry(self):
        self.usage.record("claude", "five_hour", 89.9, self.now + 100, 18000, "allowed")
        self.assertIsNone(self.usage.paused("claude"))
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000, "allowed_warning")
        self.assertIn("five_hour usage 90%", self.lines[0])
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 160))
        self.usage.record("claude", "five_hour", 95, self.now + 100, 18000)
        self.assertEqual(self.usage.readings["claude"]["five_hour"]["used_percent"], 95)
        self.assertEqual(len(self.lines), 1)
        # In-flight readings never end an established pause early.
        self.usage.record("claude", "five_hour", 50, self.now + 100, 18000)
        self.now += 159
        self.assertTrue(self.usage.paused("claude"))
        self.now += 1
        self.assertIsNone(self.usage.paused("claude"))
        self.assertEqual(self.usage.readings["claude"], {})

    def test_all_limiting_windows_must_expire_and_wait_uses_earliest_cli(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        self.usage.record("claude", "seven_day", 91, self.now + 200, 604800)
        self.usage.record("codex", "primary", 90, self.now + 50, 18000)
        self.assertEqual(self.usage.bound_wait(5000), 110)
        self.now += 160
        self.assertIsNone(self.usage.paused("codex"))
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 260))
        self.assertEqual(self.usage.bound_wait(5000), 100)
        self.now += 100
        self.assertEqual(self.usage.rows(), [])

    def test_new_reset_updates_pause_without_extending_untrusted_fallback(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        self.now += 10
        self.usage.record("claude", "five_hour", 92, self.now + 200, 18000)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 270))
        self.assertEqual(self.usage.readings["claude"]["five_hour"]["reset_at"], self.start + 210)
        self.assertIn(iso(self.start + 270), self.lines[-1])
        self.usage.record("claude", "five_hour", 92, "bad", 18000)
        self.now += 10
        self.usage.record("claude", "five_hour", 92, None, 18000)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.start + 910))

    def test_untrusted_update_after_long_active_run_still_pauses(self):
        self.usage.record("claude", "five_hour", 90, self.now + 3000, 18000)
        self.now += 1000
        self.usage.record("claude", "five_hour", 95, None, 18000)
        self.assertEqual(self.usage.paused("claude")["ends_at"], iso(self.now + 900))

    def test_untrusted_resets_have_fixed_fallback_even_with_repeated_readings(self):
        for reset in (None, "bad", float("nan"), float("inf"), True,
                      self.start, self.start - 1, self.start + 18001):
            with self.subTest(reset=reset):
                self.now = self.start
                usage = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
                usage.record("codex", "primary", 90, reset, 18000)
                self.now += 100
                usage.record("codex", "primary", 95, reset, 18000)
                self.assertEqual(usage.paused("codex")["ends_at"], iso(self.start + 900))
                self.now = self.start + 900
                self.assertIsNone(usage.paused("codex"))
                usage.limit("codex", ("primary", self.now + 300, 18000))
                self.assertEqual(usage.paused("codex")["ends_at"], iso(self.start + 1260))

    def test_local_state_expiry_restart_and_other_launcher_isolation(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        self.assertEqual(self.usage.path.parent, self.root / ".ub-agents" / "runtime-usage")
        other = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
        other.record("codex", "primary", 90, self.now + 200, 18000)
        before = self.usage.path.read_bytes()
        self.assertEqual({row["cli"] for row in local_pauses(self.root, self.now)}, {"claude", "codex"})
        self.assertEqual(self.usage.path.read_bytes(), before)
        restarted = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
        self.assertEqual(restarted.rows(), [])
        self.usage.reset()
        self.assertEqual([row["cli"] for row in local_pauses(self.root, self.now)], ["codex"])
        self.now += 260
        self.assertEqual(local_pauses(self.root, self.now), [])

    def test_state_ignores_corruption_other_hosts_and_stopped_launchers(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        good = json.loads(self.usage.path.read_text())
        for change in ({"host": "another-host"}, {"pid": -1}, {"pauses": []}, {"version": 2}):
            self.usage.path.write_text(json.dumps(good | change))
            self.assertEqual(local_pauses(self.root, self.now), [])
        self.usage.path.write_text(json.dumps(good))
        with patch("ub_agents.runtime_usage.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(local_pauses(self.root, self.now), [])
        self.assertEqual(json.loads(self.usage.path.read_text()), good)
        self.usage.path.write_text("broken json")
        self.assertEqual(local_pauses(self.root, self.now), [])

    def test_close_removes_own_state_and_temporary_file_only(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        other = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
        other.record("codex", "primary", 90, self.now + 200, 18000)
        before = other.path.read_bytes()
        temporary = self.usage.path.with_suffix(".tmp")
        temporary.write_text("incomplete write")
        self.usage.close()
        self.usage.close()
        self.assertFalse(self.usage.path.exists())
        self.assertFalse(temporary.exists())
        self.assertEqual(other.path.read_bytes(), before)
        self.assertEqual([row["cli"] for row in local_pauses(self.root, self.now)], ["codex"])

    def test_startup_prunes_dead_launchers_but_preserves_live_and_foreign_state(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        good = json.loads(self.usage.path.read_text())
        dead_pid = os.getpid() + 1000000
        dead = self.usage.path.with_name(f"{uuid.uuid4().hex}.json")
        dead.write_text(json.dumps(good | {"launcher": dead.stem, "pid": dead_pid}))
        legacy = self.usage.path.with_name(f"{uuid.uuid4().hex}.json")
        legacy_state = good | {"launcher": legacy.stem, "pid": dead_pid}
        legacy_state.pop("process_started")
        legacy.write_text(json.dumps(legacy_state))
        foreign = self.usage.path.with_name(f"{uuid.uuid4().hex}.json")
        foreign.write_text(json.dumps(good | {"launcher": foreign.stem, "pid": dead_pid,
                                              "host": "another-host"}))
        before = self.usage.path.read_bytes(), foreign.read_bytes()

        def probe(pid, sig):
            self.assertEqual(sig, 0)
            if pid == dead_pid:
                raise ProcessLookupError

        with patch("ub_agents.runtime_usage.os.kill", side_effect=probe):
            RuntimeUsage(self.root, lambda: self.now, self.lines.append).reset()
        self.assertFalse(dead.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual((self.usage.path.read_bytes(), foreign.read_bytes()), before)

    def test_recycled_pid_is_ignored_read_only_and_pruned_on_startup(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        before = self.usage.path.read_bytes()
        with patch("ub_agents.runtime_usage.process_started", return_value="a later process"):
            self.assertEqual(local_pauses(self.root, self.now), [])
            self.assertEqual(self.usage.path.read_bytes(), before)
            RuntimeUsage(self.root, lambda: self.now, self.lines.append).reset()
        self.assertFalse(self.usage.path.exists())

    def test_unavailable_inspection_never_prunes_potentially_live_state(self):
        self.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        before = self.usage.path.read_bytes()
        for target, value in (("process_started", {"return_value": None}),
                              ("os.kill", {"side_effect": PermissionError})):
            with self.subTest(target=target), patch(f"ub_agents.runtime_usage.{target}", **value):
                self.assertEqual(local_pauses(self.root, self.now), [])
                RuntimeUsage(self.root, lambda: self.now, self.lines.append).reset()
                self.assertEqual(self.usage.path.read_bytes(), before)

    def test_process_start_probe_distinguishes_exit_from_inspection_failure(self):
        for code, output, error, expected in (
                (0, "S+ Sat Oct  3 12:00:00 2026\n", "", "Sat Oct 3 12:00:00 2026"),
                (0, "Z Sat Oct  3 12:00:00 2026\n", "", ""),
                (1, "", "", ""), (1, "", "Operation not permitted", None),
                (0, "malformed", "", None)):
            with self.subTest(code=code, output=output, error=error), \
                    patch("ub_agents.runtime_usage.subprocess.run", return_value=
                          subprocess.CompletedProcess([], code, output, error)) as probe:
                self.assertEqual(process_started(123), expected)
                self.assertEqual(probe.call_args.args[0], ["ps", "-p", "123", "-o", "stat=,lstart="])
                self.assertEqual(probe.call_args.kwargs["env"]["LC_ALL"], "C")
                self.assertEqual(probe.call_args.kwargs["env"]["TZ"], "UTC")
        for error in (OSError("not permitted"), subprocess.TimeoutExpired("ps", 5)):
            with self.subTest(error=error), patch("ub_agents.runtime_usage.subprocess.run", side_effect=error):
                self.assertIsNone(process_started(123))


class UsageOutputTests(unittest.TestCase):
    def setUp(self):
        RuntimeUsageTests.setUp(self)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.log = self.run_dir / "process.log"

    def test_claude_rejection_without_windows_and_each_error_form(self):
        for event in ({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": self.now + 300}},
                {"type": "assistant", "error": "rate_limit"},
                {"type": "result", "api_error_status": 429}):
            output = UsageOutput("claude", self.run_dir, self.usage, {})
            output.event(event)
            self.assertTrue(output.reached)
        self.assertFalse(UsageOutput("claude", self.run_dir, self.usage, {}).reached)

    def test_codex_exact_warning_threshold_and_structured_error_with_last_reset(self):
        output = UsageOutput("codex", self.run_dir, self.usage, {})
        output.event(codex(self.now + 100, 89.9, None))
        self.assertIsNone(self.usage.paused("codex"))
        output.event(codex(self.now + 100, 90, None))
        self.assertTrue(self.usage.paused("codex"))
        self.assertFalse(output.reached)
        output.event({"type": "error", "codex_error_info": "usage_limit_exceeded"})
        self.assertTrue(output.reached)
        self.assertEqual(output.hint, ("primary", self.now + 100, 18000))

    def test_incremental_partial_json_and_unrelated_tool_output(self):
        output = UsageOutput("claude", self.run_dir, self.usage, {})
        event = json.dumps(claude(self.now + 100, .9, "allowed_warning"))
        self.log.write_text('not json\n[]\n{"type":[]}\n{"type":"user","content":"rate_limit"}\n' + event[:40])
        output.poll()
        self.assertEqual(self.usage.rows(), [])
        with self.log.open("a") as stream:
            stream.write(event[40:] + "\n")
        output.poll()
        self.assertTrue(self.usage.paused("claude"))
        output.poll()
        self.assertEqual(len(self.lines), 1)
        self.assertFalse(output.reached)

    def test_codex_reads_only_the_identified_fresh_session(self):
        thread, unrelated = str(uuid.uuid4()), str(uuid.uuid4())
        sessions = self.root / "codex" / "sessions" / "2026" / "10" / "03"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-date-{unrelated}.jsonl").write_text(json.dumps(
            {"type": "event_msg", "payload": codex(self.now + 100)}) + "\n")
        output = UsageOutput("codex", self.run_dir, self.usage, {"CODEX_HOME": str(self.root / "codex")})
        self.log.write_text(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
        output.poll()
        self.assertFalse(output.reached)
        session = sessions / f"rollout-date-{thread}.jsonl"
        session.write_text(json.dumps({"type": "event_msg", "payload": codex(self.now + 100)}) + "\n")
        output.poll()
        self.assertTrue(output.reached)
        self.assertEqual(output.hint, ("primary", self.now + 100, 18000))
        self.assertEqual(self.usage.paused("codex")["ends_at"], iso(self.now + 160))

    def test_supervision_observes_warning_without_ending_active_process(self):
        output = UsageOutput("claude", self.run_dir, self.usage, {})
        script = f"import time; print({json.dumps(claude(self.now + 100, .9, 'allowed_warning'))!r}, flush=True); time.sleep(.4); print('finished')"
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

    def test_warning_pauses_after_accepted_run_and_does_not_change_outcome(self):
        with patch("ub_agents.loop.supervise", side_effect=self.runtime(
                claude(self.now + 300, .9, "allowed_warning"), report=True)):
            self.loop.tick()
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])
        self.assertTrue(self.loop.usage.paused("claude"))

    def test_accepted_outcome_survives_rejected_event_without_utilization(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": self.now + 300}}
        with patch("ub_agents.loop.supervise", side_effect=self.runtime(event, report=True)):
            self.loop.tick()
        self.assertEqual(self.loop.coordinator.history(1)[0]["result"], "success")
        self.assertIsNone(self.loop.usage.paused("claude"))

    def test_alternatives_other_agents_and_waiting_items_leave_labels_alone(self):
        alternative = Runtime("codex", "other", "high")
        roles = (replace(self.role, name="only-claude", triggers=("a",)),
                 replace(self.role, name="alternatives", triggers=("b",),
                         runtimes=self.role.runtimes + (alternative,)),
                 replace(self.role, name="only-codex", triggers=("c",), runtimes=(alternative,)))
        github = FakeGitHub(issue(1, labels=("a",)), issue(2, labels=("b",)), issue(3, labels=("c",)))
        loop = Loop(config(self.root, *roles), github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        loop.usage.record("claude", "five_hour", 90, self.now + 300, 18000)
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
        loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
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
        loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        self.assertEqual(next(p for p in loop.plans() if p.agent.name == "reviewer").state, "waiting")

    def test_all_paused_keeps_polling_and_wakes_by_earliest_expiry(self):
        self.loop.config = replace(self.loop.config, poll_seconds=5000)
        starts, waits = [], []

        def tick():
            starts.append(self.now)
            if len(starts) == 1:
                self.loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
                self.loop.usage.record("codex", "primary", 90, self.now + 200, 18000)
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

    def test_launch_exit_removes_state_for_once_stop_and_errors(self):
        other = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
        other.record("codex", "primary", 90, self.now + 200, 18000)
        before = other.path.read_bytes()
        for ending in ("once", "stop", _GracefulStop(), KeyboardInterrupt(),
                       AgentError("poll failed"), LostOwnership("lost claim"), RuntimeError("unexpected")):
            with self.subTest(ending=ending):
                self.loop.stop_event.clear()

                def tick():
                    self.loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
                    self.assertTrue(self.loop.usage.path.exists())
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
                self.assertEqual(list(self.loop.usage.path.parent.glob("*.json")), [other.path])
                self.assertEqual(other.path.read_bytes(), before)

    def test_status_json_and_text_show_pauses_even_with_empty_queue(self):
        self.loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        github = FakeGitHub()
        before = self.loop.usage.path.read_bytes()
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=github), \
                patch("ub_agents.cli.timestamp", return_value=self.now):
            for json_output in (False, True):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["status"] + (["--json"] if json_output else [])), 0)
                if json_output:
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["assignments"], [])
                    self.assertEqual(result["runtime_pauses"][0]["ends_at"], iso(self.now + 160))
                else:
                    self.assertIn(f"claude paused: five_hour usage 90%; pause ends {iso(self.now + 160)}", output.getvalue())
        self.assertEqual(self.loop.usage.path.read_bytes(), before)
        self.assertEqual(github.writes, [])

    def test_signals_interrupt_runtime_pause_wait(self):
        from ub_agents.records import timestamp
        settings = replace(self.loop.config, poll_seconds=5000)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                handler = signal.getsignal(sig)

                def tick(loop):
                    loop.usage.record("claude", "five_hour", 90, timestamp() + 100, 18000)
                    return False

                def wait(delay):
                    self.assertLessEqual(delay, 160)
                    signal.raise_signal(sig)

                with patch("ub_agents.cli.load_config", return_value=settings), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch.object(Loop, "tick", autospec=True, side_effect=tick), \
                        patch("threading.Event.wait", side_effect=wait), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.root / "ub-agents.yaml"), "launch"]),
                                     0 if sig == signal.SIGTERM else 130)
                self.assertEqual(signal.getsignal(sig), handler)
                self.assertEqual(list((self.root / ".ub-agents" / "runtime-usage").glob("*.json")), [])
