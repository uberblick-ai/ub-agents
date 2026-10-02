from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import CleanupHook
from ub_agents.errors import CleanupError
from ub_agents.execution import group_members, supervise
from ub_agents.loop import Loop
from ub_agents.records import timestamp
from tests.support import FakeGitHub, agent, config, issue, stub_refresh


class SignalTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.github = FakeGitHub(issue(), issue(3))
        self.config = config(self.root, agent(self.root, kind="issue"))
        self.loop = Loop(self.config, self.github, "operator", output=lambda *_: None)

    def launch(self):
        def loop(config, github, actor, stop, **kwargs):
            self.loop.stop_event = stop
            self.loop.interrupt_event = kwargs["interrupt_event"]
            return self.loop

        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.Loop", side_effect=loop), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(["launch"])

    def run_signals(self, signals):
        marker = self.root / ".ub-agent" / "finished"

        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            script = "import os,signal,time; " + "; ".join(
                f"os.kill(os.getppid(), {int(sig)}); time.sleep(0.3)" for sig in signals)
            script += f"; open({str(marker)!r}, 'w').write('finished')"
            code = supervise([sys.executable, "-c", script], cwd, env, run_dir, 5, stop,
                             prompt, **kwargs)
            with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["report", "--outcome", "done", "--summary", "Finished"]), 0)
            return code

        with patch("ub_agents.loop.supervise", side_effect=run):
            result = self.launch()
        return result, marker

    def test_sigterm_finishes_active_process_report_transition_and_cleanup(self):
        cleaned = self.root / ".ub-agent" / "hook-finished"
        hook = CleanupHook((sys.executable, "-c",
                            f"open({str(cleaned)!r}, 'w').write('cleaned')"))
        self.config = replace(self.config, cleanup=hook)
        self.loop.config = self.config

        def cleanup(workspace, before_remove):
            # Model an owned private worktree reaching its actual cleanup hook.
            self.assertTrue(before_remove())

        with patch("ub_agents.loop.Workspace.cleanup", autospec=True, side_effect=cleanup) as cleanup:
            result, marker = self.run_signals([signal.SIGTERM])
        self.assertEqual(result, 0)
        self.assertTrue(marker.is_file())
        self.assertTrue(cleaned.is_file())
        cleanup.assert_called_once()
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual((lease["state"], lease["result"]), ("released", "success"))
        self.assertTrue(outcome["accepted"])
        self.assertEqual(self.github.item(1).labels, frozenset())
        self.assertEqual(self.loop.coordinator.history(3), [])

    def test_sigint_and_sighup_terminate_active_process_even_after_sigterm(self):
        for signals in ([signal.SIGINT], [signal.SIGHUP],
                        [signal.SIGTERM, signal.SIGINT], [signal.SIGTERM, signal.SIGHUP]):
            with self.subTest(signals=signals):
                self.setUp()
                result, marker = self.run_signals(signals)
                self.assertEqual(result, 130)
                self.assertFalse(marker.exists())
                lease, outcome = self.loop.coordinator.history(1)
                self.assertEqual((lease["state"], lease["result"]), ("released", "retry"))
                self.assertEqual(lease["attempt_effect"], "unchanged")
                self.assertFalse(outcome["accepted"])
                self.assertEqual(self.loop.coordinator.history(3), [])

    def test_sigterm_wakes_idle_wait_promptly(self):
        self.github.change(1, labels=frozenset())
        self.github.change(3, labels=frozenset())
        timers = []

        def output(line):
            timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGTERM))
            timers.append(timer)
            timer.start()

        self.loop.output = output
        start = time.monotonic()
        try:
            self.assertEqual(self.launch(), 0)
        finally:
            for timer in timers:
                timer.join()
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.github.writes, [])

    def test_sigterm_after_interrupt_does_not_interrupt_group_cleanup_or_release(self):
        for interrupt in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(interrupt=interrupt):
                self.setUp()
                groups = []
                cleaned = []

                def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
                    # Notify the launcher precisely when stop_group sends TERM.
                    # The child stays alive until that cleanup escalates to KILL.
                    script = ("import os,signal,time; signal.alarm(10); "
                              "signal.signal(signal.SIGTERM, "
                              "lambda *_: os.kill(os.getppid(), signal.SIGTERM)); "
                              f"os.kill(os.getppid(), {int(interrupt)}); time.sleep(30)")
                    try:
                        return supervise([sys.executable, "-c", script], cwd, env, run_dir,
                                         5, stop, prompt, **kwargs)
                    finally:
                        groups.append(int((run_dir / "pid").read_text()))

                def cleanup(workspace, before_remove):
                    self.assertEqual(group_members(groups[0]), [])
                    signal.raise_signal(signal.SIGTERM)
                    self.assertTrue(before_remove())
                    cleaned.append(True)

                release = self.loop.coordinator.release

                def finish_release(*args, **kwargs):
                    signal.raise_signal(signal.SIGTERM)
                    return release(*args, **kwargs)

                try:
                    with patch("ub_agents.loop.supervise", side_effect=run), \
                            patch("ub_agents.loop.Workspace.cleanup", autospec=True, side_effect=cleanup), \
                            patch.object(self.loop.coordinator, "release", side_effect=finish_release):
                        self.assertEqual(self.launch(), 130)
                    self.assertEqual(group_members(groups[0]), [])
                    self.assertEqual(cleaned, [True])
                    lease, outcome = self.loop.coordinator.history(1)
                    self.assertEqual((lease["state"], lease["result"]), ("released", "retry"))
                    self.assertEqual(lease["attempt_effect"], "unchanged")
                    self.assertFalse(outcome["accepted"])
                    self.assertEqual(self.loop.coordinator.history(3), [])
                finally:
                    for group in groups:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_sigterm_interrupts_slow_discovery_subprocess_promptly(self):
        pid_path = self.root / "poll-pid"

        def observe(details=True):
            script = ("import os,signal,time; "
                      f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
                      "os.kill(os.getppid(), signal.SIGTERM); time.sleep(10)")
            subprocess.run([sys.executable, "-c", script], check=True, timeout=15)
            self.fail("Discovery continued after SIGTERM")

        start = time.monotonic()
        with patch.object(self.github, "observe", side_effect=observe):
            self.assertEqual(self.launch(), 0)
        self.assertLess(time.monotonic() - start, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)
        self.assertEqual(self.github.writes, [])

    def test_sigterm_before_claim_write_makes_no_claim(self):
        original = self.github.item

        def item(*args):
            result = original(*args)
            signal.raise_signal(signal.SIGTERM)
            return result

        with patch.object(self.github, "item", side_effect=item), \
                patch("ub_agents.loop.supervise") as execution:
            self.assertEqual(self.launch(), 0)
        execution.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_sigterm_during_recovery_finishes_and_claims_nothing_else(self):
        now = timestamp()
        self.loop.coordinator.clock = lambda: now
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        self.loop.coordinator.report(lease, "success", "Finished", outcome="done")
        now += 61
        finalize = self.loop.finalize

        def finish(*args):
            signal.raise_signal(signal.SIGTERM)
            return finalize(*args)

        with patch.object(self.loop, "finalize", side_effect=finish), \
                patch("ub_agents.loop.supervise") as execution:
            self.assertEqual(self.launch(), 0)
        execution.assert_not_called()
        history = self.loop.coordinator.history(1)
        recovered = next(r for r in history if r.get("mode") == "recovery")
        self.assertEqual((recovered["state"], recovered["result"]), ("released", "success"))
        self.assertTrue(self.loop.coordinator.outcome(lease)["accepted"])
        self.assertEqual(self.github.item(1).labels, frozenset())
        self.assertEqual(self.loop.coordinator.history(3), [])

    def test_sigterm_does_not_hide_launcher_error(self):
        def fail(*args, **kwargs):
            signal.raise_signal(signal.SIGTERM)
            raise CleanupError("Unconfirmed process cleanup")

        with patch("ub_agents.loop.supervise", side_effect=fail):
            self.assertEqual(self.launch(), 1)
