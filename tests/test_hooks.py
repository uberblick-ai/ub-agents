from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.config import CleanupHook, load_config
from ub_agents.errors import AgentError, CleanupError, LostOwnership
from ub_agents.execution import Workspace, group_members, supervise
from ub_agents.hooks import run_hook
from ub_agents.loop import Loop
from ub_agents.records import iso, timestamp
from tests.support import stub_refresh, FakeGitHub, agent, config, issue, pr


class HookTests(unittest.TestCase):
    def setUp(self):
        self.refresh = stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.agent = agent(self.root, worktree=True, kind="issue")
        self.github = FakeGitHub(issue(), pr(labels=("needs-review",)))
        self.removed = []

    def loop(self, script="pass", timeout=3):
        cfg = replace(config(self.root, self.agent), cleanup=CleanupHook((sys.executable, "-c", script), timeout))
        self.current = Loop(cfg, self.github, "operator", output=lambda *_: None,
                            interrupt_event=threading.Event())
        return self.current

    def prepare(self, workspace):
        workspace.private.mkdir(parents=True)
        workspace.created = True
        workspace.lease["branch"] = "ub-agents/worker/1/" + workspace.lease["run"]
        self.workspace = workspace
        return workspace.private

    def execute(self, loop, effect=None):
        def agent_execution(*args, **kwargs):
            lease = loop.coordinator.history(1)[0]
            if effect:
                return effect(lease)
            loop.coordinator.report(lease, "success", "done", handoff=2, outcome="done")
            return 0
        def remove(*args):
            self.removed.append(args)
            return ""
        with patch.object(Workspace, "prepare", lambda workspace: self.prepare(workspace)), \
                patch("ub_agents.execution.git", side_effect=remove), \
                patch("ub_agents.loop.supervise", side_effect=agent_execution):
            return loop.tick()

    def test_hook_success_has_reported_handoff_context_and_removes_tree(self):
        output = self.root / "context.json"
        loop = self.loop("import os; from pathlib import Path; "
                         f"Path({str(output)!r}).write_text(Path(os.environ['UB_AGENTS_CLEANUP_CONTEXT']).read_text())")
        self.assertTrue(self.execute(loop))
        self.assertEqual(len(self.removed), 1)
        value = json.loads(output.read_text())
        self.assertEqual((value["status"], value["handoff"], value["kind"]), ("success", 2, "issue"))
        self.assertEqual(value["worktree"], str(self.workspace.private))

    def test_cleanup_hook_inherits_gh_run_lock(self):
        loop = self.loop()
        marker = self.root / "inherited"
        def hook(command, *args, **kwargs):
            descriptors = kwargs["pass_fds"]
            self.assertEqual(len(descriptors), 1)
            script = (f"import os; from pathlib import Path; os.fstat({descriptors[0]}); "
                      f"Path({str(marker)!r}).touch()")
            return supervise([sys.executable, "-c", script], *args, **kwargs)
        with patch("ub_agents.hooks.supervise", side_effect=hook):
            self.assertTrue(self.execute(loop))
        self.assertTrue(marker.exists())

    def test_command_run_and_cleanup_need_no_host_gh(self):
        which = shutil.which
        with patch("shutil.which", side_effect=lambda cli: None if cli == "gh" else which(cli)):
            stub_refresh(self)
            loop = self.loop()
            self.assertIsNone(shutil.which("gh"))
            self.assertTrue(self.execute(loop))
        self.assertEqual(len(self.removed), 1)

    def test_interrupted_outcome_read_still_runs_hook_once_and_removes_tree(self):
        output = self.root / "hook-ran"
        loop = self.loop(f"from pathlib import Path; Path({str(output)!r}).touch()")
        with ExitStack() as stack:
            def report_then_interrupt(lease):
                loop.coordinator.report(lease, "success", "done", handoff=2, outcome="done")
                original = loop.coordinator.outcome
                first = True
                def read(lease):
                    nonlocal first
                    if first:
                        first = False
                        raise KeyboardInterrupt
                    return original(lease)
                stack.enter_context(patch.object(loop.coordinator, "outcome", side_effect=read))
                return 0
            with patch("ub_agents.loop.run_hook", wraps=run_hook) as hook:
                with self.assertRaises(KeyboardInterrupt):
                    self.execute(loop, report_then_interrupt)
                self.assertEqual(hook.call_count, 1)
        self.assertTrue(output.exists())
        self.assertEqual(len(self.removed), 1)
        self.assertFalse(self.workspace.created)
        self.assertTrue(loop.stop_event.is_set())
        self.assertTrue(loop.interrupt_event.is_set())

    def test_interrupt_recording_hook_failure_keeps_verdict_best_effort(self):
        for method in ("assert_owned", "update"):
            with self.subTest(method=method):
                self.github = FakeGitHub(issue(), pr(labels=("needs-review",)))
                loop = self.loop("import sys; sys.exit(4)")
                with ExitStack() as stack:
                    def report_then_interrupt(lease):
                        loop.coordinator.report(lease, "success", "done", handoff=2, outcome="done")
                        original = getattr(loop.coordinator, method)
                        first = True
                        def write(*args, **kwargs):
                            nonlocal first
                            if first:
                                first = False
                                raise KeyboardInterrupt
                            return original(*args, **kwargs)
                        stack.enter_context(patch.object(loop.coordinator, method, side_effect=write))
                        return 0
                    with self.assertRaises(KeyboardInterrupt):
                        self.execute(loop, report_then_interrupt)
                self.assertTrue(loop.stop_event.is_set())
                self.assertTrue(loop.interrupt_event.is_set())
                lease, outcome = loop.coordinator.history(1)
                self.assertTrue(outcome["accepted"])
                self.assertEqual(lease["result"], "success")
                self.assertFalse(self.removed)
                self.assertTrue(self.workspace.created)
                events = self.root / ".ub-agents" / "runs" / lease["run"] / "events.jsonl"
                self.assertIn("cleanup-hook-verdict-unrecorded", events.read_text())

    def test_interrupt_recording_uncertainty_preserves_cleanup_error(self):
        for method in ("assert_owned", "update"):
            with self.subTest(method=method):
                self.github = FakeGitHub(issue())
                loop = self.loop()
                failure = CleanupError("agent stop uncertain")
                with ExitStack() as stack:
                    def uncertain(lease):
                        stack.enter_context(patch.object(loop.coordinator, method, side_effect=KeyboardInterrupt))
                        raise failure
                    with self.assertRaises(CleanupError) as caught:
                        self.execute(loop, uncertain)
                self.assertIs(caught.exception, failure)
                self.assertTrue(loop.stop_event.is_set())
                self.assertTrue(loop.interrupt_event.is_set())
                self.assertFalse(self.removed)
                self.assertTrue(self.workspace.created)
                lease = loop.coordinator.history(1)[0]
                self.assertEqual(lease["state"], "running")
                events = self.root / ".ub-agents" / "runs" / lease["run"] / "events.jsonl"
                self.assertIn("cleanup-verdict-unrecorded", events.read_text())

    def test_failure_and_timeout_keep_tree_without_affecting_success_or_queue(self):
        for script, timeout, message in (("import sys; sys.exit(4)", 3, "exited 4"),
                                         ("import time; time.sleep(30)", .05, "timed out")):
            with self.subTest(message=message):
                self.github = FakeGitHub(issue(), pr(labels=("needs-review",)))
                self.removed.clear()
                loop = self.loop(script, timeout)
                self.assertTrue(self.execute(loop))
                lease, outcome = loop.coordinator.history(1)
                self.assertEqual(lease["result"], "success")
                self.assertTrue(outcome["accepted"])
                self.assertIn(message, lease["cleanup_hook_error"])
                self.assertNotIn("cleanup", lease)
                self.assertFalse(self.removed)
                self.assertTrue(self.workspace.created)
                run_dir = self.root / ".ub-agents" / "runs" / lease["run"]
                events = (run_dir / "events.jsonl").read_text()
                self.assertIn("cleanup-hook-failed", events)
                group = int(next((run_dir / "cleanup").glob("*/pid")).read_text())
                self.assertEqual(group_members(group), [])
                self.assertEqual(next((run_dir / "cleanup").glob("*/stopped")).read_text(), "confirmed\n")

    def test_hook_runs_under_the_lease_expiry_and_success_is_accepted(self):
        loop = self.loop()
        started = timestamp()
        seen = []

        def hook(command, cwd, env, directory, timeout, stop_event, expires=None, **kwargs):
            seen.append(expires() if callable(expires) else expires)
            self.assertIn("UB_AGENTS_CLEANUP_CONTEXT", env)
            self.assertNotIn("UB_AGENTS_LEASE_ID", env)
            return 0

        with patch("ub_agents.hooks.supervise", side_effect=hook), \
                patch.dict("os.environ", {"UB_AGENTS_LEASE_ID": "inherited"}):
            self.assertTrue(self.execute(loop))
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], started)  # the live lease's wall-clock deadline
        lease, outcome = loop.coordinator.history(1)
        self.assertEqual(lease["result"], "success")
        self.assertTrue(outcome["accepted"])
        self.assertEqual(len(self.removed), 1)

    def test_lease_expiry_during_hook_stops_real_group_and_retains_artifacts(self):
        loop = self.loop("import time; time.sleep(30)")

        def report(lease):
            loop.coordinator.report(lease, "success", "done", handoff=2, outcome="done")
            self.last_writes = self.github.writes.copy()
            return 0

        # The wall clock jumps past the lease while the hook runs, as after a sleep.
        with patch("ub_agents.execution.time.time", return_value=timestamp() + 10_000), \
                self.assertRaisesRegex(LostOwnership, "lease deadline"):
            self.execute(loop, report)
        self.assertFalse(self.removed)
        self.assertEqual(self.github.writes, self.last_writes)
        lease, outcome = loop.coordinator.history(1)
        self.assertFalse(outcome["accepted"])
        self.assertEqual(lease["state"], "running")
        directory = next((self.root / ".ub-agents" / "runs" / lease["run"] / "cleanup").iterdir())
        self.assertEqual(group_members(int((directory / "pid").read_text())), [])
        self.assertEqual((directory / "stopped").read_text(), "confirmed\n")

    def test_hook_runs_after_agent_timeout_interruption_and_lost_ownership(self):
        for mode in ("timeout", "interrupt", "lost"):
            with self.subTest(mode=mode):
                self.github = FakeGitHub(issue())
                self.removed.clear()
                output = self.root / f"{mode}.json"
                loop = self.loop("import os; from pathlib import Path; "
                                 f"Path({str(output)!r}).write_text(Path(os.environ['UB_AGENTS_CLEANUP_CONTEXT']).read_text())")
                def stop(lease):
                    if mode == "timeout":
                        raise AgentError("Execution timed out")
                    if mode == "interrupt":
                        loop.stop_event.set()
                        raise KeyboardInterrupt
                    loop.coordinator.update(lease, state="released", expires=iso(timestamp()), result="blocked")
                    self.last_writes = self.github.writes.copy()
                    raise LostOwnership("lost")
                if mode == "timeout":
                    self.assertTrue(self.execute(loop, stop))
                else:
                    with self.assertRaises(KeyboardInterrupt if mode == "interrupt" else LostOwnership):
                        self.execute(loop, stop)
                self.assertTrue(output.exists())
                self.assertEqual(len(self.removed), 1)
                if mode == "lost":
                    self.assertEqual(self.github.writes, self.last_writes)

    def test_failed_hook_on_lost_ownership_writes_only_local_diagnostic(self):
        loop = self.loop("import sys; sys.exit(1)")
        def lose(lease):
            loop.coordinator.update(lease, state="released", expires=iso(timestamp()), result="blocked")
            self.last_writes = self.github.writes.copy()
            raise LostOwnership("lost")
        with self.assertRaises(LostOwnership):
            self.execute(loop, lose)
        self.assertEqual(self.github.writes, self.last_writes)
        self.assertFalse(self.removed)

    def test_unconfirmed_agent_stop_never_runs_hook_or_removes_tree(self):
        output = self.root / "should-not-exist"
        loop = self.loop(f"from pathlib import Path; Path({str(output)!r}).touch()")
        def uncertain(lease):
            raise CleanupError("agent stop uncertain")
        with self.assertRaises(CleanupError):
            self.execute(loop, uncertain)
        self.assertFalse(output.exists())
        self.assertFalse(self.removed)
        self.assertEqual(loop.coordinator.history(1)[0]["cleanup"], "unconfirmed")

    def test_unconfirmed_hook_stop_stops_loop_and_preserves_tree(self):
        loop = self.loop()
        with patch("ub_agents.hooks.supervise", side_effect=CleanupError("hook stop uncertain")):
            with self.assertRaises(CleanupError):
                self.execute(loop)
        self.assertFalse(self.removed)
        self.assertEqual(loop.coordinator.history(1)[0]["cleanup"], "unconfirmed")
        directory = next((self.root / ".ub-agents" / "runs" / self.workspace.lease["run"] / "cleanup").iterdir())
        self.assertFalse((directory / "stopped").exists())

    def test_shared_agent_never_runs_hook(self):
        self.agent = replace(self.agent, worktree=False)
        loop = self.loop("raise AssertionError('shared hook')")
        def execution(*args, **kwargs):
            loop.coordinator.report(loop.coordinator.history(1)[0], "success", "done", outcome="done")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execution), patch("ub_agents.loop.run_hook") as hook:
            self.assertTrue(loop.tick())
            hook.assert_not_called()

    def test_configuration_requires_argv_and_bounded_finite_positive_timeout(self):
        path = self.root / "ub-agents.yaml"
        base = "repository: org/project\nagents:\n  task:\n    trigger: ready\n    command: [echo]\n    outcomes: {done: {}}\n"
        path.write_text(base)
        self.assertIsNone(load_config(path).cleanup)
        path.write_text(base + "cleanup:\n  command: [./cleanup, 'literal;arg']\n")
        self.assertEqual(load_config(path).cleanup, CleanupHook(("./cleanup", "literal;arg"), 60))
        for value in ("null", "[]", "{}", "{command: echo}", "{command: []}",
                      "{command: [echo], shell: true}", "{command: [echo], timeout-seconds: false}",
                      "{command: [echo], timeout-seconds: 0}", "{command: [echo], timeout-seconds: .inf}",
                      "{command: [echo], timeout-seconds: 3601}"):
            with self.subTest(value=value):
                path.write_text(base + f"cleanup: {value}\n")
                with self.assertRaises(AgentError):
                    load_config(path)

    def test_relative_hook_uses_operator_script_and_handoff_context(self):
        script = self.root / "cleanup-script"
        script.write_text("#!/bin/sh\nprintf '%s' operator > \"$UB_AGENTS_WORKTREE/operator-ran\"\n")
        script.chmod(0o755)
        loop = self.loop()
        loop.config = replace(loop.config, cleanup=CleanupHook(("./cleanup-script",), 3))
        def reported(lease):
            (self.workspace.private / "cleanup-script").write_text("candidate contents")
            lease = self.workspace.lease
            loop.coordinator.report(lease, "success", "done", handoff=2, outcome="done")
            return 0
        self.assertTrue(self.execute(loop, reported))
        self.assertEqual((self.workspace.private / "operator-ran").read_text(), "operator")
        lease = loop.coordinator.history(1)[0]
        context = next((self.root / ".ub-agents" / "runs" / lease["run"] / "cleanup").glob("*/context.json"))
        self.assertEqual(json.loads(context.read_text())["handoff"], 2)

    def test_unconfirmed_hook_after_ownership_loss_keeps_local_evidence_without_writes(self):
        loop = self.loop()
        def lose(lease):
            loop.coordinator.update(lease, state="released", expires=iso(timestamp()), result="blocked")
            self.last_writes = self.github.writes.copy()
            raise LostOwnership("lost")
        with patch("ub_agents.hooks.supervise", side_effect=CleanupError("hook survived")):
            with self.assertRaises(CleanupError):
                self.execute(loop, lose)
        self.assertFalse(self.removed)
        self.assertEqual(self.github.writes, self.last_writes)
        events = self.root / ".ub-agents" / "runs" / self.workspace.lease["run"] / "events.jsonl"
        self.assertIn("cleanup-unconfirmed", events.read_text())

    def test_launcher_records_real_agent_process_group_on_lease(self):
        self.agent = replace(self.agent, worktree=False)
        loop = self.loop()
        self.assertTrue(loop.tick())
        lease = loop.coordinator.history(1)[0]
        pid = int((self.root / ".ub-agents" / "runs" / lease["run"] / "pid").read_text())
        self.assertEqual(lease["process_group"], pid)
        self.assertEqual(group_members(pid), [])
