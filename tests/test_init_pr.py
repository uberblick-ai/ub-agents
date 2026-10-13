import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.support import RecordingRunner, SetupGitHub
from ub_agents.cli import main
from ub_agents.errors import AgentError
from ub_agents.init_pr import SetupPR, saved_attempt
from ub_agents.init_setup import Choices, apply_plan, make_plan


class PRRunner(RecordingRunner):
    def __init__(self, plan, default="trunk"):
        super().__init__(plan.path.parent)
        self.plan = plan
        self.branch = default
        self.head = "a" * 40
        self.base = self.head
        self.commit = "b" * 40
        self.local = ""
        self.remote = ""
        self.dirty = ""
        self.fail = None
        self.committed = {}

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        args = command[3:]
        if self.fail and args[0] == self.fail:
            return subprocess.CompletedProcess(command, 1, "", "sensitive diagnostic")
        if args == ["rev-parse", "--show-toplevel"]:
            value = str(self.root)
        elif args == ["remote", "get-url", "origin"]:
            value = "git@github.com:org/project.git"
        elif args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            value = self.branch
        elif args == ["rev-parse", "HEAD"]:
            value = self.head
        elif args[0] == "check-ref-format":
            value = ""
        elif args[0] == "ls-remote":
            ref = args[-1]
            sha = self.base if ref == "refs/heads/trunk" else self.remote
            value = f"{sha}\t{ref}\n" if sha else ""
        elif "status" in args:
            value = self.dirty
        elif args[0] == "for-each-ref":
            value = self.local
        elif args[0] == "switch":
            self.branch = args[2]
            self.local = self.head
            value = ""
        elif args[0] == "add":
            value = ""
        elif args[0] == "commit":
            paths = args[args.index("--") + 1:]
            self.committed = {p: (self.root / p).read_text() for p in paths}
            self.local = self.head = self.commit
            value = ""
        elif args[:3] == ["show", "-s", "--format=%P"]:
            value = self.base
        elif args[:3] == ["show", "-s", "--format=%s"]:
            value = "Set up ub-agents"
        elif args[0] == "show":
            value = self.committed[args[1].split(":", 1)[1]]
        elif args[0] == "diff-tree":
            value = "\0".join(self.committed) + "\0"
        elif args[0] == "push":
            self.remote = self.head
            value = ""
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(command, 0, value, "")


class PRGitHub(SetupGitHub):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self.prs = []
        self.lose_create = False

    def request(self, endpoint, method="GET", data=None, **kwargs):
        self.reads.append(endpoint)
        if endpoint == "repos/org/project":
            return {"full_name": "org/project", "default_branch": "trunk"}
        if "/pulls?" in endpoint:
            return self.prs.copy()
        if method == "POST":
            pr = {"number": 7, "html_url": "https://github.com/org/project/pull/7", "state": "open",
                  "merged_at": None, "body": data["body"], "user": {"login": "operator"},
                  "head": {"sha": self.runner.head, "ref": data["head"], "repo": {"full_name": "org/project"}},
                  "base": {"ref": data["base"]}}
            self.prs.append(pr)
            self.writes.append(("create-pr", data))
            if self.lose_create:
                raise AgentError("Response unavailable")
            return pr
        if endpoint == "repos/org/project/pulls/7":
            return self.prs[0]
        return super().request(endpoint, method, data, **kwargs)


class SetupPRTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "workflow.yaml"
        self.plan = make_plan(self.path, "org/project", Choices("codex:model:high", setup_pr=True))
        self.runner = PRRunner(self.plan)
        self.github = PRGitHub(self.runner)

    def publication(self, plan=None):
        return SetupPR(plan or self.plan, self.github, self.runner).inspect()

    def apply(self):
        publication = self.publication()
        publication.begin()
        result = apply_plan(self.plan, self.github)
        self.assertTrue(result.ok, result.error)
        return publication.publish(result)

    def resume(self):
        self.plan = make_plan(self.path, "org/project", Choices("codex:model:high", setup_pr=True),
                              resume=saved_attempt(self.path))
        return self.apply()

    def test_inspection_and_cancel_are_read_only(self):
        text = self.publication().preview()
        for value in ("trunk", "operator", "origin/trunk", "setup-ub-agents", "workflow.yaml"):
            self.assertIn(value, text)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".ub-agents").exists())
        self.assertFalse(self.github.writes)
        self.assertFalse(any(call[0][3] in {"switch", "push", "commit", "fetch"} for call in self.runner.calls))

    def test_commit_and_push_only_exact_setup_paths_and_actual_default(self):
        result = self.apply()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.pr_url, "https://github.com/org/project/pull/7")
        self.assertEqual(set(self.runner.committed), {"workflow.yaml", ".gitignore",
                         *(".agents/" + role + ".md" for role in
                           ("ub_agents", "issue-preparer", "implementer", "reviewer", "integrator"))})
        self.assertEqual(self.github.writes[0][1]["base"], "trunk")
        self.assertIn(("git", "-C", str(self.root), "push", "origin", "HEAD:refs/heads/setup-ub-agents"),
                      [call[0] for call in self.runner.calls])
        commit = next(call[0] for call in self.runner.calls if call[0][3] == "commit")
        self.assertIn("--only", commit)
        self.assertTrue(all("--force" not in call[0] for call in self.runner.calls))

    def test_dirty_local_branch_remote_branch_and_wrong_head_stop_before_writes(self):
        for attr, value in (("dirty", " M unrelated\0"), ("local", "c" * 40),
                            ("remote", "c" * 40), ("head", "c" * 40)):
            with self.subTest(attr=attr):
                previous = getattr(self.runner, attr)
                setattr(self.runner, attr, value)
                with self.assertRaises(AgentError):
                    self.publication()
                setattr(self.runner, attr, previous)
                self.assertFalse(self.path.exists())
                self.assertFalse(self.github.writes)

    def test_default_branch_name_cannot_be_setup_branch(self):
        with self.assertRaisesRegex(AgentError, "different from the default"):
            SetupPR(self.plan, self.github, self.runner, "trunk").inspect()

    def test_push_failure_reuses_verified_files_and_commit(self):
        self.runner.fail = "push"
        self.assertFalse(self.apply().ok)
        self.runner.fail = None
        result = self.resume()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sum(call[0][3] == "commit" for call in self.runner.calls), 1)
        self.assertEqual(len(self.github.prs), 1)

    def test_ambiguous_pr_create_reuses_same_pr_without_duplicate(self):
        self.github.lose_create = True
        self.assertFalse(self.apply().ok)
        self.github.lose_create = False
        result = self.resume()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.github.prs), 1)

    def test_modified_recovery_file_is_never_overwritten(self):
        self.runner.fail = "push"
        self.apply()
        self.path.write_text("operator changes")
        with self.assertRaisesRegex(AgentError, "nothing overwritten"):
            self.resume()
        self.assertEqual(self.path.read_text(), "operator changes")

    def test_unattended_flag_publishes_without_prompts_or_optional_effects(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("subprocess.run", self.runner), patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("builtins.input", side_effect=AssertionError("no prompts")), \
                redirect_stdout(output), redirect_stderr(error):
            code = main(["init", "--use-defaults", "--config", str(self.path),
                         "--repository", "org/project", "--runtime", "codex:model:high"])
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertIn("https://github.com/org/project/pull/7", output.getvalue())
        self.assertIn("permissions deferred", output.getvalue())
        self.assertEqual([w[0] for w in self.github.writes], ["create-pr"])

    def test_unattended_required_failure_is_nonzero_and_actionable(self):
        self.runner.dirty = "?? unrelated\0"
        with patch("subprocess.run", self.runner), patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("builtins.input", side_effect=AssertionError("no prompts")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            code = main(["init", "--use-defaults", "--config", str(self.path),
                         "--repository", "org/project", "--runtime", "codex:model:high"])
        self.assertEqual(code, 1)
        self.assertIn("commit them externally", error.getvalue())
        self.assertFalse(self.path.exists())
