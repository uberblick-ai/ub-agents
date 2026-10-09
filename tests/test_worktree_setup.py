from dataclasses import replace
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ub_agents.cleanup import Cleaner
from ub_agents.config import CheckoutSetup, instruction_text
from ub_agents.errors import CleanupError, LostOwnership
from ub_agents.execution import git, group_members, supervise
from ub_agents.loop import Loop
from ub_agents.records import iso, timestamp
from ub_agents.worktree_setup import confirm_worktree_setup_stopped
from tests.support import FakeGitHub, agent, config, edit_lease, issue, pr, stub_refresh
from tests import test_refresh


class WorktreeSetupTests(unittest.TestCase):
    def setUp(self):
        refresh = stub_refresh(self)
        refresh.side_effect = lambda cfg, role, github, **kwargs: instruction_text(
            cfg.root, role.instructions, f"{role.name} instructions")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        git(self.root, "init", "-b", "main")
        (self.root / ".gitignore").write_text(".ub-agents/\nnode_modules/\n")
        (self.root / "lockfile").write_text("control dependencies")
        test_refresh.RefreshTests.commit(self, self.root)
        git(self.root, "remote", "add", "origin", str(self.root))
        self.github = FakeGitHub(issue())
        self.role = agent(self.root, worktree=True, kind="issue")
        self.lines = []
        self.observer = Mock()

    def loop(self, script=None, timeout=3, *, configured=True):
        script = script or ("from pathlib import Path; import sys; "
                            "Path('node_modules').mkdir(); "
                            "Path('node_modules/installed').write_text(sys.argv[1]); "
                            "print('install output')")
        setting = CheckoutSetup((sys.executable, "-c", script, "literal;argument"), ("lockfile",), timeout)
        cfg = replace(config(self.root, self.role), checkout_setup=setting if configured else None)
        self.current = Loop(cfg, self.github, "operator", output=self.lines.append,
                            observer=self.observer, interrupt_event=threading.Event())
        return self.current

    def execute(self, loop, callback=None):
        def execution(command, cwd, env, run_dir, *args, **kwargs):
            if callback:
                callback(cwd, env, run_dir)
            lease = next(r for r in reversed(loop.coordinator.history(int(env["UB_AGENTS_ASSIGNMENT"])))
                         if r["kind"] == "lease")
            loop.coordinator.report(lease, "success", "done", outcome="done")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execution) as execution:
            self.assertTrue(loop.tick())
        execution.assert_called_once()

    def artifacts(self):
        lease = self.current.coordinator.history(next(iter(self.github.items)))[0]
        directory = self.root / ".ub-agents" / "runs" / lease["run"]
        worktree = self.root / ".ub-agents" / "worktrees" / lease["run"]
        return lease, directory, worktree

    def test_each_new_private_worktree_installs_before_agent_without_watched_changes(self):
        loop = self.loop()
        def installed(cwd, env, run_dir):
            self.assertNotEqual(cwd, self.root)
            self.assertEqual((cwd / "node_modules/installed").read_text(), "literal;argument")
            directory = run_dir / "checkout-setup"
            self.assertEqual((directory / "process.log").read_text(), "install output\n")
            self.assertEqual((directory / "stopped").read_text(), "confirmed\n")
            self.assertFalse((run_dir / "process.log").exists())
            lease = loop.coordinator.history(int(env["UB_AGENTS_ASSIGNMENT"]))[0]
            self.assertEqual(lease["process_group"], int((directory / "pid").read_text()))
            self.assertEqual(group_members(lease["process_group"]), [])
        with patch("ub_agents.worktree_setup.supervise", wraps=supervise) as setup:
            self.execute(loop, installed)
            self.github.items[3] = issue(3)
            self.execute(loop, installed)
        self.assertEqual(setup.call_count, 2)
        self.observer.activity.assert_any_call("checkout setup running in worktree")
        self.assertEqual(sum(line.startswith("checkout setup running in worktree") for line in self.lines), 2)
        self.assertNotIn("install output", self.lines)
        self.assertFalse((self.root / "node_modules").exists())
        self.assertEqual(list((self.root / ".ub-agents/worktrees").iterdir()), [])

    def test_no_extra_setup_for_shared_checkout_or_missing_configuration(self):
        for private, configured in ((False, True), (True, False), (False, False)):
            with self.subTest(private=private, configured=configured):
                self.github = FakeGitHub(issue())
                self.role = replace(self.role, worktree=private)
                loop = self.loop(configured=configured)
                with patch("ub_agents.worktree_setup.supervise") as setup:
                    self.execute(loop)
                setup.assert_not_called()

    def test_setup_uses_control_environment_timeout_and_inherits_run_locks(self):
        loop = self.loop(timeout=7)
        def setup(command, cwd, env, directory, timeout, stop, **kwargs):
            self.assertEqual(env, {k: v for k, v in os.environ.items() if not k.startswith("UB_AGENTS_")})
            self.assertEqual(timeout, 7)
            self.assertIs(stop, loop.interrupt_event)
            self.assertIsInstance(command, list)
            self.assertEqual(len(kwargs["pass_fds"]), 1)
            for descriptor in kwargs["pass_fds"]:
                os.fstat(descriptor)
            self.assertGreater(kwargs["expires"](), timestamp())
            command[2] = "import os; " + "; ".join(
                f"os.fstat({descriptor})" for descriptor in kwargs["pass_fds"]) + "; " + command[2]
            return supervise(command, cwd, env, directory, timeout, stop, **kwargs)
        with patch("ub_agents.worktree_setup.supervise", side_effect=setup):
            self.execute(loop)

    def test_reviewer_installs_candidate_lockfile_using_control_setup_command(self):
        base = git(self.root, "rev-parse", "HEAD")
        (self.root / "lockfile").write_text("candidate dependencies")
        test_refresh.RefreshTests.commit(self, self.root)
        head = git(self.root, "rev-parse", "HEAD")
        git(self.root, "update-ref", "refs/pull/2/head", head)
        git(self.root, "reset", "--hard", base)
        self.github = FakeGitHub(pr(head=head))
        self.role = replace(self.role, kind="pr")
        loop = self.loop("from pathlib import Path; Path('node_modules').mkdir(); "
                         "Path('node_modules/installed').write_text(Path('lockfile').read_text())")
        def installed(cwd, env, run_dir):
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), head)
            self.assertEqual((cwd / "node_modules/installed").read_text(), "candidate dependencies")
        self.execute(loop, installed)
        self.assertEqual((self.root / "lockfile").read_text(), "control dependencies")

    def test_nonzero_start_failure_and_timeout_end_run_before_agent_and_remove_tree(self):
        for mode, detail in (("nonzero", "exited 4"), ("start", "Cannot start configured execution"),
                             ("timeout", "timed out after 0.1 seconds")):
            with self.subTest(mode=mode):
                self.github = FakeGitHub(issue())
                loop = self.loop("import sys; sys.exit(4)" if mode == "nonzero" else
                                 "import time; time.sleep(30)", timeout=0.1 if mode == "timeout" else 3)
                if mode == "start":
                    loop.config = replace(loop.config, checkout_setup=replace(loop.config.checkout_setup,
                                                                             command=("/missing/install",)))
                with patch("ub_agents.loop.supervise") as execution:
                    self.assertTrue(loop.tick())
                execution.assert_not_called()
                lease, directory, worktree = self.artifacts()
                self.assertEqual((lease["state"], lease["result"]), ("released", "retry"))
                self.assertIn(detail, lease["summary"])
                self.assertIn(str(directory / "checkout-setup/process.log"), lease["summary"])
                self.assertFalse(worktree.exists())
                self.assertFalse((directory / "process.log").exists())
                self.assertEqual((directory / "checkout-setup/stopped").read_text(), "confirmed\n")
                if mode != "start":
                    self.assertEqual(group_members(lease["process_group"]), [])

    def test_interrupt_confirms_group_ends_and_records_setup_log(self):
        loop = self.loop("import time; time.sleep(30)")
        def setup(*args, **kwargs):
            started = kwargs["process_started"]
            def interrupt(pid):
                started(pid)
                loop.interrupt_event.set()
            kwargs["process_started"] = interrupt
            return supervise(*args, **kwargs)
        with patch("ub_agents.worktree_setup.supervise", side_effect=setup), \
                patch("ub_agents.loop.supervise") as execution, self.assertRaises(KeyboardInterrupt):
            loop.tick()
        execution.assert_not_called()
        lease, directory, worktree = self.artifacts()
        self.assertEqual((lease["state"], lease["result"], lease["attempt_effect"]),
                         ("released", "retry", "unchanged"))
        self.assertIn("interrupted", lease["summary"])
        self.assertIn(str(directory / "checkout-setup/process.log"), lease["summary"])
        self.assertEqual(group_members(lease["process_group"]), [])
        self.assertFalse(worktree.exists())

    def test_item_checks_are_repeated_after_setup(self):
        for change, detail in (({"labels": frozenset({"ready", "needs-human"})}, "Stop label"),
                               ({"labels": frozenset()}, "State or trigger"),
                               ({"state": "closed"}, "State or trigger"),
                               ({"head": "b" * 40}, "Candidate changed")):
            with self.subTest(change=change):
                self.github = FakeGitHub(issue())
                loop = self.loop()
                def setup(*args, **kwargs):
                    code = supervise(*args, **kwargs)
                    self.github.change(1, **change)
                    return code
                with patch("ub_agents.worktree_setup.supervise", side_effect=setup), \
                        patch("ub_agents.loop.supervise") as execution:
                    self.assertTrue(loop.tick())
                execution.assert_not_called()
                lease, directory, worktree = self.artifacts()
                self.assertIn(detail, lease["summary"])
                self.assertFalse(worktree.exists())

    def test_uncertain_setup_stop_keeps_ownership_scratch_and_worktree(self):
        loop = self.loop()
        def uncertain(command, cwd, env, directory, *args, **kwargs):
            (directory / "pid").write_text("123456")
            raise CleanupError("cannot inspect process group", next_step="make ps usable")
        with patch("ub_agents.worktree_setup.supervise", side_effect=uncertain), \
                patch("ub_agents.loop.supervise") as execution, self.assertRaises(CleanupError):
            loop.tick()
        execution.assert_not_called()
        lease, directory, worktree = self.artifacts()
        self.assertEqual((lease["state"], lease["cleanup"]), ("running", "unconfirmed"))
        self.assertIn("checkout-setup/process.log", lease["summary"])
        self.assertTrue(worktree.exists())
        self.assertFalse((directory / "checkout-setup/stopped").exists())

    def test_ownership_loss_stops_setup_before_cleanup_without_release_writes(self):
        loop = self.loop("import time; time.sleep(30)")
        def setup(*args, **kwargs):
            def lost(pid):
                self.writes = list(self.github.writes)
                raise LostOwnership("lost setup ownership")
            kwargs["process_started"] = lost
            return supervise(*args, **kwargs)
        with patch("ub_agents.worktree_setup.supervise", side_effect=setup), \
                patch("ub_agents.loop.supervise") as execution, self.assertRaises(LostOwnership):
            loop.tick()
        execution.assert_not_called()
        lease, directory, worktree = self.artifacts()
        self.assertEqual(self.github.writes, self.writes)
        self.assertEqual(lease["state"], "running")
        self.assertEqual(group_members(int((directory / "checkout-setup/pid").read_text())), [])
        self.assertFalse(worktree.exists())

    def crashed(self, *, outcome=False):
        loop = self.loop()
        loop.coordinator.clock = lambda: timestamp() - 2
        lease = loop.coordinator.claim(loop.plans()[0])
        loop.coordinator.update(lease, state="running", started=True, host=socket.gethostname(),
                                process_group=123456)
        if outcome:
            loop.coordinator.report(lease, "success", "done", outcome="done")
        edit_lease(self.github, lease, expires=iso(timestamp() - 1))
        loop.coordinator.clock = timestamp
        directory = self.root / ".ub-agents/runs" / lease["run"] / "checkout-setup"
        directory.mkdir(parents=True)
        (directory / "pid").write_text("123456")
        return loop, directory

    def test_crashed_setup_blocks_later_claim_and_outcome_recovery_until_group_ends(self):
        for outcome in (False, True):
            with self.subTest(outcome=outcome):
                self.github = FakeGitHub(issue())
                loop, directory = self.crashed(outcome=outcome)
                writes = list(self.github.writes)
                with patch("ub_agents.worktree_setup.group_members", return_value=[123456]), \
                        self.assertRaisesRegex(CleanupError, "setup process group 123456 is still present"):
                    loop.tick()
                self.assertEqual(self.github.writes, writes)
                with patch("ub_agents.worktree_setup.group_members", return_value=[]):
                    if outcome:
                        self.assertTrue(loop.tick())
                    else:
                        self.execute(loop)

    def test_crash_records_need_valid_pid_or_confirmed_stop(self):
        loop, directory = self.crashed()
        for value in (None, "invalid", "0", "-1"):
            with self.subTest(pid=value):
                (directory / "pid").unlink(missing_ok=True)
                if value is not None:
                    (directory / "pid").write_text(value)
                with self.assertRaises(CleanupError):
                    confirm_worktree_setup_stopped(loop.config, directory.parent.name)
        (directory / "stopped").write_text("confirmed\n")
        with patch("ub_agents.worktree_setup.group_members", side_effect=AssertionError("reused pid")):
            confirm_worktree_setup_stopped(loop.config, directory.parent.name)
        (directory / "stopped").write_text("partial")
        with self.assertRaisesRegex(CleanupError, "unreadable setup stop record"):
            confirm_worktree_setup_stopped(loop.config, directory.parent.name)

    def test_stop_check_accepts_an_aliased_root_but_not_redirected_records(self):
        loop, directory = self.crashed()
        (directory / "stopped").write_text("confirmed\n")
        alias = Path(tempfile.mkdtemp()) / "alias"
        self.addCleanup(lambda: alias.unlink())
        alias.symlink_to(self.root)
        aliased = SimpleNamespace(root=alias)
        confirm_worktree_setup_stopped(aliased, directory.parent.name)
        (directory / "stopped").unlink()
        (directory / "stopped").symlink_to(directory / "pid")
        with self.assertRaisesRegex(CleanupError, "redirected setup process record"):
            confirm_worktree_setup_stopped(aliased, directory.parent.name)
        (directory / "stopped").unlink()
        target = directory.with_name("elsewhere")
        directory.rename(target)
        directory.symlink_to(target)
        with self.assertRaisesRegex(CleanupError, "redirected setup diagnostics"):
            confirm_worktree_setup_stopped(aliased, directory.parent.name)

    def test_cleaner_keeps_crashed_setup_worktree_until_termination_is_confirmed(self):
        loop, directory = self.crashed()
        lease = loop.coordinator.history(1)[0]
        worktree = self.root / ".ub-agents/worktrees" / lease["run"]
        git(self.root, "worktree", "add", "--detach", str(worktree), "HEAD")
        cleaner = Cleaner(loop.config, self.github, "operator", output=self.lines.append)
        with patch("ub_agents.worktree_setup.group_members", return_value=[123456]), \
                patch("ub_agents.cleanup.group_members", return_value=[]):
            rows = cleaner.clean(apply=True)
        self.assertEqual(rows[0]["action"], "kept")
        self.assertIn("setup process group", rows[0]["reason"])
        self.assertTrue(worktree.exists())
        with patch("ub_agents.worktree_setup.group_members", return_value=[]), \
                patch("ub_agents.cleanup.group_members", return_value=[]):
            self.assertEqual(cleaner.clean(apply=True)[0]["action"], "removed")
