from contextlib import redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import ub_agents
from ub_agents.cli import main
from ub_agents.coordination import Coordinator
from ub_agents.execution import supervise
from ub_agents.launcher_code import descriptors, helper_command, startup_copy
from ub_agents.loop import Loop
from ub_agents.report_command import launcher_report_command
from ub_agents.state import lock
from tests.support import FakeGitHub, agent, config, isolate_runtime_state, issue, run_environment


class LauncherCodeTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_supervised_report_and_helpers_ignore_changed_imports_and_editable_install(self):
        installation = self.root / "installed package"
        package = installation / "ub_agents"
        shutil.copytree(Path(ub_agents.__file__).parent, package)
        # Reuse the recording adapter inside the copied package so the real
        # report subprocess validates and writes a durable outcome without gh.
        shutil.copyfile(Path(__file__).with_name("support.py"), package / "fixture_support.py")
        with (package / "cli.py").open("a") as stream:
            stream.write('''
from .fixture_support import FakeGitHub, issue
fixture = FakeGitHub(issue())
fixture.store = {1: json.loads(Path(os.environ["FIXTURE_COMMENTS"]).read_text())}
fixture.next_id = 100
GitHub = lambda repository: fixture
''')
        probe = 'from ub_agents import run_config\nprint(run_config.__file__)\n'
        for module in ("view_request", "observation_worker", "view", "startup_probe"):
            (package / f"{module}.py").write_text(probe)
        github = FakeGitHub(issue())
        coordinator = Coordinator(github, "operator")
        configured = config(self.root)
        lease = coordinator.claim(coordinator.plan(issue(), configured.agents[0], ()))
        coordinator.update(lease, state="running", started=True)
        comments = self.root / "comments.json"
        comments.write_text(json.dumps(github.comments(1)))
        shadow = self.root / "shadow"
        shadow.mkdir()
        env = dict(os.environ, **run_environment(configured, configured.agents[0], lease),
                   FIXTURE_COMMENTS=str(comments), PYTHONPATH=str(installation))
        with patch("ub_agents.launcher_code.__file__", str(package / "launcher_code.py")), \
                startup_copy() as copied:
            command = launcher_report_command()
            self.assertEqual(Path(shlex.split(command)[-1]).parent, copied / "ub_agents")
            self.assertFalse(copied.is_relative_to(installation))
            # An imported dependency, rather than only the entry-point script,
            # reproduces the failure caused by upgrading a running launcher.
            (package / "run_config.py").write_text('raise RuntimeError("changed installed code")\n')
            (shadow / "ub_agents").mkdir()
            (shadow / "ub_agents/__init__.py").write_text('raise RuntimeError("cwd shadow")\n')
            run_dir = self.root / "execution"
            self.assertEqual(supervise(shlex.split(command) + ["report", "--outcome", "done",
                                      "--summary", "Copied code reported successfully"], shadow, env, run_dir,
                                      10, threading.Event(), pass_fds=descriptors()), 0,
                             (run_dir / "process.log").read_text())
            self.assertEqual(json.loads((run_dir / "process.log").read_text())["status"], "success")
            for module in ("view_request", "observation_worker", "view"):
                (package / f"{module}.py").write_text('raise RuntimeError("changed helper")\n')
                result = subprocess.run(helper_command("ub_agents." + module), cwd=shadow, env=env,
                                        pass_fds=descriptors(), capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout.strip()), copied / "ub_agents/run_config.py")
            lazy = importlib.import_module("ub_agents.startup_probe")
            self.addCleanup(sys.modules.pop, "ub_agents.startup_probe", None)
            self.assertEqual(Path(lazy.__file__), copied / "ub_agents/startup_probe.py")
        self.assertFalse(copied.exists())

    def test_cli_copies_before_refresh_and_removes_copy_on_normal_exit(self):
        (self.root / "ub-agents.yaml").touch()
        paths = []
        def launch(*, once):
            paths.append(Path(shlex.split(launcher_report_command())[-1]).parent.parent)
            self.assertTrue(paths[-1].is_dir())
            self.assertEqual(len(descriptors()), 1)
        with patch("ub_agents.cli.load_config", return_value=config(self.root)), \
                patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.cli.launch_checks"), patch.object(Loop, "_before_claim"), \
                patch.object(Loop, "launch", side_effect=launch), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.root / "ub-agents.yaml"), "launch", "--once"]), 0)
        self.assertFalse(paths[0].exists())
        self.assertEqual(descriptors(), ())

    def test_inherited_run_lock_retains_copy_after_launcher_exit_and_during_startup_pruning(self):
        with startup_copy() as copied:
            process = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                       pass_fds=descriptors(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(process.wait, timeout=10)
            self.addCleanup(process.kill)
            self.assertEqual(process.stdout.readline().strip(), "ready")
        try:
            self.assertTrue(copied.exists())
            with startup_copy():
                self.assertTrue(copied.exists())
            process.communicate(timeout=10)
            with startup_copy():
                self.assertFalse(copied.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_another_user_retains_copy_until_its_lock_closes(self):
        with startup_copy() as copied:
            other = self.enterContext(lock(copied / "users.lock", shared=True))
        self.assertTrue(copied.exists())
        with startup_copy():
            self.assertTrue(copied.exists())
        other.close()
        with startup_copy():
            self.assertFalse(copied.exists())

    def test_killed_launcher_copy_is_pruned_on_next_start(self):
        script = '''
import sys
from ub_agents.launcher_code import startup_copy
with startup_copy() as path:
    print(path, flush=True)
    sys.stdin.read()
'''
        process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            copied = Path(process.stdout.readline().strip())
            self.assertTrue(copied.is_dir())
            process.kill()
            process.communicate(timeout=10)
            self.assertTrue(copied.exists())
            with startup_copy():
                self.assertFalse(copied.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
