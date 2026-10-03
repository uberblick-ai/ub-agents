from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Runtime
from ub_agents.errors import AgentError, CleanupError, LostOwnership
from ub_agents.execution import ScratchDirectory, group_members, supervise
from ub_agents.loop import Loop
from tests.support import FakeGitHub, agent, config, issue, stub_refresh


RECORD_SCRATCH = """import json, os, stat, tempfile
from pathlib import Path
scratch = Path(os.environ['UB_AGENTS_SCRATCH'])
assert scratch.is_absolute()
assert os.environ['TMPDIR'] == str(scratch)
assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
(scratch / 'nested').mkdir()
(scratch / 'nested' / 'temporary').write_text('temporary contents')
with tempfile.NamedTemporaryFile() as temporary:
    assert Path(temporary.name).parent == scratch
print(json.dumps({'scratch': str(scratch), 'mode': stat.S_IMODE(scratch.stat().st_mode)}), flush=True)
"""


class ScratchTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.github = FakeGitHub(issue())
        self.output = []

    def loop(self, script=RECORD_SCRATCH, timeout=3):
        configured = agent(self.root, kind="issue", command=(sys.executable, "-c", script),
                           timeout_seconds=timeout)
        return Loop(config(self.root, configured), self.github, "operator", output=self.output.append)

    def run_dir(self, loop):
        lease = loop.coordinator.history(1)[0]
        return self.root / ".ub-agents" / "runs" / lease["run"]

    def assert_logs_retained(self, directory):
        for name in ("events.jsonl", "process.log", "prompt.txt", "context.json", "pid"):
            self.assertTrue((directory / name).is_file(), name)

    def assert_removal_diagnostic(self, directory, error):
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        failures = [event for event in events if event["event"] == "scratch-removal-failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["path"], str(directory / "scratch"))
        self.assertIn(error, failures[0]["error"])
        self.assertTrue(any("Scratch removal failed:" in line and error in line for line in self.output))
        self.assertFalse(any(event["event"] == "cleanup-unconfirmed" for event in events))

    def test_command_receives_private_scratch_and_removes_contents_after_success_or_failure(self):
        for success in (True, False):
            with self.subTest(success=success):
                self.github = FakeGitHub(issue())
                loop = self.loop(RECORD_SCRATCH + ("" if success else "import sys; sys.exit(3)"))

                def execute(*args, **kwargs):
                    code = supervise(*args, **kwargs)
                    if success:
                        self.assertEqual(code, 0)
                        loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Done", outcome="done")
                    return code

                with patch.dict(os.environ, {"UB_AGENTS_SCRATCH": "/old/scratch", "TMPDIR": "/old/tmp"}), \
                        patch("ub_agents.loop.supervise", side_effect=execute):
                    self.assertTrue(loop.tick())
                directory = self.run_dir(loop)
                recorded = json.loads((directory / "process.log").read_text())
                self.assertEqual(recorded, {"scratch": str(directory / "scratch"), "mode": 0o700})
                self.assertFalse((directory / "scratch").exists())
                self.assert_logs_retained(directory)
                lease, outcome = loop.coordinator.history(1)
                self.assertEqual(lease["result"], "success" if success else "retry")
                self.assertEqual(outcome["accepted"], success)
                if not success:
                    self.assertIn("Execution exited 3", lease["summary"])

    def test_readonly_scratch_does_not_block_release_after_confirmed_termination(self):
        for ending in ("success", "failure", "timeout", "interrupt"):
            with self.subTest(ending=ending):
                self.github = FakeGitHub(issue())
                self.output.clear()
                script = RECORD_SCRATCH + "(scratch / 'nested').chmod(0o500)\nprint('readonly ready', flush=True)\n"
                if ending == "failure":
                    script += "import sys; sys.exit(3)"
                elif ending in {"timeout", "interrupt"}:
                    script += "import time; time.sleep(30)"
                loop = self.loop(script, timeout=1 if ending == "timeout" else 3)

                def execute(command, cwd, env, directory, *args, **kwargs):
                    def observe(final=False):
                        if ending == "interrupt" and "readonly ready" in (directory / "process.log").read_text():
                            loop.interrupt_event.set()
                    code = supervise(command, cwd, env, directory, *args,
                                     **(kwargs | {"observe_output": observe}))
                    if ending == "success":
                        self.assertEqual(code, 0)
                        loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Done", outcome="done")
                    return code

                with patch("ub_agents.loop.supervise", side_effect=execute):
                    if ending == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            loop.tick()
                    else:
                        self.assertTrue(loop.tick())
                directory = self.run_dir(loop)
                self.assertIn("readonly ready", (directory / "process.log").read_text())
                self.assertEqual(group_members(int((directory / "pid").read_text())), [])
                self.assert_logs_retained(directory)
                lease, outcome = loop.coordinator.history(1)
                self.assertEqual((lease["state"], lease["result"]),
                                 ("released", "success" if ending == "success" else "retry"))
                self.assertNotEqual(lease.get("cleanup"), "unconfirmed")
                self.assertEqual(outcome["accepted"], ending == "success")
                scratch = directory / "scratch"
                if scratch.exists():
                    # Privileged users may remove read-only directories without an error.
                    self.addCleanup((scratch / "nested").chmod, 0o700)
                    self.assertEqual((scratch / "nested" / "temporary").read_text(), "temporary contents")
                    self.assert_removal_diagnostic(directory, "Cannot remove run scratch directory")
                else:
                    self.assertFalse(any("Scratch removal failed:" in line for line in self.output))

    def test_removal_io_error_is_diagnostic_and_releases_successful_run(self):
        loop = self.loop()

        def execute(*args, **kwargs):
            code = supervise(*args, **kwargs)
            self.assertEqual(code, 0)
            loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Done", outcome="done")
            return code

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch("ub_agents.execution.shutil.rmtree", side_effect=OSError("disk failure")):
            self.assertTrue(loop.tick())
        directory = self.run_dir(loop)
        self.assertEqual((directory / "scratch" / "nested" / "temporary").read_text(), "temporary contents")
        self.assert_removal_diagnostic(directory, "disk failure")
        self.assert_logs_retained(directory)
        lease, outcome = loop.coordinator.history(1)
        self.assertEqual((lease["state"], lease["result"], outcome["accepted"]), ("released", "success", True))
        self.assertNotEqual(lease.get("cleanup"), "unconfirmed")

    def test_removal_failure_does_not_mask_lost_ownership_or_restart_writes(self):
        loop = self.loop()

        def execute(*args, **kwargs):
            self.assertEqual(supervise(*args, **kwargs), 0)
            self.last_writes = list(self.github.writes)
            raise LostOwnership("Ownership read failed")

        with patch("ub_agents.loop.supervise", side_effect=execute), \
                patch("ub_agents.execution.shutil.rmtree", side_effect=OSError("disk failure")), \
                self.assertRaisesRegex(LostOwnership, "Ownership read failed"):
            loop.tick()
        self.assertEqual(self.github.writes, self.last_writes)
        self.assert_removal_diagnostic(self.run_dir(loop), "disk failure")

    def test_timeout_removes_scratch_after_stopping_processes_and_retains_logs(self):
        loop = self.loop(RECORD_SCRATCH + "import time; time.sleep(30)", timeout=1)
        self.assertTrue(loop.tick())
        directory = self.run_dir(loop)
        self.assertIn('"scratch":', (directory / "process.log").read_text())
        self.assertFalse((directory / "scratch").exists())
        self.assertEqual(group_members(int((directory / "pid").read_text())), [])
        self.assert_logs_retained(directory)
        self.assertIn("timed out", loop.coordinator.history(1)[0]["summary"])

    def test_interrupt_removes_scratch_after_stopping_processes_and_retains_logs(self):
        loop = self.loop(RECORD_SCRATCH + "import time; time.sleep(30)")

        def execute(command, cwd, env, directory, *args, **kwargs):
            def observe(final=False):
                if '"scratch":' in (directory / "process.log").read_text():
                    loop.interrupt_event.set()
            return supervise(command, cwd, env, directory, *args, **(kwargs | {"observe_output": observe}))

        with patch("ub_agents.loop.supervise", side_effect=execute), self.assertRaises(KeyboardInterrupt):
            loop.tick()
        directory = self.run_dir(loop)
        self.assertFalse((directory / "scratch").exists())
        self.assertEqual(group_members(int((directory / "pid").read_text())), [])
        self.assert_logs_retained(directory)
        self.assertIn("Launcher interrupted", loop.coordinator.history(1)[0]["summary"])

    def test_unconfirmed_process_stop_preserves_scratch_contents_and_logs(self):
        loop = self.loop(RECORD_SCRATCH + "import time; time.sleep(30)", timeout=1)
        with patch("ub_agents.execution.group_members", side_effect=CleanupError("inspection failed")), \
                self.assertRaisesRegex(CleanupError, "inspection failed"):
            loop.tick()
        directory = self.run_dir(loop)
        self.assertEqual((directory / "scratch" / "nested" / "temporary").read_text(), "temporary contents")
        self.assert_logs_retained(directory)
        self.assertEqual(loop.coordinator.history(1)[0]["cleanup"], "unconfirmed")
        self.assertIn("cleanup-unconfirmed", (directory / "events.jsonl").read_text())
        # The fallback killed the owned child, but failed inspection cannot prove cleanup.
        self.assertEqual(group_members(int((directory / "pid").read_text())), [])

    def test_creation_or_permission_failure_is_visible_setup_failure_and_never_starts_agent(self):
        for operation in ("mkdir", "chmod"):
            with self.subTest(operation=operation):
                self.github = FakeGitHub(issue())
                loop = self.loop()
                original = getattr(Path, operation)

                def fail_scratch(path, *args, **kwargs):
                    if path.name == "scratch":
                        raise PermissionError("scratch permission denied")
                    return original(path, *args, **kwargs)

                with patch.object(Path, operation, fail_scratch), \
                        patch("ub_agents.loop.supervise") as execute:
                    self.assertTrue(loop.tick())
                execute.assert_not_called()
                lease, outcome = loop.coordinator.history(1)
                self.assertEqual((lease["state"], lease["result"], outcome["status"]), ("released", "retry", "retry"))
                self.assertIn("Cannot create run scratch directory", outcome["summary"])
                self.assertIn("scratch permission denied", lease["summary"])
                self.assertTrue(any("scratch permission denied" in line for line in self.output))
                directory = self.run_dir(loop)
                self.assertIn("scratch permission denied", (directory / "events.jsonl").read_text())
                self.assertFalse((directory / "scratch").exists())
                self.assertFalse((directory / "pid").exists())

    def test_runtime_receives_scratch_outside_worktree_and_prompt_guidance(self):
        loop = self.loop()
        configured = replace(loop.config.agents[0], command=(), runtimes=(Runtime("codex", "model", "high"),),
                             worktree=True)
        loop.config = config(self.root, configured)
        worktree = self.root / "private-worktree"
        worktree.mkdir()

        def execute(command, cwd, env, directory, timeout, stop, prompt, **kwargs):
            scratch = Path(env["UB_AGENTS_SCRATCH"])
            self.assertEqual(scratch, directory / "scratch")
            self.assertEqual(env["TMPDIR"], str(scratch))
            self.assertTrue(scratch.is_absolute())
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
            self.assertEqual(cwd, worktree)
            self.assertFalse(scratch.is_relative_to(worktree))
            self.assertIn("Put temporary files in UB_AGENTS_SCRATCH", prompt)
            self.assertIn("not directly under /tmp", prompt)
            return 0

        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.Workspace.prepare", return_value=worktree), \
                patch("ub_agents.loop.supervise", side_effect=execute) as executed:
            self.assertTrue(loop.tick())
        executed.assert_called_once()
        self.assertFalse((self.run_dir(loop) / "scratch").exists())

    def test_exact_private_mode_even_under_restrictive_umask(self):
        scratch = ScratchDirectory(self.root)
        previous = os.umask(0o777)
        try:
            scratch.prepare()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(scratch.path.stat().st_mode), 0o700)
        scratch.cleanup()

    def test_redirected_scratch_never_removes_shared_files(self):
        scratch = ScratchDirectory(self.root)
        scratch.prepare()
        scratch.path.rmdir()
        shared = self.root / "shared"
        shared.mkdir()
        artifact = shared / "keep"
        artifact.write_text("shared contents")
        scratch.path.symlink_to(shared, target_is_directory=True)
        with self.assertRaisesRegex(AgentError, "Scratch path redirects"):
            scratch.cleanup()
        self.assertEqual(artifact.read_text(), "shared contents")

    def test_redirected_scratch_is_diagnostic_and_does_not_block_release(self):
        shared = self.root / "shared"
        shared.mkdir()
        artifact = shared / "keep"
        artifact.write_text("shared contents")
        loop = self.loop("""import os
from pathlib import Path
scratch = Path(os.environ['UB_AGENTS_SCRATCH'])
scratch.rmdir()
scratch.symlink_to(Path.cwd() / 'shared', target_is_directory=True)
""")
        self.assertTrue(loop.tick())
        self.assertEqual(artifact.read_text(), "shared contents")
        self.assert_removal_diagnostic(self.run_dir(loop), "Scratch path redirects")
        lease = loop.coordinator.history(1)[0]
        self.assertEqual((lease["state"], lease["result"]), ("released", "retry"))
        self.assertNotEqual(lease.get("cleanup"), "unconfirmed")
