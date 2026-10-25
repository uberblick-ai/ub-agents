"""Real process locks, CLI startup ordering and informational owner reporting."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.launcher_lock import launcher_lock, running_launcher
from tests.support import FakeGitHub, config, isolate_runtime_state


HOLDER = '''
import json, sys
from unittest.mock import patch
from ub_agents.cli import main
from ub_agents.launcher_lock import running_launcher

def run(args):
    print(json.dumps(running_launcher(args.config.parent)), flush=True)
    sys.stdin.read()

with patch("ub_agents.cli.run", side_effect=run):
    raise SystemExit(main(sys.argv[1:]))
'''


class LauncherLockTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "ub-agents.yaml"
        # Refusal must happen before even trying to parse the configuration.
        self.path.write_text("invalid: [")

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def holder(self, path=None, options=()):
        process = subprocess.Popen([sys.executable, "-c", HOLDER, "--config", str(path or self.path),
                                    "launch", *options], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], "launcher did not start")
        line = process.stdout.readline()
        self.assertTrue(line, process.communicate(timeout=10) if process.poll() is not None else "no owner")
        owner = json.loads(line)
        self.assertEqual(owner["pid"], process.pid)
        self.assertEqual(owner["host"], socket.gethostname())
        self.assertTrue(owner["started_at"].endswith("Z"))
        return process, owner

    def test_all_modes_refuse_before_startup_including_resolved_alias_and_other_config(self):
        holder, owner = self.holder()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        other_config = self.root / "other.yaml"
        other_config.touch()
        log = self.root / ".ub-agents/launch.log"
        before = log.read_bytes()
        for path in (self.path, alias / self.path.name, other_config):
            for options in ([], ["--agent", "worker"], ["--once"], ["42"], ["42", "--agent", "worker"]):
                with self.subTest(path=path, options=options):
                    refused = subprocess.run([sys.executable, "-m", "ub_agents", "--config", str(path),
                                              "launch", *options], capture_output=True, text=True, timeout=10)
                    self.assertEqual(refused.returncode, 1, refused.stderr)
                    for detail in (str(owner["pid"]), owner["host"], owner["started_at"],
                                   "Stop that launcher first", "ub-agents retry"):
                        self.assertIn(detail, refused.stderr)
                    self.assertEqual(refused.stdout, "")
        self.assertEqual(log.read_bytes(), before)
        self.assertIsNone(holder.poll())

    def test_every_mode_takes_the_lock_and_releases_on_normal_exit(self):
        for options in ([], ["--agent", "worker"], ["--once"], ["42"]):
            with self.subTest(options=options):
                process, owner = self.holder(options=options)
                self.assertEqual(running_launcher(self.root), owner)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertIsNone(running_launcher(self.root))

    def test_other_checkout_including_git_worktree_is_independent(self):
        # Checkout identity is the working directory, not a shared .git directory.
        other = self.root / "other-checkout"
        other.mkdir()
        (self.root / ".git").mkdir()
        (other / ".git").write_text(f"gitdir: {self.root / '.git/worktrees/other'}\n")
        other_path = other / self.path.name
        other_path.touch()
        first, first_owner = self.holder()
        second, second_owner = self.holder(path=other_path)
        self.assertNotEqual(first.pid, second.pid)
        self.assertEqual(running_launcher(self.root), first_owner)
        self.assertEqual(running_launcher(other), second_owner)

    def test_sigkill_releases_and_dead_owner_record_never_blocks_or_reports_running(self):
        process, owner = self.holder()
        process.kill()
        process.communicate(timeout=10)
        record = self.root / ".ub-agents/launcher.lock"
        self.assertEqual(json.loads(record.read_text()), owner)
        self.assertIsNone(running_launcher(self.root))
        replacement, replacement_owner = self.holder()
        self.assertNotEqual(replacement.pid, owner["pid"])
        self.assertEqual(running_launcher(self.root), replacement_owner)

    def test_malformed_stale_record_and_reused_live_pid_are_ignored(self):
        with launcher_lock(self.root):
            pass
        record = self.root / ".ub-agents/launcher.lock"
        for stale in ("{broken", json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                            "started_at": "old"})):
            with self.subTest(stale=stale):
                record.write_text(stale)
                self.assertIsNone(running_launcher(self.root))
                process, owner = self.holder()
                self.assertEqual(running_launcher(self.root), owner)
                process.communicate(timeout=10)

    def test_errors_release_lock_and_refusal_precedes_any_startup_calls(self):
        with launcher_lock(self.root), \
                patch("ub_agents.cli.launch_output") as log, \
                patch("ub_agents.launcher_code.startup_copy") as copy, \
                patch("ub_agents.cli.run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["launch", "--config", str(self.path)]), 1)
            log.assert_not_called()
            copy.assert_not_called()
            run.assert_not_called()
        with patch("ub_agents.cli.run", side_effect=OSError("startup failed")), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["launch", "--config", str(self.path)]), 1)
        self.assertIsNone(running_launcher(self.root))
        with self.assertRaisesRegex(RuntimeError, "failure"), launcher_lock(self.root):
            raise RuntimeError("failure")
        self.assertIsNone(running_launcher(self.root))

    def test_exec_helper_does_not_inherit_even_with_close_fds_false(self):
        with launcher_lock(self.root):
            process = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                       close_fds=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(self.stop, process)
            self.assertTrue(select.select([process.stdout], [], [], 10)[0])
            self.assertEqual(process.stdout.readline().strip(), "ready")
        self.assertIsNone(process.poll())
        replacement, _ = self.holder()
        self.assertIsNone(replacement.poll())

    def test_fork_without_exec_does_not_retain_lock(self):
        read, write = os.pipe()
        child = None
        try:
            with launcher_lock(self.root):
                child = os.fork()
                if child == 0:
                    os.close(write)
                    os.read(read, 1)
                    os._exit(0)
                os.close(read)
                read = None
            replacement, _ = self.holder()
            self.assertIsNone(replacement.poll())
        finally:
            if read is not None:
                os.close(read)
            os.close(write)
            if child:
                os.waitpid(child, 0)

    def test_status_text_and_json_show_owner_or_none_without_github_writes(self):
        github = FakeGitHub()
        configured = config(self.root)
        with patch("ub_agents.cli.load_config", return_value=configured), \
                patch("ub_agents.cli.GitHub", return_value=github):
            for active in (False, True):
                if active:
                    process, owner = self.holder()
                else:
                    owner = None
                with self.subTest(active=active), redirect_stdout(io.StringIO()) as structured:
                    self.assertEqual(main(["status", "--config", str(self.path), "--json"]), 0)
                self.assertEqual(json.loads(structured.getvalue())["launcher"], owner)
                with redirect_stdout(io.StringIO()) as text:
                    self.assertEqual(main(["status", "--config", str(self.path)]), 0)
                if active:
                    for value in (str(process.pid), owner["host"], owner["started_at"]):
                        self.assertIn(value, text.getvalue())
                    process.communicate(timeout=10)
                else:
                    self.assertIn("No launcher running on this checkout", text.getvalue())
        self.assertEqual(github.writes, [])

    def test_inspecting_checkout_without_state_creates_nothing(self):
        self.assertIsNone(running_launcher(self.root))
        self.assertFalse((self.root / ".ub-agents").exists())
