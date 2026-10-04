from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Runtime, load_config
from ub_agents.errors import AgentError, GitHubError
from ub_agents.execution import git
from ub_agents.loop import Loop
from ub_agents.records import attempts, timestamp
from ub_agents.refresh import refresh_checkout
from ub_agents.updates import Updates
from tests.support import FakeGitHub, PollGitHub, agent, config, isolate_runtime_state, issue, pr


class RefreshTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.origin, self.upstream, self.root = [base / p for p in ("origin.git", "upstream", "control")]
        git(base, "init", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", str(self.origin), str(self.upstream))
        (self.upstream / ".gitignore").write_text(".ub-agents/\nignored\n")
        (self.upstream / "role.md").write_text("First operator policy\n")
        (self.upstream / "ub-agents.yaml").write_text("repository: org/project\nagents:\n  worker:\n"
                                                    "    runtime: codex:model:high\n"
                                                    "    trigger: ready\n    instructions: role.md\n"
                                                    "    outcomes: {done: {}}\n")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        git(base, "clone", str(self.origin), str(self.root))
        self.worker = agent(self.root, kind="issue", instructions=self.root / "role.md",
                            command=(), runtimes=(Runtime("codex", "model", "high"),))
        self.github = FakeGitHub(issue(), issue(3))
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)

    def commit(self, root):
        git(root, "add", "-A")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "synthetic fixture")

    def push_policy(self, text="Second operator policy\n"):
        (self.upstream / "role.md").write_text(text)
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")

    def test_update_notice_counts_started_code_after_normal_fast_forwards(self):
        from tests.test_updates import eventually, stop_checker
        started = git(self.root, "rev-parse", "HEAD")
        update = Updates(self.root, detect=lambda _: 'checkout')
        self.addCleanup(stop_checker, update)
        update.start()
        self.assertTrue(eventually(lambda: update.started == started))
        self.assertIsNone(update.banner)
        for count in (1, 2):
            self.push_policy(f'Policy revision {count}\n')
            with patch('ub_agents.refresh.git', wraps=git) as calls:
                refresh_checkout(self.loop.config, self.github, on_fetch=update.fetched)
            self.assertEqual(sum('fetch' in call.args for call in calls.call_args_list), 1)
            self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), git(self.upstream, 'rev-parse', 'HEAD'))
            self.assertTrue(eventually(lambda: update.banner and f'{count} commits behind' in update.banner['text']))
            self.assertEqual(update.started, started)
        # A newly started launcher has the fast-forwarded code and no notice.
        restarted = Updates(self.root, detect=lambda _: 'checkout')
        self.addCleanup(stop_checker, restarted)
        restarted.start()
        self.assertTrue(eventually(lambda: restarted.started == git(self.root, 'rev-parse', 'HEAD')))
        refresh_checkout(self.loop.config, self.github, on_fetch=restarted.fetched)
        self.assertTrue(eventually(lambda: restarted.generation == 1))
        stop_checker(restarted)
        self.assertIsNone(restarted.banner)

    def snapshot(self):
        files = {}
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if relative.parts[0] in {".git", ".ub-agents"}:
                continue
            if path.is_symlink():
                files[str(relative)] = ("link", str(path.readlink()))
            elif path.is_file():
                files[str(relative)] = (path.stat().st_mode, path.read_bytes())
            else:
                files[str(relative)] = ("directory", path.stat().st_mode)
        return (git(self.root, "rev-parse", "--abbrev-ref", "HEAD"),
                git(self.root, "rev-parse", "HEAD"),
                (self.root / ".git" / "index").read_bytes(), files)

    def stopped(self, condition, once=True):
        before, writes = self.snapshot(), list(self.github.writes)
        with patch("ub_agents.loop.supervise") as execution, \
                patch("ub_agents.loop.Workspace.prepare") as prepare, \
                patch.object(self.loop.stop_event, "wait") as wait, \
                patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                self.assertRaisesRegex(AgentError, condition) as error:
            self.loop.launch(once=once)
        execution.assert_not_called()
        prepare.assert_not_called()
        wait.assert_not_called()
        self.assertIn("Fix the operator checkout", str(error.exception))
        self.assertIn("no assignment attempt was charged", str(error.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.github.writes, writes)
        self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])
        return str(error.exception)

    def execute(self, callback=None):
        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            if callback:
                callback(cwd, prompt)
            number = int(env["UB_AGENTS_ASSIGNMENT"])
            lease = self.loop.coordinator.history(number)[0]
            self.loop.coordinator.report(lease, "success", "Synthetic run finished", outcome="done")
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        for item in self.github.items.values():
            if self.loop.coordinator.history(item.number):
                self.assert_success(item.number)

    def assert_success(self, number):
        history = self.loop.coordinator.history(number)
        lease = next(record for record in reversed(history) if record["kind"] == "lease")
        self.assertEqual((lease["state"], lease["result"]), ("released", "success"))
        self.assertTrue(self.loop.coordinator.outcome(lease)["accepted"])

    def test_pushed_policy_appears_in_second_prompt_without_restart(self):
        prompts = []
        self.execute(lambda cwd, prompt: prompts.append(prompt))
        self.push_policy()
        self.execute(lambda cwd, prompt: prompts.append(prompt))
        self.assertIn("First operator policy", prompts[0])
        self.assertIn("Second operator policy", prompts[1])
        self.assertNotIn("First operator policy", prompts[1])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), git(self.upstream, "rev-parse", "HEAD"))
        self.assertIs(self.loop.config.agents[0], self.worker)

    def test_already_current_reads_current_file_without_merging(self):
        before = self.snapshot()
        (self.root / "ignored").write_text("ignored files do not prevent refresh")
        calls = []
        from ub_agents import refresh
        def record(root, *args, **kwargs):
            calls.append(args)
            return git(root, *args, **kwargs)
        with patch.object(refresh, "git", side_effect=record):
            self.execute(lambda cwd, prompt: self.assertIn("First operator policy", prompt))
        self.assertFalse(any("merge" in call for call in calls))
        self.assertEqual(self.snapshot()[:3], before[:3])

    def enable_reload(self):
        path = self.root / "ub-agents.yaml"
        self.loop.config = load_config(path)
        self.loop.config_path = path

    def test_sigterm_during_fast_forward_finishes_refresh_without_claiming(self):
        for reload in (False, True):
            with self.subTest(reload=reload):
                self.setUp()
                if reload:
                    self.enable_reload()
                self.push_policy()
                merges = []

                def refresh_git(root, *args, **kwargs):
                    if "merge" not in args:
                        return git(root, *args, **kwargs)
                    # Exercise subprocess.run's actual exception/child cleanup
                    # while a delayed merge receives SIGTERM from its child.
                    command = ["git", "-C", str(root), *args]
                    script = ("import os,signal,sys,time; "
                              "os.kill(os.getppid(), signal.SIGTERM); time.sleep(.2); "
                              "os.execvp('git', sys.argv[1:])")
                    result = subprocess.run([sys.executable, "-c", script, *command],
                                            capture_output=True, text=True, timeout=10, check=True)
                    merges.append(result.returncode)
                    return result.stdout.strip()

                def loop(config, github, actor, stop, **kwargs):
                    self.loop.stop_event = stop
                    self.loop.interrupt_event = kwargs["interrupt_event"]
                    return self.loop

                with patch("ub_agents.refresh.git", side_effect=refresh_git), \
                        patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                        patch("ub_agents.cli.GitHub", return_value=self.github), \
                        patch("ub_agents.cli.repository_checks", return_value=[]), \
                        patch("ub_agents.cli.Loop", side_effect=loop), \
                        patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                        patch("ub_agents.loop.supervise") as execution, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["launch"]), 0)
                execution.assert_not_called()
                self.assertEqual(merges, [0])
                self.assertEqual(git(self.root, "rev-parse", "HEAD"),
                                 git(self.upstream, "rev-parse", "HEAD"))
                self.assertEqual((self.root / "role.md").read_text(), "Second operator policy\n")
                self.assertEqual(git(self.root, "status", "--porcelain"), "")
                self.assertFalse((self.root / ".git" / "index.lock").exists())
                self.assertEqual(self.github.writes, [])

    def test_sigterm_during_failed_refresh_preserves_launcher_error(self):
        def fail(*args):
            signal.raise_signal(signal.SIGTERM)
            raise AgentError("Fast-forward failed")

        self.loop.interrupt_event = threading.Event()
        previous = signal.signal(signal.SIGTERM, lambda *_: self.loop.stop_gracefully())
        try:
            with patch("ub_agents.loop.refresh_instructions", side_effect=fail), \
                    patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                    self.assertRaisesRegex(AgentError, "Fast-forward failed"):
                self.loop.launch(once=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(self.github.writes, [])

    def test_reload_between_runs_changes_runtime_instructions_triggers_and_report_outcomes(self):
        self.enable_reload()
        calls = []

        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            number = int(env["UB_AGENTS_ASSIGNMENT"])
            calls.append(number)
            if number == 1:
                self.assertIn("First operator policy", prompt)
                self.assertIn("model", command)
                path = self.upstream / "ub-agents.yaml"
                path.write_text(path.read_text().replace("codex:model:high", "claude:next:low")
                                .replace("trigger: ready", "trigger: needs-changes")
                                .replace("instructions: role.md", "instructions: next.md")
                                .replace("{done: {}}", "{revised: {add: [needs-review]}}"))
                (self.upstream / "role.md").unlink()
                (self.upstream / "next.md").write_text("New policy and outcome")
                self.commit(self.upstream)
                git(self.upstream, "push", "origin", "main")
                # The active run still has its original declaration.
                name = "done"
            else:
                self.assertIn("next", command)
                self.assertIn("New policy and outcome", prompt)
                self.assertIn('"revised"', prompt)
                self.assertEqual(self.loop.config.agents[0].triggers, ("needs-changes",))
                name = "revised"
            with patch.dict("os.environ", env), patch("ub_agents.cli.GitHub", return_value=self.github), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(["report", "--outcome", name, "--summary", "Finished"]), 0)
            return 0

        self.github.change(3, labels=frozenset({"ready", "needs-changes"}))
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
            self.assertTrue(self.loop.tick())
        self.assertEqual(calls, [1, 3])
        self.assert_success(1)
        self.assert_success(3)
        self.assertEqual(self.github.item(3).labels, frozenset({"ready", "needs-review"}))

    def test_reloaded_config_can_withdraw_the_planned_assignment(self):
        for edit in (lambda text: text.replace("trigger: ready", "trigger: other"),
                     lambda text: text.replace("worker:", "replacement:"),
                     lambda text: text.replace("runtime: codex:model:high", "kind: pr\n    runtime: codex:model:high"),
                     lambda text: text + "queue: {milestones: gate}\n",
                     lambda text: text + "stop-labels: [paused]\n"):
            with self.subTest(edit=edit):
                self.setUp()
                self.enable_reload()
                self.github.change(1, labels=frozenset({"ready", "paused"}))
                self.github.change(3, labels=frozenset({"ready", "paused"}))
                self.github.milestones = [{"number": 1, "state": "open", "created_at": "2026-01-01T00:00:00Z"}]
                self.github.items[4] = issue(4, labels=(), milestone=1)
                path = self.upstream / "ub-agents.yaml"
                path.write_text(edit(path.read_text()))
                self.commit(self.upstream)
                git(self.upstream, "push", "origin", "main")
                with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                        patch("ub_agents.loop.supervise") as execution:
                    self.assertFalse(self.loop.tick())
                execution.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_reloaded_milestone_order_preserves_eligible_later_assignment(self):
        self.enable_reload()
        self.github.change(1, milestone=20)
        self.github.milestones = [{"number": n, "state": "open", "created_at": f"2026-01-{n:02}T00:00:00Z"}
                                  for n in (10, 20)]
        self.github.items[4] = issue(4, labels=(), milestone=10)
        path = self.upstream / "ub-agents.yaml"
        path.write_text(path.read_text() + "queue: {milestones: order}\n")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.execute()
        self.assert_success(1)
        self.assertEqual(self.loop.config.queue.milestones, "order")

    def test_reloaded_repository_is_checked_against_origin_before_claim(self):
        self.enable_reload()
        path = self.upstream / "ub-agents.yaml"
        path.write_text(path.read_text().replace("org/project", "org/other"))
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise") as execution, \
                self.assertRaisesRegex(AgentError, "origin must point to the configured GitHub repository"):
            self.loop.launch(once=True)
        execution.assert_not_called()
        self.assertEqual(self.github.writes, [])

    def test_invalid_reloaded_config_exits_with_check_error_without_charging_attempt(self):
        (self.upstream / "ub-agents.yaml").write_text("invalid: configuration\n")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        launch_error, check_error = io.StringIO(), io.StringIO()
        args = ["--config", str(self.root / "ub-agents.yaml")]
        with patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise") as execution, \
                redirect_stderr(launch_error):
            self.assertEqual(main(args + ["launch"]), 1)
        with redirect_stderr(check_error):
            self.assertEqual(main(args + ["check"]), 1)
        self.assertEqual(launch_error.getvalue(), check_error.getvalue())
        execution.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])

    def test_ignored_local_instructions_remain_valid_after_refresh(self):
        local = self.root / ".ub-agents" / "local-role.md"
        local.parent.mkdir()
        local.write_text("Local ignored operator policy")
        self.worker = replace(self.worker, instructions=local)
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
        self.push_policy()
        self.execute(lambda cwd, prompt: self.assertIn("Local ignored operator policy", prompt))

    def test_incoming_symlink_to_ignored_local_instructions_remains_valid(self):
        local = self.root / "ignored"
        local.write_text("Policy from local ignored file")
        (self.upstream / "role.md").unlink()
        (self.upstream / "role.md").symlink_to("ignored")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.execute(lambda cwd, prompt: self.assertIn("Policy from local ignored file", prompt))

    def test_incoming_cycle_through_ignored_symlink_stops_before_fast_forward(self):
        (self.root / "ignored").symlink_to("role.md")
        (self.upstream / "role.md").unlink()
        (self.upstream / "role.md").symlink_to("ignored")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("symlink loop")

    def test_modified_checkout_stops(self):
        (self.root / "role.md").write_text("Local edits")
        self.stopped("dirty")

    def test_continuous_launch_does_not_retry_local_refresh_failure(self):
        (self.root / "role.md").write_text("Local edits")
        self.stopped("dirty", once=False)

    def test_default_branch_read_retries_then_refreshes_before_claiming(self):
        errors = [GitHubError("GET", "repos/org/project", "HTTP 504", retryable=True),
                  GitHubError("GET", "repos/org/project", "request timed out", retryable=True),
                  GitHubError("GET", "repos/org/project", "HTTP 429 secondary rate limit", retryable=True, reset_at=1012,
                              rate_limited=True)]
        for failure in errors:
            with self.subTest(error=str(failure)):
                self.setUp()
                self.github = PollGitHub(issue())
                lines = []
                self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lines.append)
                self.loop.coordinator.clock = lambda: 1000
                self.push_policy()
                before = self.snapshot()
                self.github.read_results["default_branch"] = [failure]

                def wait(delay):
                    self.assertEqual(delay, 12 if failure.reset_at else 5)
                    self.assertEqual(self.github.writes, [])
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])

                def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
                    self.assertIn("Second operator policy", prompt)
                    self.assertEqual(git(self.root, "rev-parse", "HEAD"),
                                     git(self.upstream, "rev-parse", "HEAD"))
                    lease = self.loop.coordinator.history(1)[0]
                    self.loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    stop.set()
                    return 0

                with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                        patch("ub_agents.loop.timestamp", return_value=1000), \
                        patch.object(self.loop.stop_event, "wait", side_effect=wait) as waits, \
                        patch("ub_agents.loop.supervise", side_effect=run) as execution, \
                        self.assertRaises(KeyboardInterrupt):
                    self.loop.launch()
                waits.assert_called_once()
                execution.assert_called_once()
                self.assertEqual(sum(name == "default_branch" for name, _ in self.github.reads), 2)
                prefix = "GitHub rate limit reached" if failure.rate_limited else "Skipped"
                self.assertEqual(sum(line.startswith(prefix) for line in lines), 1)
                self.assertNotIn("Waiting for eligible GitHub work", lines)
                self.assert_success(1)

    def test_default_branch_read_stops_without_writes_for_permanent_error_or_once(self):
        cases = [(False, GitHubError("GET", "repos/org/project", "HTTP 401 Bad credentials")),
                 (False, GitHubError("GET", "repos/org/project", "Unreadable response")),
                 (True, GitHubError("GET", "repos/org/project", "HTTP 504", retryable=True))]
        for once, failure in cases:
            with self.subTest(once=once, error=str(failure)):
                self.github = PollGitHub(issue())
                self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
                self.github.read_results["default_branch"] = [failure]
                before = self.snapshot()
                with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                        patch.object(self.loop.stop_event, "wait") as wait, \
                        patch("ub_agents.loop.supervise") as execution, self.assertRaises(AgentError) as raised:
                    self.loop.launch(once=once)
                wait.assert_not_called()
                execution.assert_not_called()
                self.assertIn(str(failure), str(raised.exception))
                if once:
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertIn("failure is not retryable; retries not exhausted", str(raised.exception))
                    self.assertIn("Fix the cause and restart ub-agents launch", str(raised.exception))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.github.writes, [])

    def test_staged_checkout_stops(self):
        (self.root / "role.md").write_text("Staged edits")
        git(self.root, "add", "role.md")
        self.stopped("dirty")

    def test_untracked_checkout_stops(self):
        (self.root / "untracked").write_text("Local file")
        self.stopped("untracked")

    def test_wrong_branch_stops(self):
        git(self.root, "switch", "-c", "feature")
        self.stopped("on feature; switch to the default branch main")

    def test_detached_head_stops(self):
        git(self.root, "checkout", "--detach")
        self.stopped("detached HEAD; switch to the default branch main")

    def test_local_commits_stop(self):
        (self.root / "role.md").write_text("Unpushed policy")
        self.commit(self.root)
        self.stopped("local commits not on origin")

    def test_diverged_checkout_stops(self):
        (self.root / "role.md").write_text("Unpushed policy")
        self.commit(self.root)
        self.push_policy()
        self.stopped("diverged")

    def test_fetch_failure_stops(self):
        git(self.root, "remote", "set-url", "origin", str(self.origin / "missing"))
        self.stopped("fetch of origin/main failed; fix origin access")

    def test_fast_forward_failure_stops_without_mutation(self):
        self.push_policy()
        lock = self.root / ".git" / "index.lock"
        lock.write_text("Existing synthetic lock")
        self.stopped("fast-forward to origin/main failed; fix the checkout")
        self.assertEqual(lock.read_text(), "Existing synthetic lock")

    def test_fast_forward_refuses_to_overwrite_ignored_local_files(self):
        (self.root / "ignored").write_text("Operator local data")
        (self.upstream / "ignored").write_text("New upstream tracked file")
        git(self.upstream, "add", "-f", "ignored")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("fast-forward.*failed")

    def test_autostash_and_merge_hooks_are_not_used(self):
        git(self.root, "config", "merge.autostash", "true")
        marker = self.root / "ignored"
        hook = self.root / ".git" / "hooks" / "post-merge"
        hook.write_text(f"#!/bin/sh\nprintf 'merge hook ran' > '{marker}'\n")
        hook.chmod(0o755)
        self.push_policy()
        self.execute()
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.root, "stash", "list"), "")
        self.assertEqual(git(self.root, "config", "merge.autostash"), "true")

    def test_incoming_missing_instructions_stop_before_fast_forward(self):
        (self.upstream / "role.md").unlink()
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("instructions does not exist")

    def test_incoming_outside_symlink_stops_before_fast_forward(self):
        (self.upstream / "role.md").unlink()
        (self.upstream / "role.md").symlink_to("../outside.md")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("instructions must remain inside the project")

    def test_unreadable_instructions_stop(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.root / "role.md":
                raise PermissionError("Synthetic unreadable instruction file")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            self.stopped("instructions is unreadable")

    def test_unreadable_instructions_stop_before_fast_forward(self):
        self.push_policy()
        with patch.object(Path, "read_text", side_effect=PermissionError("Synthetic unreadable file")):
            self.stopped("instructions is unreadable")

    def test_invalid_incoming_text_stops_before_fast_forward(self):
        (self.upstream / "role.md").write_bytes(b"\xff")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("decode")

    def test_non_main_default_branch_is_used(self):
        git(self.upstream, "branch", "-m", "trunk")
        git(self.upstream, "push", "origin", "trunk")
        git(self.root, "branch", "-m", "trunk")
        self.github.default_branch = lambda: "trunk"
        (self.upstream / "role.md").write_text("Trunk policy")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "trunk")
        self.execute(lambda cwd, prompt: self.assertIn("Trunk policy", prompt))

    def test_configured_symlink_is_retained_and_validated_in_new_tree(self):
        (self.upstream / "alias.md").symlink_to("role.md")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        git(self.root, "pull", "--ff-only")
        self.worker = replace(self.worker, instructions=self.root / "alias.md")
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
        path = self.root / "ub-agents.yaml"
        path.write_text(path.read_text().replace("role.md", "alias.md"))
        loaded = load_config(path)
        self.assertEqual(loaded.agents[0].instructions, self.root / "alias.md")
        # Keep this fixture clean after checking path loading.
        path.write_text(path.read_text().replace("alias.md", "role.md"))
        (self.upstream / "alias.md").unlink()
        (self.upstream / "alias.md").symlink_to("/outside.md")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("instructions must remain inside the project")

    def test_incoming_directory_symlink_is_resolved_in_the_new_tree(self):
        (self.upstream / "policies").mkdir()
        (self.upstream / "policies" / "role.md").write_text("Policy through directory symlink")
        (self.upstream / "alias").symlink_to("policies", target_is_directory=True)
        (self.upstream / "role.md").unlink()
        (self.upstream / "role.md").symlink_to("alias/role.md")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.execute(lambda cwd, prompt: self.assertIn("Policy through directory symlink", prompt))

    def test_incoming_symlink_loop_stops_before_fast_forward(self):
        (self.upstream / "role.md").unlink()
        (self.upstream / "role.md").symlink_to("role.md")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        self.stopped("symlink loop")

    def test_issue_worktree_starts_from_remote_default_branch(self):
        self.worker = replace(self.worker, worktree=True)
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
        self.push_policy()
        head = git(self.upstream, "rev-parse", "HEAD")
        def inspect(cwd, prompt):
            self.assertNotEqual(cwd, self.root)
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), head)
            self.assertIn("Second operator policy", prompt)
        self.execute(inspect)

    def test_pr_worktree_keeps_exact_candidate_and_fixed_operator_prompt(self):
        candidate = git(self.root, "rev-parse", "HEAD")
        git(self.origin, "update-ref", "refs/pull/2/head", candidate)
        self.worker = replace(self.worker, kind="pr", worktree=True)
        self.github = FakeGitHub(pr(head=candidate))
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
        self.push_policy()
        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), candidate)
            self.assertIn("Second operator policy", prompt)
            (cwd / "role.md").write_text("Candidate replacement policy")
            self.assertIn("Second operator policy", prompt)
            self.assertNotIn("Candidate replacement policy", prompt)
            self.loop.coordinator.report(self.loop.coordinator.history(2)[0], "success", "Checked candidate",
                                         outcome="done")
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        self.assert_success(2)

    def test_durable_recovery_never_refreshes(self):
        now = timestamp()
        self.loop.coordinator.clock = lambda: now
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            plan = self.loop.coordinator.plan(self.github.item(1), self.worker, ())
            lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        self.loop.coordinator.report(lease, "success", "Durable completion", outcome="done")
        now += 61
        (self.root / "role.md").write_text("Dirty checkout must not affect recovery")
        with patch("ub_agents.loop.refresh_instructions", side_effect=AssertionError("must not refresh")):
            self.assertTrue(self.loop.tick())
        history = self.loop.coordinator.history(1)
        self.assertTrue(self.loop.coordinator.outcome(lease)["accepted"])
        recovery = next(record for record in history if record.get("mode") == "recovery")
        self.assertEqual((recovery["state"], recovery["result"]), ("released", "success"))

    def test_resume_keeps_checkpoint_sha_after_control_refresh(self):
        checkpoint = git(self.root, "rev-parse", "HEAD")
        git(self.origin, "update-ref", "refs/heads/feature/test", checkpoint)
        self.worker = replace(self.worker, worktree=True)
        self.github = FakeGitHub(issue(), pr(head=checkpoint, labels=(), draft=True))
        self.loop = Loop(config(self.root, self.worker), self.github, "operator", output=lambda *_: None)
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            plan = self.loop.coordinator.plan(self.github.item(1), self.worker, ())
            lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True, branch="feature/test")
        self.loop.coordinator.release(lease, "retry", "Interrupted")
        self.push_policy()
        def run(command, cwd, env, run_dir, timeout, stop, prompt, **kwargs):
            # #32 delegates draft continuation to the agent in a fresh issue worktree.
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), git(self.upstream, "rev-parse", "HEAD"))
            self.assertIn("Second operator policy", prompt)
            self.assertIn('"earlier_branches": [\n    "feature/test"\n  ]', prompt)
            self.assertIn("continue an open draft PR", prompt)
            draft = self.github.prs_for_branch("feature/test")[0]
            git(cwd, "fetch", "origin", draft.branch)
            git(cwd, "checkout", "--detach", "FETCH_HEAD")
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), checkpoint)
            self.assertIn("Second operator policy", prompt)
            self.github.change(2, draft=False)
            current = self.loop.coordinator.history(1)[-1]
            self.loop.coordinator.report(current, "success", "Continued checkpoint", handoff=2, outcome="done")
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())
        self.assert_success(1)

    def test_idle_launcher_does_not_refresh(self):
        self.github.change(1, labels=frozenset())
        self.github.change(3, labels=frozenset())
        with patch("ub_agents.loop.refresh_instructions", side_effect=AssertionError("must not refresh")):
            self.loop.launch(once=True)

    def test_check_rejects_unreadable_instruction_text(self):
        (self.root / "role.md").write_bytes(b"\xff")
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(["--config", str(self.root / "ub-agents.yaml"), "check"]), 1)
        self.assertIn("instructions is unreadable", error.getvalue())

    def test_cli_refresh_failure_exits_nonzero_without_claiming(self):
        (self.root / "untracked").write_text("Local file")
        error = io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.repository_checks", return_value=[]), \
                patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                redirect_stdout(io.StringIO()), redirect_stderr(error):
            self.assertEqual(main(["launch", "--once"]), 1)
        self.assertIn("Control checkout refresh stopped", error.getvalue())
        self.assertIn("dirty", error.getvalue())
        self.assertEqual(self.github.writes, [])
