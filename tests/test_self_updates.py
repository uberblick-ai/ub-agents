from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ub_agents import __version__
from ub_agents.cli import main
from ub_agents.config import RuntimeUpdates, load_config
from ub_agents.doctor import diagnose
from ub_agents.launcher_lock import running_launcher
from ub_agents.loop import Loop
from ub_agents.runtime_updates import MaintenanceFailure
from ub_agents.self_updates import (BrewInstallation, FORMULA, RestartLauncher, SelfUpdates,
                                    installation, release_range)
from tests.support import FakeGitHub, config, isolate_runtime_state, issue


def release(tag, marker="independent", **fields):
    return dict(tag_name=tag, body=f"<!-- ub-agents:launcher-upgrade={marker} -->",
                draft=False, prerelease=False) | fields


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.install = BrewInstallation(self.root / "brew", self.root / "brew/Cellar/ub-agents")
        self.install.cellar.mkdir(parents=True)
        Path(self.install.brew).parent.mkdir()
        Path(self.install.brew).touch()
        self.current = __version__
        self.target = "99.0.2"
        self.put_keg(self.current)
        self.pages = [[release("99.0.1")], [release(self.target)]]
        self.stop = threading.Event()
        self.lines, self.calls = [], []
        self.settings = replace(config(self.root), runtime_updates=RuntimeUpdates({"ub-agents": "auto"}, 17))
        self.updates = Mock(release_check=(1, self.target), banner={"text": "New release available"})
        self.after_upgrade = lambda: self.put_keg(self.target)
        self.clock = 0
        self.manager = SelfUpdates(self.lines.append, self.stop, detect=lambda: (self.install, None),
                                   runner=self.run_command, clock=lambda: self.clock)

    def put_keg(self, value, tap="uberblick-ai/tap"):
        keg = self.install.cellar / value
        (keg / "bin").mkdir(parents=True, exist_ok=True)
        (keg / "bin/ub-agents").touch()
        (keg / "INSTALL_RECEIPT.json").write_text(json.dumps({"source": {"tap": tap}}))
        opt = self.install.prefix / "opt/ub-agents"
        opt.parent.mkdir(exist_ok=True)
        opt.unlink(missing_ok=True)
        opt.symlink_to(keg, target_is_directory=True)
        self.current = value
        return keg

    def run_command(self, args, env, timeout, cancellable=True):
        self.assertFalse(cancellable)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 17)
        self.calls.append(args)
        if args[-1] == "--version":
            return "ub-agents " + self.current
        if args[-1] == "--prefix":
            return str(self.install.prefix)
        if args[0] == "gh":
            self.assertIn("--paginate", args)
            self.assertIn("--slurp", args)
            return json.dumps(self.pages)
        if "info" in args:
            return json.dumps({"formulae": [{"name": "ub-agents", "tap": "uberblick-ai/tap",
                                               "versions": {"stable": self.target}}]})
        if "upgrade" in args:
            self.assertEqual(args, [self.install.brew, "upgrade", "--formula", FORMULA])
            self.assertEqual(env["HOMEBREW_NO_INSTALL_CLEANUP"], "1")
            self.assertEqual(env["HOMEBREW_NO_AUTO_UPDATE"], "1")
            self.after_upgrade()
        return ""

    def boundary(self):
        self.manager.boundary(self.settings, self.updates)

    def test_owned_brew_only_and_doctor_reports_installation_offline(self):
        source = self.install.cellar / __version__ / "libexec/lib/python3.14/site-packages/ub_agents"
        self.assertEqual(installation(source), (self.install, None))
        self.put_keg(__version__, "other/tap")
        self.assertIn("not from", installation(source)[1])
        self.put_keg(__version__)
        (self.install.cellar / __version__ / "INSTALL_RECEIPT.json").write_text("invalid")
        self.assertIn("unreadable", installation(source)[1])
        for path in ("venv/lib/site-packages/ub_agents", "src/ub_agents", "Caskroom/ub-agents/1/ub_agents"):
            self.assertIn("only uberblick", installation(self.root / path)[1])
        path = self.root / "ub-agents.yaml"
        path.write_text("repository: org/project\nagents: {worker: {command: [echo], trigger: ready, outcomes: {done: {}}}}\n")
        with patch("ub_agents.self_updates.installation", return_value=(self.install, None)):
            result = diagnose(path, offline=True)
        check, = [entry for entry in result["checks"] if entry["id"] == "launcher-installation"]
        self.assertEqual(check["status"], "ok")
        self.assertIn("automatic updates: off", check["message"])

    def test_ub_agents_setting_only_accepts_auto_and_off(self):
        path = self.root / "ub-agents.yaml"
        base = "repository: org/project\nagents: {worker: {command: [echo], trigger: ready, outcomes: {done: {}}}}\n"
        for value in ("auto", "off"):
            path.write_text(base + f"runtime-updates: {{ub-agents: {value}}}\n")
            self.assertEqual(load_config(path).runtime_updates.policies["ub-agents"], value)
        for value in ("true", "false", "null", "[brew, upgrade]", "{command: [brew, upgrade, ub-agents]}"):
            path.write_text(base + f"runtime-updates: {{ub-agents: {value}}}\n")
            with self.subTest(value=value), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(path), "doctor", "--offline"]), 1)

    def test_upgrade_then_restart_and_daily_retry(self):
        with self.assertRaises(RestartLauncher) as result:
            self.boundary()
        self.assertEqual(result.exception.executable, self.install.current())
        self.assertIn("upgrade succeeded", "\n".join(self.lines))
        before = len(self.calls)
        self.boundary()
        self.assertEqual(len(self.calls), before)

    def test_a_brew_binary_owning_another_prefix_cannot_upgrade(self):
        original = self.manager.runner
        def wrong_prefix(args, *positional, **keywords):
            if args[-1] == "--prefix":
                return str(self.root / "different-homebrew")
            return original(args, *positional, **keywords)
        self.manager.runner = wrong_prefix
        self.boundary()
        self.assertFalse(any("update" in call or "upgrade" in call for call in self.calls))
        self.assertIn("owning Homebrew prefix", self.updates.banner["text"])

    def test_newer_install_from_peer_restarts_without_upgrading_even_when_latest_read_failed(self):
        self.put_keg(self.target)
        self.updates.release_check = (1, None)
        with self.assertRaises(RestartLauncher):
            self.boundary()
        self.assertFalse(any("update" in call or "upgrade" in call for call in self.calls))
        self.assertEqual(self.calls[-1][0], "gh")

    def test_peer_cleanup_of_startup_receipt_does_not_prevent_restart(self):
        source = self.install.cellar / __version__ / "libexec/site-packages/ub_agents"
        manager = SelfUpdates(self.lines.append, self.stop, detect=lambda: installation(source),
                              runner=self.run_command, clock=lambda: self.clock)
        self.put_keg(self.target)
        shutil.rmtree(self.install.cellar / __version__)
        self.assertIsNone(installation(source)[0])
        with self.assertRaises(RestartLauncher):
            manager.boundary(self.settings, self.updates)

    def test_unchanged_formula_or_successful_noop_does_not_restart(self):
        self.target = __version__
        self.boundary()
        self.assertFalse(any("upgrade" in call for call in self.calls))
        self.target = "99.0.2"
        self.updates.release_check = (2, self.target)
        self.after_upgrade = lambda: None
        self.boundary()
        self.assertIn("version unchanged", "\n".join(self.lines))

    def test_range_checks_all_pages_and_fails_closed(self):
        good = self.pages
        for pages in ([[release("99.0.1", "coordinated")], [release(self.target)]],
                      [[release("99.0.1", body="no marker"), release(self.target)]],
                      [[release(self.target, "unknown")]], [], {}, [[release("99.0.1")]],
                      [[release(self.target), release(self.target)]], [[{}]],
                      [[release(self.target, body="<!-- ub-agents:launcher-upgrade=independent -->" * 2)]]):
            with self.subTest(pages=pages):
                self.pages = pages
                self.manager.checked = None
                self.calls.clear()
                self.boundary()
                self.assertFalse(any("upgrade" in call for call in self.calls))
                self.assertIn("automatic update skipped", self.updates.banner["text"])
        release_range(good + [[release("100.0.0", "coordinated"), release("0.0.1", "coordinated")]],
                      __version__, self.target)

    def test_installed_marked_or_missing_release_does_not_restart(self):
        self.put_keg(self.target)
        for pages in ([[release(self.target, "coordinated")]], [[release("99.0.1")]]):
            self.pages = pages
            self.manager.checked = None
            self.boundary()
        self.assertFalse(any("upgrade" in call for call in self.calls))

    def test_failed_upgrade_is_logged_and_only_retried_next_update_check(self):
        def fail():
            raise MaintenanceFailure("command exited 1")
        self.after_upgrade = fail
        self.boundary()
        self.assertIn("failed — command exited 1", "\n".join(self.lines))
        before = len(self.calls)
        self.boundary()
        self.assertEqual(len(self.calls), before)
        self.updates.release_check = (2, self.target)
        self.after_upgrade = lambda: self.put_keg(self.target)
        with self.assertRaises(RestartLauncher):
            self.boundary()

    def test_upgrade_timeout_is_one_budget_and_shutdown_during_upgrade_finishes(self):
        def finish():
            self.stop.set()
            self.put_keg(self.target)
        self.after_upgrade = finish
        self.boundary()
        self.assertIn("exiting without restart", self.lines[-1])
        self.stop.clear()
        self.put_keg(__version__)
        self.manager.checked = None
        self.calls.clear()
        original = self.manager.runner
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock += 10
            return result
        self.manager.runner = slow
        self.boundary()
        self.assertIn("timed out after 17s", "\n".join(self.lines))
        self.assertFalse(any("upgrade" in call for call in self.calls))

    def test_both_queue_scopes_restart_before_claim_but_once_and_number_do_not(self):
        for agent_name in (None, "worker"):
            self.manager.checked = None
            self.put_keg(self.target)
            github = FakeGitHub(issue())
            loop = Loop(self.settings, github, "operator", output=self.lines.append)
            loop.self_updates, loop.updates = self.manager, self.updates
            with self.assertRaises(RestartLauncher):
                loop.launch(agent_name=agent_name)
            self.assertEqual(github.writes, [])
        for once, number in ((True, None), (False, 1)):
            loop = Loop(self.settings, FakeGitHub(), "operator", output=self.lines.append)
            loop.self_updates = Mock()
            loop.updates = self.updates
            with patch.object(loop, "tick_item", return_value=False):
                loop.launch(once=once, number=number)
            loop.self_updates.boundary.assert_not_called()

    def test_busy_boundaries_defer_and_graceful_stop_during_upgrade_claims_nothing(self):
        loop = Loop(self.settings, FakeGitHub(issue()), "operator", output=self.lines.append)
        loop._continuous = True
        loop.self_updates = Mock()
        for field in ("_refreshing_checkout", "_maintaining", "_finalizing"):
            setattr(loop, field, True)
            loop.maintain_launcher()
            setattr(loop, field, False)
        loop.github.lease = {"run": "owned"}
        loop.maintain_launcher()
        loop.self_updates.boundary.assert_not_called()
        loop.github.lease = None
        loop.self_updates, loop.updates = self.manager, self.updates
        def stop():
            loop.stop_gracefully()
            self.put_keg(self.target)
        self.after_upgrade = stop
        loop.launch()
        self.assertEqual(loop.github.writes, [])
        self.assertEqual(self.current, self.target)

    def test_exec_preserves_pid_arguments_and_waits_until_resources_are_closed(self):
        path = self.root / "ub-agents.yaml"
        path.touch()
        for mode in ([], ["--no-ui"]):
            view = Mock() if not mode else None
            args = ["--config", str(path), "launch", "--agent", "worker", *mode]
            restart = RestartLauncher("/new/ub-agents", self.stop, self.root)
            def execute(executable, arguments):
                self.assertIsNone(running_launcher(self.root))
                self.assertEqual(arguments, [executable, *args])
                if view is not None:
                    view.close.assert_called_once()
                raise RuntimeError("exec replaced the process")
            with patch("ub_agents.cli.load_config", return_value=self.settings), \
                    patch("ub_agents.cli.GitHub", return_value=FakeGitHub()), \
                    patch("ub_agents.cli.repository_checks", return_value=[]), \
                    patch("ub_agents.cli.check_launch_setup"), \
                    patch("ub_agents.launch_ui.open_view", return_value=view), \
                    patch("ub_agents.updates.Updates.start"), \
                    patch("ub_agents.startup_cleanup.StartupCleanup.start"), \
                    patch.object(Loop, "launch", side_effect=restart), \
                    patch("ub_agents.cli.os.execv", side_effect=execute), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "exec replaced"):
                    main(args)


if __name__ == "__main__":
    unittest.main()
