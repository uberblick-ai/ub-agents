from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Runtime, load_config
from ub_agents.errors import AgentError
from ub_agents.execution import git
from ub_agents.loop import Loop
from ub_agents.records import attempts, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.origin, self.upstream, self.root = [base / p for p in ("origin.git", "upstream", "control")]
        git(base, "init", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", str(self.origin), str(self.upstream))
        (self.upstream / ".gitignore").write_text(".ub-agent/\nignored\n")
        (self.upstream / "role.md").write_text("First operator policy\n")
        (self.upstream / "ub-agent.yaml").write_text("repository: org/project\nagents:\n  worker:\n"
                                                    "    runtime: codex:model:high\n"
                                                    "    trigger: ready\n    instructions: role.md\n")
        self.commit(self.upstream)
        git(self.upstream, "push", "origin", "main")
        git(base, "clone", str(self.origin), str(self.root))
        self.worker = agent(self.root, kind="issue", instructions=self.root / "role.md",
                            command=(), runtimes=(Runtime("codex", "model", "high", "openai"),))
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

    def snapshot(self):
        files = {}
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if relative.parts[0] in {".git", ".ub-agent"}:
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

    def stopped(self, condition):
        before, writes = self.snapshot(), list(self.github.writes)
        with patch("ub_agents.loop.supervise") as execution, \
                patch("ub_agents.loop.Workspace.prepare") as prepare, \
                patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                self.assertRaisesRegex(AgentError, condition) as error:
            self.loop.launch(once=True)
        execution.assert_not_called()
        prepare.assert_not_called()
        self.assertIn("Fix the operator checkout", str(error.exception))
        self.assertIn("no assignment attempt was charged", str(error.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.github.writes, writes)
        self.assertEqual(attempts(self.loop.coordinator.history(1), "worker", timestamp()), [])
        return str(error.exception)

    def execute(self, callback=None):
        def run(command, cwd, env, run_dir, timeout, heartbeat, stop, prompt, **kwargs):
            if callback:
                callback(cwd, prompt)
            number = int(env["UB_AGENT_ASSIGNMENT"])
            lease = self.loop.coordinator.history(number)[0]
            self.github.change(number, labels=frozenset())
            self.loop.coordinator.report(lease, "success", "Synthetic run finished")
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())

    def test_pushed_policy_appears_in_second_prompt_without_restart(self):
        prompts = []
        self.execute(lambda cwd, prompt: prompts.append(prompt))
        # An upstream config edit remains ineffective until launcher restart.
        (self.upstream / "ub-agent.yaml").write_text("configuration changes require restart\n")
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

    def test_ignored_local_instructions_remain_valid_after_refresh(self):
        local = self.root / ".ub-agent" / "local-role.md"
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
        path = self.root / "ub-agent.yaml"
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
        def run(command, cwd, env, run_dir, timeout, heartbeat, stop, prompt, **kwargs):
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), candidate)
            self.assertIn("Second operator policy", prompt)
            (cwd / "role.md").write_text("Candidate replacement policy")
            self.assertIn("Second operator policy", prompt)
            self.assertNotIn("Candidate replacement policy", prompt)
            self.github.change(2, labels=frozenset())
            self.loop.coordinator.report(self.loop.coordinator.history(2)[0], "success", "Checked candidate")
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())

    def test_durable_recovery_never_refreshes(self):
        now = timestamp()
        self.loop.coordinator.clock = lambda: now
        with patch("ub_agents.coordination.shutil.which", return_value="installed"):
            plan = self.loop.coordinator.plan(self.github.item(1), self.worker, ())
            lease = self.loop.coordinator.claim(plan)
        self.loop.coordinator.update(lease, state="running", started=True)
        self.github.change(1, labels=frozenset())
        self.loop.coordinator.report(lease, "success", "Durable completion")
        now += 61
        (self.root / "role.md").write_text("Dirty checkout must not affect recovery")
        with patch("ub_agents.loop.refresh_instructions", side_effect=AssertionError("must not refresh")):
            self.assertTrue(self.loop.tick())

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
        def run(command, cwd, env, run_dir, timeout, heartbeat, stop, prompt, **kwargs):
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), checkpoint)
            self.assertIn("Second operator policy", prompt)
            self.assertIn("Resume existing draft PR #2", prompt)
            self.github.change(1, labels=frozenset())
            self.github.change(2, draft=False)
            current = self.loop.coordinator.history(1)[-1]
            self.loop.coordinator.report(current, "success", "Continued checkpoint", handoff=2)
            return 0
        with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(self.loop.tick())

    def test_idle_launcher_does_not_refresh(self):
        self.github.change(1, labels=frozenset())
        self.github.change(3, labels=frozenset())
        with patch("ub_agents.loop.refresh_instructions", side_effect=AssertionError("must not refresh")):
            self.loop.launch(once=True)

    def test_check_rejects_unreadable_instruction_text(self):
        (self.root / "role.md").write_bytes(b"\xff")
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(["--config", str(self.root / "ub-agent.yaml"), "check"]), 1)
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
