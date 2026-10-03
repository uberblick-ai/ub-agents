from contextlib import contextmanager, ExitStack
from dataclasses import replace
import json
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

from ub_agents.config import Runtime, RuntimeUpdates
from ub_agents.execution import supervise
from ub_agents.loop import Loop
from ub_agents.records import attempts, iso
from ub_agents.runtime_installations import installation
from ub_agents.runtime_updates import (COOLDOWN_SECONDS, MaintenanceFailure, RuntimeMaintenance,
                                      lock, state_directory)
from ub_agents.runtime_usage import RuntimeUsage
from ub_agents.usage_output import UsageOutput
from tests.support import FakeGitHub, agent, config, issue, stub_refresh


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch("pathlib.Path.home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath,
                                              "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "DISABLE_UPDATES": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 1000000
        self.calls = []
        self.brew_envs = []
        self.lines = []
        self.action = lambda: None
        self.stop = threading.Event()
        self.manager = self.manager_for()

    def which(self, name):
        path = self.bin / name
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None

    def manager_for(self, runner=None):
        return RuntimeMaintenance(self.lines.append, self.stop, self.root / "state",
                                  lambda: self.now, self.which, runner or self.run_command)

    def executable(self, path, version="1.0"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\nprint({version!r})\n")
        path.chmod(0o755)
        Path(str(path) + ".version").write_text(version)
        return path

    def link(self, cli, target):
        path = self.bin / cli
        path.unlink(missing_ok=True)
        path.symlink_to(target)
        return path

    def npm(self, cli="codex", prefix=None, version="1.0"):
        prefix = prefix or self.root / "npm"
        package = "@openai/codex" if cli == "codex" else "@anthropic-ai/claude-code"
        folder = prefix / "lib/node_modules" / package
        target = self.executable(folder / "bin" / (cli + ".js"), version)
        (folder / "package.json").write_text(json.dumps({"name": package, "version": version,
                                                        "bin": {cli: "bin/" + cli + ".js"}}))
        self.executable(prefix / "bin/npm")
        self.link(cli, target)
        return target

    def brew(self, cli="codex", token=None, kind="Caskroom"):
        token = token or ("codex" if cli == "codex" else "claude-code")
        prefix = self.root / "brew"
        target = self.executable(prefix / kind / token / "1.0/bin" / cli)
        self.executable(prefix / "bin/brew")
        self.link(cli, target)
        return target

    def native(self):
        target = self.executable(self.home / ".local/share/claude/versions/1.0")
        self.link("claude", target)
        return target

    def run_command(self, command, env, timeout, cancellable=True):
        self.calls.append(tuple(command))
        if "upgrade" in command and Path(command[0]).name == "brew":
            self.brew_envs.append({key: env.get(key) for key in (
                "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK", "HOMEBREW_NO_INSTALL_CLEANUP",
                "HOMEBREW_NO_SUDO", "HOMEBREW_NO_UPGRADE_QUIT_CASKS", "HOMEBREW_NO_ASK")})
        if command[-1] == "--version":
            try:
                return Path(str(Path(command[0]).resolve()) + ".version").read_text()
            except OSError as exc:
                raise MaintenanceFailure("broken executable") from exc
        if command[1:] == ["--prefix"]:
            return str(Path(command[0]).parent.parent)
        if command[1:] == ["prefix", "-g"]:
            return str(Path(command[0]).parent.parent)
        self.action()
        return "updated"

    def settings(self, cli="codex", policy="auto", timeout=300):
        role = agent(self.root, command=(), runtimes=(Runtime(cli, "model", "high"),))
        return replace(config(self.root, role), runtime_updates=RuntimeUpdates({cli: policy}, timeout))

    def updates(self):
        return [c for c in self.calls if "update" in c or "upgrade" in c or "install" in c]

    def test_due_restart_and_shared_installation(self):
        self.npm()
        settings = self.settings()
        self.manager.boundary(settings)
        self.assertEqual(len(self.updates()), 1)
        self.assertIn("up-to-date", self.lines[-1])
        second = self.manager_for()
        second.boundary(replace(settings, root=self.root / "other-project"))
        self.now += COOLDOWN_SECONDS - 1
        second.boundary(settings)
        self.assertEqual(len(self.updates()), 1)
        self.now += 1
        second.boundary(settings)
        self.assertEqual(len(self.updates()), 2)

    def test_project_opt_out_does_not_suppress_another_project_update(self):
        self.native()
        policies = {"off": RuntimeUpdates({"claude": "off"}),
                    "omitted": RuntimeUpdates({"codex": "auto"}), "absent": None}
        for name, policy in policies.items():
            with self.subTest(policy=name):
                self.calls.clear()
                first, second = self.manager_for(), self.manager_for()
                first.root = second.root = self.root / name
                settings = replace(self.settings("claude"), runtime_updates=policy)
                first.boundary(settings)
                first.boundary(settings)
                self.assertEqual(self.calls, [])
                self.assertFalse(first.root.exists())
                second.boundary(replace(self.settings("claude", ("custom-updater", "claude")),
                                        root=self.root / "other-project"))
                self.assertEqual(self.calls.count(("custom-updater", "claude")), 1)

    def test_launcher_disable_updates_does_not_suppress_another_launcher(self):
        self.native()
        settings = self.settings("claude")
        with patch.dict(os.environ, {"DISABLE_UPDATES": "1"}):
            self.manager.boundary(settings)
            self.manager.boundary(settings)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.manager.root.exists())
        self.assertEqual(len(self.lines), 1)
        self.assertIn("DISABLE_UPDATES", self.lines[0])
        self.manager_for().boundary(settings)
        self.assertEqual(len(self.updates()), 1)

    def test_auto_skips_do_not_suppress_another_projects_command_update(self):
        cases = ("unknown", "shim", "npm-privileges", "brew-privileges",
                 "npm-owner", "brew-owner", "prerelease")
        for case in cases:
            with self.subTest(case=case), ExitStack() as cleanup:
                self.calls.clear()
                first, second = self.manager_for(), self.manager_for()
                first.root = second.root = self.root / ("state-" + case)
                if case == "unknown":
                    self.executable(self.bin / "codex")
                elif case == "shim":
                    self.link("codex", self.executable(self.root / "mise/shims/codex"))
                elif case.startswith("npm") or case == "prerelease":
                    target = self.npm(prefix=self.root / case,
                                      version="2.0-alpha.1" if case == "prerelease" else "1.0")
                    if case == "npm-privileges":
                        target.parent.parent.chmod(0o555)
                        cleanup.callback(target.parent.parent.chmod, 0o755)
                    elif case == "npm-owner":
                        (self.root / case / "bin/npm").unlink()
                else:
                    self.brew()
                    if case == "brew-privileges":
                        (self.root / "brew").chmod(0o555)
                        cleanup.callback((self.root / "brew").chmod, 0o755)
                    else:
                        (self.root / "brew/bin/brew").unlink()
                first.boundary(self.settings())
                self.assertIn("skipped", self.lines[-1])
                state_path = first.paths(installation("codex", self.which))[2]
                self.assertFalse(state_path.exists())
                calls, lines = list(self.calls), list(self.lines)
                first.boundary(self.settings())
                self.assertEqual(self.calls, calls)
                self.assertEqual(self.lines, lines)
                self.now += COOLDOWN_SECONDS - 60
                second.boundary(replace(self.settings(policy=("custom-updater", "codex")),
                                        root=self.root / "other-project"))
                self.assertEqual(self.calls.count(("custom-updater", "codex")), 1)
                self.assertTrue(second.read(state_path)["usable"])
                # A completed command check still shares its cooldown with auto.
                self.now += 60
                calls = list(self.calls)
                first.boundary(self.settings())
                self.assertEqual(self.calls, calls)

    def test_legacy_shared_auto_skip_does_not_suppress_command_update(self):
        self.executable(self.bin / "codex")
        install = installation("codex", self.which)
        self.manager.root.mkdir()
        self.manager.write(self.manager.paths(install)[2],
                           {"checked": self.now, "usable": True, "result": "skipped"})
        self.manager_for().boundary(self.settings(policy=("custom-updater", "codex")))
        self.assertEqual(self.calls.count(("custom-updater", "codex")), 1)

    def test_local_auto_skip_can_be_replaced_by_command_and_retried_next_day(self):
        self.executable(self.bin / "codex")
        settings = self.settings()
        self.manager.boundary(settings)
        self.now += COOLDOWN_SECONDS
        count = len(self.lines)
        self.manager.boundary(settings)
        self.assertEqual(len(self.lines), count + 1)
        self.manager.boundary(self.settings(policy=("custom-updater", "codex")))
        self.assertEqual(self.calls.count(("custom-updater", "codex")), 1)

    def test_guard_blocks_another_loop_and_opt_out_runtime_start(self):
        self.npm()
        second = self.manager_for()
        entered, finish = threading.Event(), threading.Event()
        self.action = lambda: (entered.set(), finish.wait(5))
        worker = threading.Thread(target=self.manager.boundary, args=(self.settings(),))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            before = list(self.calls)
            second.boundary(self.settings())
            self.assertEqual(before, self.calls)
            self.assertFalse(second.available("codex"))
            with second.reserve("codex") as reserved:
                self.assertIsNone(reserved)
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(second.available("codex"))
        second.boundary(self.settings())
        self.assertEqual(len(self.updates()), 1)

    def test_in_place_active_runs_defer_without_cooldown(self):
        self.npm()
        settings = self.settings()
        with self.manager.reserve("codex") as reserved:
            self.assertIsNotNone(reserved)
            reserved.started()
            self.now += COOLDOWN_SECONDS
            self.manager_for().boundary(settings)
            self.assertEqual(self.calls, [])
            state = self.manager.paths(installation("codex", self.which))[2]
            self.assertFalse(state.exists())
        self.manager.boundary(settings)
        self.assertEqual(len(self.updates()), 1)

    def test_native_updates_keep_active_runs_and_cooldown_across_symlink_changes(self):
        self.native()
        newer = self.executable(self.home / ".local/share/claude/versions/2.0", "2.0")
        self.action = lambda: self.link("claude", newer)
        with self.manager.reserve("claude") as reserved:
            self.assertIsNotNone(reserved)
            reserved.started()
            self.manager_for().boundary(self.settings("claude"))
        self.assertIn("1.0 -> 2.0: updated", self.lines[-1])
        self.manager.boundary(self.settings("claude"))
        self.assertEqual(self.updates(), [(str(self.bin / "claude"), "update")])

    def test_operator_command_is_in_place_even_for_native(self):
        self.native()
        settings = self.settings("claude", ("custom-updater", "claude"))
        with self.manager.reserve("claude") as reserved:
            reserved.started()
            self.manager_for().boundary(settings)
            self.assertEqual(self.calls, [])
        self.manager.boundary(settings)
        self.assertIn(("custom-updater", "claude"), self.calls)

    def test_stale_locks_from_crashed_launcher_do_not_block(self):
        self.npm()
        guard, runs, state = self.manager.paths(installation("codex", self.which))
        self.manager.root.mkdir()
        script = "import fcntl,os,sys; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_SH); os._exit(1)"
        result = subprocess.run([sys.executable, "-c", script, str(runs)], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(runs.exists())
        self.manager.boundary(self.settings())
        self.assertEqual(len(self.updates()), 1)

    def test_active_descriptor_survives_launcher_crash_until_child_exits(self):
        self.npm()
        self.manager.root.mkdir()
        runs = self.manager.paths(installation("codex", self.which))[1]
        pidfile = self.root / "child-pid"
        gate = self.root / "finish-child"
        child = ("import os,time; from pathlib import Path; "
                 f"Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                 f"gate=Path({str(gate)!r}); "
                 "exec('while not gate.exists(): time.sleep(0.01)')")
        parent = ("import fcntl,os,subprocess,sys; "
                  "f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_SH); "
                  "subprocess.Popen([sys.executable,'-c',sys.argv[2]],pass_fds=(f.fileno(),)); os._exit(1)")
        try:
            subprocess.run([sys.executable, "-c", parent, str(runs), child], check=False)
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pidfile.exists())
            self.manager.boundary(self.settings())
            self.assertEqual(self.calls, [])
        finally:
            gate.touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with lock(runs) as released:
                    if released is not None:
                        break
                time.sleep(0.01)
        self.manager.boundary(self.settings())
        self.assertEqual(len(self.updates()), 1)

    def test_failures_and_timeout_complete_cooldown_and_preserve_usable_runtime(self):
        self.npm()
        for reason in ("command exited 1", "timed out after 1s"):
            with self.subTest(reason=reason):
                self.now += COOLDOWN_SECONDS
                def fail():
                    raise MaintenanceFailure(reason)
                self.action = fail
                self.manager.boundary(self.settings())
                self.assertIn("failed — warning: " + reason, self.lines[-1])
                self.assertTrue(self.manager.available("codex"))
                count = len(self.updates())
                self.manager_for().boundary(self.settings())
                self.assertEqual(len(self.updates()), count)

    def test_next_path_executable_is_verified_and_broken_runtime_not_claimed(self):
        self.npm()
        replacement = self.executable(self.root / "replacement", "9.0")
        self.action = lambda: self.link("codex", replacement)
        self.manager.boundary(self.settings())
        self.assertIn("1.0 -> 9.0: updated", self.lines[-1])
        # A second install is broken by its own updater, not merely a failing exit code.
        self.npm(prefix=self.root / "npm2")
        target = (self.bin / "codex").resolve()
        self.action = lambda: Path(str(target) + ".version").unlink()
        self.manager.boundary(self.settings())
        self.assertFalse(self.manager.available("codex"))
        self.assertIn("runtime is unusable; no new runs", self.lines[-1])
        github = FakeGitHub(issue())
        loop = Loop(self.settings(), github, "operator", output=self.lines.append)
        loop.maintenance = self.manager
        loop.coordinator.runtime_available = self.manager.available
        loop.tick()
        self.assertEqual(github.writes, [])
        self.assertEqual(len(self.updates()), 2)

    def test_absent_off_unused_missing_unknown_and_mise_skips(self):
        self.executable(self.bin / "codex")
        self.manager.boundary(config(self.root))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.lines, [])
        self.manager.boundary(self.settings(policy="off"))
        self.assertIn("skipped — runtime-updates policy is off", self.lines[-1])
        self.assertEqual(self.updates(), [])
        self.now += COOLDOWN_SECONDS
        self.manager.boundary(self.settings())
        self.assertIn("configure runtime-updates.codex.command argv", self.lines[-1])
        self.now += COOLDOWN_SECONDS
        shim = self.executable(self.root / "mise/shims/codex")
        self.link("codex", shim)
        self.manager.boundary(self.settings())
        self.assertIn("shim", self.lines[-1])
        count = len(self.calls)
        unused = replace(config(self.root), runtime_updates=RuntimeUpdates({"claude": "auto"}))
        self.manager.boundary(unused)
        self.assertIn("(unused): skipped", self.lines[-1])
        self.assertEqual(len(self.calls), count)
        self.manager.boundary(self.settings("claude"))
        self.assertIn("(missing): skipped", self.lines[-1])
        self.assertEqual(self.updates(), [])

    def test_targeted_commands_and_homebrew_channels(self):
        cases = [("claude", "npm", None, None), ("claude", "brew", "claude-code", "Caskroom"),
                 ("claude", "brew", "claude-code@latest", "Caskroom"),
                 ("codex", "brew", "codex", "Caskroom"), ("codex", "brew", "codex", "Cellar"),
                 ("codex", "npm", None, None)]
        for cli, method, token, kind in cases:
            with self.subTest(cli=cli, method=method, token=token, kind=kind):
                self.now += COOLDOWN_SECONDS
                self.calls.clear()
                if method == "npm":
                    self.npm(cli)
                else:
                    self.brew(cli, token, kind)
                self.manager.boundary(self.settings(cli))
                expected = ([str(self.bin / cli), "update"] if cli == "claude" and method == "npm" else
                            [str(self.root / "npm/bin/npm"), "install", "-g", "@openai/codex@latest"] if method == "npm" else
                            [str(self.root / "brew/bin/brew"), "upgrade", "--cask" if kind == "Caskroom" else "--formula", token])
                self.assertEqual(self.updates(), [tuple(expected)])
                if method == "brew":
                    self.assertTrue(all(value == "1" for value in self.brew_envs[-1].values()))

    def test_wrong_npm_prefix_prerelease_and_nonwritable_install_skipped(self):
        target = self.npm()
        owner_npm = self.root / "npm/bin/npm"
        owner_npm.unlink()
        self.executable(self.bin / "npm")
        self.manager.boundary(self.settings())
        self.assertIn("owning this global prefix not found", self.lines[-1])
        self.assertEqual(self.updates(), [])
        self.now += COOLDOWN_SECONDS
        self.npm(version="2.0-alpha.1")
        self.manager.boundary(self.settings())
        self.assertIn("prerelease channel", self.lines[-1])
        self.assertEqual(self.updates(), [])
        self.now += COOLDOWN_SECONDS
        self.npm()
        folder = target.parent.parent
        folder.chmod(0o555)
        try:
            self.manager.boundary(self.settings())
            self.assertIn("elevated privileges", self.lines[-1])
            self.assertEqual(self.updates(), [])
        finally:
            folder.chmod(0o755)
        self.assertEqual(installation("codex", lambda cli: "/usr/bin/codex").method, "system")

    def test_claude_disable_updates_environment_user_and_managed_settings(self):
        self.brew("claude")
        user = self.home / ".claude/settings.json"
        user.parent.mkdir()
        managed = self.root / "managed-settings.json"
        for source in ("environment", "user", "managed"):
            with self.subTest(source=source):
                self.now += COOLDOWN_SECONDS
                user.unlink(missing_ok=True)
                managed.unlink(missing_ok=True)
                if source != "environment":
                    (user if source == "user" else managed).write_text(json.dumps({"env": {"DISABLE_UPDATES": "1"}}))
                read_text = Path.read_text
                def read(path, *args, **kwargs):
                    if str(path) in ("/etc/claude-code/managed-settings.json",
                                     "/Library/Application Support/ClaudeCode/managed-settings.json"):
                        path = managed
                    return read_text(path, *args, **kwargs)
                with patch("pathlib.Path.read_text", read), patch.dict(os.environ, {"DISABLE_UPDATES": "1"} if source == "environment" else {}, clear=False):
                    self.manager.boundary(self.settings("claude"))
                self.assertIn("DISABLE_UPDATES", self.lines[-1])
                self.assertEqual(self.updates(), [])
                self.assertFalse(self.manager.root.exists())
        self.now += COOLDOWN_SECONDS
        user.unlink(missing_ok=True)
        with patch.dict(os.environ, {"DISABLE_AUTOUPDATER": "1"}):
            self.manager.boundary(self.settings("claude"))
        self.assertEqual(len(self.updates()), 1)

    def test_claude_policy_precheck_covers_native_npm_and_operator_commands(self):
        user = self.home / ".claude/settings.json"
        user.parent.mkdir()
        for method in ("native", "npm", "command"):
            for contents in ('{"env": {"DISABLE_UPDATES": "1"}}', "malformed"):
                with self.subTest(method=method, contents=contents):
                    if method == "npm":
                        self.npm("claude")
                    else:
                        self.native()
                    user.write_text(contents)
                    policy = ("custom-updater", "claude") if method == "command" else "auto"
                    self.manager_for().boundary(self.settings("claude", policy))
                    self.assertIn("skipped", self.lines[-1])
                    self.assertEqual(self.calls, [])
                    self.assertFalse(self.manager.root.exists())

    def test_real_fake_updater_timeout_and_shutdown_stop_process(self):
        self.executable(self.bin / "codex")
        updater = self.root / "fake-updater"
        updater.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        updater.chmod(0o755)
        real = self.manager_for()
        real.runner = real._run
        real.boundary(self.settings(policy=(str(updater),), timeout=0.1))
        self.assertIn("timed out", self.lines[-1])
        self.assertTrue(real.available("codex"))
        self.now += COOLDOWN_SECONDS
        timer = threading.Timer(0.1, self.stop.set)
        timer.start()
        try:
            real.boundary(self.settings(policy=(str(updater),)))
        finally:
            timer.join()
        self.assertIn("shutdown requested", self.lines[-1])
        state = real.read(real.paths(installation("codex", self.which))[2])
        self.assertEqual(state["checked"], self.now)

    def test_shutdown_during_boundary_has_no_github_attempt(self):
        self.npm()
        settings = self.settings()
        github = FakeGitHub(issue())
        loop = Loop(settings, github, "operator", stop_event=self.stop, output=self.lines.append)
        loop.maintenance = self.manager
        loop.coordinator.runtime_available = self.manager.available
        def shutdown():
            loop.stop_gracefully()
            raise MaintenanceFailure("shutdown requested")
        self.action = shutdown
        loop.launch(once=True)
        self.assertEqual(github.writes, [])
        self.assertIn("shutdown requested", self.lines[-1])

    def test_run_crossing_due_time_is_uninterrupted_then_next_boundary_updates(self):
        stub_refresh(self)
        self.npm()
        settings = self.settings()
        github = FakeGitHub(issue())
        loop = Loop(settings, github, "operator", output=self.lines.append)
        loop.maintenance = self.manager
        loop.coordinator.runtime_available = self.manager.available
        def execution(*args, **kwargs):
            kwargs["process_started"](12345)
            count = len(self.updates())
            self.now += COOLDOWN_SECONDS
            self.manager_for().boundary(settings)
            self.assertEqual(len(self.updates()), count)
            self.assertIn("pass_fds", kwargs)
            self.assertTrue(self.manager.available("codex"))
            return 1
        with patch("ub_agents.loop.supervise", side_effect=execution):
            self.assertTrue(loop.tick())
        self.assertEqual(len(self.updates()), 1)
        loop.tick()
        self.assertEqual(len(self.updates()), 2)

    def test_usage_limit_preserves_run_lock_pause_and_uncharged_retry(self):
        stub_refresh(self)
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                self.npm(cli)
                self.calls.clear()
                settings = self.settings(cli)
                role = replace(settings.agents[0], backoff_seconds=60)
                settings = replace(settings, agents=(role,))
                self.manager.boundary(settings)
                self.now += COOLDOWN_SECONDS - 1
                github = FakeGitHub(issue())
                loop = Loop(settings, github, "operator", output=self.lines.append)
                loop.coordinator.clock = lambda: self.now
                loop.maintenance = self.manager
                loop.coordinator.runtime_available = self.manager.available

                def execution(command, cwd, env, run_dir, *args, **kwargs):
                    self.assertEqual(command[0], str(self.bin / cli))
                    self.assertTrue(kwargs["pass_fds"])
                    if cli == "codex":
                        self.assertIn("--json", command)
                    kwargs["process_started"](12345)
                    self.now += 1
                    self.manager_for().boundary(settings)
                    self.assertEqual(len(self.updates()), 1)
                    if cli == "claude":
                        event = {"type": "rate_limit_event", "rate_limit_info": {
                            "status": "rejected", "rateLimitType": "five_hour",
                            "resetsAt": self.now + 100}}
                    else:
                        event = {"type": "token_count", "rate_limits": {
                            "primary": {"used_percent": 100, "window_minutes": 300,
                                        "resets_at": self.now + 100},
                            "rate_limit_reached_type": "rate_limit_reached"}}
                    (run_dir / "process.log").write_text(json.dumps(event) + "\n")
                    kwargs["observe_output"]()
                    return 1

                with patch("ub_agents.loop.supervise", side_effect=execution) as run:
                    self.assertTrue(loop.tick())
                    history = loop.coordinator.history(1)
                    self.assertEqual((history[0]["result"], history[0]["attempt_effect"]),
                                     ("retry", "unchanged"), history[0]["summary"])
                    self.assertIsNone(history[0].get("retry_after"))
                    self.assertEqual(attempts(history, role.name, self.now), [])
                    self.assertEqual(loop.usage.paused(cli)["ends_at"], iso(self.now + 160))
                    writes = list(github.writes)
                    self.assertFalse(loop.tick())
                    self.assertEqual(len(self.updates()), 2)
                    self.assertEqual(github.writes, writes)
                    self.assertEqual(loop.plans()[0].state, "waiting")
                    run.assert_called_once()
                self.now += 160
                self.assertEqual((loop.plans()[0].state, loop.plans()[0].attempt), ("ready", 1))

    def test_health_recovery_keeps_usage_pause_and_allows_healthy_alternative(self):
        stub_refresh(self)
        self.npm("claude")
        target = self.npm("codex")
        version = Path(str(target) + ".version")
        self.action = version.unlink
        self.manager.boundary(self.settings("codex"))
        state_path = self.manager.paths(installation("codex", self.which))[2]
        checked = self.manager.read(state_path)["checked"]
        self.action = lambda: None
        settings = self.settings("claude")
        role = replace(settings.agents[0], runtimes=settings.agents[0].runtimes +
                       (Runtime("codex", "model", "high"),))
        settings = replace(settings, agents=(role,), runtime_updates=RuntimeUpdates({
            "claude": "auto", "codex": "auto"}))
        github = FakeGitHub(issue())
        loop = Loop(settings, github, "operator", output=self.lines.append)
        loop.coordinator.clock = lambda: self.now
        loop.maintenance = self.manager
        loop.coordinator.runtime_available = self.manager.available
        loop.usage.record("claude", "five_hour", 90, self.now + 100, 18000)
        self.assertFalse(loop.tick())
        self.assertEqual(github.writes, [])
        self.assertEqual(loop.plans()[0].state, "waiting")
        version.write_text("repaired")
        with patch("ub_agents.loop.supervise", return_value=1) as run:
            self.assertTrue(loop.tick())
        self.assertEqual(run.call_args.args[0][0], str(self.bin / "codex"))
        self.assertTrue(loop.usage.paused("claude"))
        self.assertTrue(self.manager.available("codex"))
        self.assertEqual(self.manager.read(state_path)["checked"], checked)
        self.assertEqual(len(self.updates()), 2)

    def test_supervision_inherits_run_lock_while_observing_usage(self):
        self.npm("claude")
        settings = self.settings("claude")
        usage = RuntimeUsage(self.root, lambda: self.now, self.lines.append)
        run_dir = self.root / "run"
        output = UsageOutput("claude", run_dir, usage, {})
        observed = []

        def observe(final=False):
            output.poll(final)
            if usage.paused("claude"):
                observed.append(final)
                self.manager_for().boundary(settings)
                self.assertEqual(self.updates(), [])

        with self.manager.reserve("claude") as reserved:
            event = {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed_warning", "unifiedWindows": {"five_hour": {
                    "utilization": .9, "resetsAt": self.now + 100}}}}
            script = (f"import os, time; os.fstat({reserved.descriptor}); "
                      f"print({json.dumps(event)!r}, flush=True); "
                      "time.sleep(.4); print('finished')")
            self.assertEqual(supervise([sys.executable, "-c", script], self.root,
                                      os.environ.copy(), run_dir, 3, threading.Event(),
                                      process_started=lambda pid: reserved.started(),
                                      pass_fds=(reserved.descriptor,), observe_output=observe), 0)
        self.assertIn(False, observed)
        self.assertIn("finished", (run_dir / "process.log").read_text())
        self.manager.boundary(settings)
        self.assertEqual(len(self.updates()), 1)
        self.assertTrue(usage.paused("claude"))

    def test_start_reservation_keeps_guard_until_process_started(self):
        self.native()
        second = self.manager_for()
        with self.manager.reserve("claude") as reserved:
            self.assertTrue(self.manager.available("claude"))
            self.assertTrue(second.available("claude"))
            second.boundary(self.settings("claude"))
            self.assertEqual(self.calls, [])
            reserved.started()
            second.boundary(self.settings("claude"))
            self.assertEqual(len(self.updates()), 1)

    def test_concurrent_start_reservations_defer_maintenance_until_both_start(self):
        self.native()
        second, updater = self.manager_for(), self.manager_for()
        settings = self.settings("claude")
        with self.manager.reserve("claude") as first:
            with second.reserve("claude") as other:
                self.assertIsNotNone(first)
                self.assertIsNotNone(other)
                self.assertTrue(updater.available("claude"))
                updater.boundary(settings)
                self.assertEqual(self.calls, [])
                first.started()
                updater.boundary(settings)
                self.assertEqual(self.calls, [])
                other.started()
                updater.boundary(settings)
                self.assertEqual(len(self.updates()), 1)

    def test_read_only_availability_guard_does_not_block_start_reservation(self):
        self.native()
        second = self.manager_for()
        state_path = self.manager.paths(installation("claude", self.which))[2]
        self.manager.root.mkdir()
        self.manager.paths(installation("claude", self.which))[0].touch()
        read = self.manager.read
        def read_while_starting(path):
            if path == state_path:
                with second.reserve("claude") as reserved:
                    self.assertIsNotNone(reserved)
            return read(path)
        with patch.object(self.manager, "read", side_effect=read_while_starting):
            self.assertTrue(self.manager.available("claude"))

    def test_healthy_cooldown_boundary_does_not_exclude_concurrent_starts(self):
        self.native()
        settings = self.settings("claude")
        self.manager.boundary(settings)
        second = self.manager_for()
        read = self.manager.read
        def read_while_starting(path):
            with second.reserve("claude") as reserved:
                self.assertIsNotNone(reserved)
            return read(path)
        with patch.object(self.manager, "read", side_effect=read_while_starting):
            self.manager.boundary(settings)
        self.assertEqual(len(self.updates()), 1)

    def test_concurrent_completed_check_is_rechecked_under_guard(self):
        self.npm()
        settings = self.settings()
        second = self.manager_for()
        read = self.manager.read
        reads = 0
        def complete_check_after_read(path):
            nonlocal reads
            state = read(path)
            reads += 1
            if reads == 1:
                second.boundary(settings)
            return state
        with patch.object(self.manager, "read", side_effect=complete_check_after_read):
            self.manager.boundary(settings)
        self.assertEqual(len(self.updates()), 1)

    def test_reservation_race_reports_waiting_without_claiming(self):
        stub_refresh(self)
        self.native()
        settings = self.settings("claude")
        github = FakeGitHub(issue())
        loop = Loop(settings, github, "operator", output=self.lines.append)
        loop.maintenance = self.manager
        loop.coordinator.runtime_available = self.manager.available
        plan = loop.plans()[0]
        self.assertEqual(plan.state, "ready")
        guard = self.manager.paths(installation("claude", self.which))[0]
        self.manager.boundary(settings)
        reserve = self.manager.reserve
        @contextmanager
        def maintenance_wins(cli):
            with lock(guard):
                with reserve(cli) as reservation:
                    yield reservation
        with patch.object(self.manager, "reserve", side_effect=maintenance_wins):
            self.assertFalse(loop.execute(plan))
        self.assertEqual(github.writes, [])
        self.assertIn("waiting", self.lines[-1])
        self.assertIn("claude runtime became unavailable before the claim", self.lines[-1])

    def test_sigterm_and_sigint_during_maintenance_exit_without_claim(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from ub_agents.cli import main
        self.executable(self.bin / "codex")
        for sig, expected in ((signal.SIGTERM, 0), (signal.SIGINT, 130)):
            with self.subTest(signal=sig):
                self.now += COOLDOWN_SECONDS
                updater = self.root / "signal-updater"
                updater.write_text(f"#!{sys.executable}\nimport os,signal,time\nos.kill(os.getppid(), {int(sig)})\ntime.sleep(60)\n")
                updater.chmod(0o755)
                settings = self.settings(policy=(str(updater),))
                github = FakeGitHub(issue())
                manager = self.manager_for()
                manager.runner = manager._run
                def create_loop(config, gh, actor, stop, **kwargs):
                    loop = Loop(config, gh, actor, stop, **kwargs)
                    loop.maintenance = manager
                    loop.coordinator.runtime_available = manager.available
                    return loop
                with patch("ub_agents.cli.load_config", return_value=settings), \
                        patch("ub_agents.cli.GitHub", return_value=github), \
                        patch("ub_agents.cli.Loop", side_effect=create_loop), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.root / "ub-agent.yaml"), "launch", "--once"]), expected)
                self.assertEqual(github.writes, [])
                state = manager.read(manager.paths(installation("codex", self.which))[2])
                self.assertEqual(state["checked"], self.now)
                self.assertEqual(state["result"], "failed")
                self.assertTrue(state["usable"])

    def test_broken_changed_next_path_is_reprobed_without_retrying_updater(self):
        self.npm()
        broken = self.executable(self.root / "broken")
        Path(str(broken) + ".version").unlink()
        self.action = lambda: self.link("codex", broken)
        self.manager.boundary(self.settings())
        self.assertFalse(self.manager_for().available("codex"))
        count = len(self.calls)
        self.manager_for().boundary(self.settings())
        self.assertEqual(self.calls[count:], [(str(self.bin / "codex"), "--version")])
        self.assertFalse(self.manager.available("codex"))
        self.assertEqual(len(self.updates()), 1)

    def test_failed_health_probe_recovers_before_update_cooldown_expires(self):
        target = self.npm()
        settings = self.settings()
        version = Path(str(target) + ".version")
        self.action = version.unlink
        self.manager.boundary(settings)
        state_path = self.manager.paths(installation("codex", self.which))[2]
        failed = self.manager.read(state_path)
        self.assertFalse(failed["usable"])
        self.now += 60
        version.write_text("2.0")
        repaired = self.manager_for()
        repaired.boundary(settings)
        self.assertTrue(repaired.available("codex"))
        with repaired.reserve("codex") as reserved:
            self.assertIsNotNone(reserved)
        recovered = repaired.read(state_path)
        self.assertEqual(recovered, failed | {"usable": True, "version": "2.0"})
        self.assertIn("recovered", self.lines[-1])
        self.assertEqual(len(self.updates()), 1)
        count = len(self.calls)
        repaired.boundary(settings)
        self.assertEqual(len(self.calls), count)
        self.now = failed["checked"] + COOLDOWN_SECONDS
        self.action = lambda: None
        repaired.boundary(settings)
        self.assertEqual(len(self.updates()), 2)

    def test_transient_version_timeout_recovers_at_next_boundary(self):
        self.npm()
        probes = 0
        def transient_timeout(command, env, timeout, cancellable=True):
            nonlocal probes
            if command[-1] == "--version":
                probes += 1
                if probes == 2:
                    raise MaintenanceFailure("timed out after 5s")
            return self.run_command(command, env, timeout, cancellable)
        self.manager.runner = transient_timeout
        settings = self.settings()
        self.manager.boundary(settings)
        self.assertFalse(self.manager.available("codex"))
        state_path = self.manager.paths(installation("codex", self.which))[2]
        checked = self.manager.read(state_path)["checked"]
        # The failed probe needs no operator repair or update retry.
        self.manager_for().boundary(settings)
        self.assertTrue(self.manager.available("codex"))
        self.assertEqual(self.manager.read(state_path)["checked"], checked)
        self.assertEqual(len(self.updates()), 1)

    def test_opt_out_launchers_can_recover_shared_failed_health(self):
        target = self.npm()
        version = Path(str(target) + ".version")
        settings = self.settings()
        for policy in (None, RuntimeUpdates({"codex": "off"})):
            with self.subTest(policy=policy):
                version.write_text("1.0")
                self.action = version.unlink
                self.now += COOLDOWN_SECONDS
                self.manager.boundary(settings)
                state_path = self.manager.paths(installation("codex", self.which))[2]
                checked = self.manager.read(state_path)["checked"]
                self.assertFalse(self.manager.available("codex"))
                version.write_text("1.0")
                repaired = self.manager_for()
                repaired.boundary(replace(settings, runtime_updates=policy))
                self.assertTrue(repaired.available("codex"))
                self.assertEqual(repaired.read(state_path)["checked"], checked)

    def test_cached_auto_skip_can_recover_failed_health_without_shared_cooldown(self):
        target = self.executable(self.bin / "codex")
        version = Path(str(target) + ".version")
        self.action = version.unlink
        self.manager.boundary(self.settings(policy=("custom-updater", "codex")))
        state_path = self.manager.paths(installation("codex", self.which))[2]
        failed = self.manager.read(state_path)
        self.assertFalse(failed["usable"])
        self.now += COOLDOWN_SECONDS
        self.manager.boundary(self.settings())
        self.assertIn("skipped", self.lines[-1])
        self.assertEqual(self.manager.read(state_path), failed)
        version.write_text("2.0")
        self.manager.boundary(self.settings())
        self.assertTrue(self.manager.available("codex"))
        self.assertEqual(self.manager.read(state_path), failed | {"usable": True, "version": "2.0"})
        self.assertEqual(self.calls.count(("custom-updater", "codex")), 1)

    def test_health_recovery_defers_while_maintenance_holds_guard(self):
        target = self.npm()
        version = Path(str(target) + ".version")
        self.action = version.unlink
        settings = self.settings()
        self.manager.boundary(settings)
        guard, _, state_path = self.manager.paths(installation("codex", self.which))
        version.write_text("1.0")
        count = len(self.calls)
        with lock(guard) as held:
            self.assertIsNotNone(held)
            self.manager_for().boundary(settings)
            self.manager_for().boundary(replace(settings, runtime_updates=None))
            self.assertEqual(len(self.calls), count)
            self.assertFalse(self.manager.read(state_path)["usable"])
        self.manager_for().boundary(settings)
        self.assertTrue(self.manager.available("codex"))

    def test_read_only_availability_creates_no_state(self):
        self.executable(self.bin / "codex")
        self.assertTrue(self.manager.available("codex"))
        self.assertFalse(self.manager.root.exists())

    def test_state_location_is_per_user_and_respects_absolute_xdg_path(self):
        for configured in ("", "relative", str(self.root / "xdg-state")):
            with self.subTest(configured=configured), patch.dict(os.environ, {"XDG_STATE_HOME": configured}):
                base = Path(configured) if configured and Path(configured).is_absolute() else self.home / ".local/state"
                self.assertEqual(state_directory(), base / "ub-agent/runtime-updates")

    def test_orphan_updater_retains_guard_until_it_finishes(self):
        self.executable(self.bin / "codex")
        entered, finish = self.root / "updater-started", self.root / "finish-updater"
        updater = self.root / "orphan-updater"
        updater.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
                           f"Path({str(entered)!r}).touch()\n"
                           f"while not Path({str(finish)!r}).exists(): time.sleep(0.01)\n")
        updater.chmod(0o755)
        script = f'''import os, threading, time
from pathlib import Path
from types import SimpleNamespace
from ub_agents.config import Runtime, RuntimeUpdates
from ub_agents.runtime_updates import RuntimeMaintenance
manager = RuntimeMaintenance(output=lambda *_: None, root=Path({str(self.manager.root)!r}))
settings = SimpleNamespace(runtime_updates=RuntimeUpdates({{"codex": ({str(updater)!r},)}}),
                           agents=[SimpleNamespace(runtimes=[Runtime("codex", "model", "high")])])
def crash_when_updater_starts():
    while not Path({str(entered)!r}).exists(): time.sleep(0.01)
    os._exit(1)
threading.Thread(target=crash_when_updater_starts, daemon=True).start()
manager.boundary(settings)
'''
        parent = subprocess.Popen([sys.executable, "-c", script])
        guard = self.manager.paths(installation("codex", self.which))[0]
        try:
            self.assertEqual(parent.wait(timeout=10), 1)
            self.assertTrue(entered.exists())
            self.assertFalse(self.manager.available("codex"))
            self.manager.boundary(self.settings())
            self.assertEqual(self.calls, [])
        finally:
            finish.touch()
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and guard.exists():
                with lock(guard) as released:
                    if released is not None:
                        break
                time.sleep(0.01)
        self.assertTrue(self.manager.available("codex"))
        self.manager.boundary(self.settings(policy=(str(updater),)))
        self.assertEqual(self.calls.count((str(updater),)), 1)

    def test_npm_guard_survives_temporarily_missing_package_metadata(self):
        target = self.npm()
        second = self.manager_for()
        def replace_package():
            (target.parent.parent / "package.json").unlink()
            self.assertFalse(second.available("codex"))
            before = list(self.calls)
            second.boundary(self.settings())
            self.assertEqual(before, self.calls)
            with second.reserve("codex") as reserved:
                self.assertIsNone(reserved)
        self.action = replace_package
        self.manager.boundary(self.settings())
        self.assertTrue(second.available("codex"))
        count = len(self.calls)
        second.boundary(self.settings())
        self.assertEqual(len(self.calls), count)
