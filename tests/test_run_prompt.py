"""Pin run inputs at the execution boundary and verify the pure builders."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ub_agents.approvals import ApprovalCheck
from ub_agents.config import Runtime
from ub_agents.coordination import Plan
from ub_agents.loop import Loop
from ub_agents.run_prompt import run_context, run_environment, run_prompt
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class RunPromptCharacterizationTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = str(Path(os.environ["XDG_STATE_HOME"]).resolve())

    def capture(self, kind):
        runtime = Runtime("codex", "model", "high")
        outcomes = {"done": {"add": ("needs-review",), "remove": ("ready",)}}
        if kind == "pr":
            outcomes["done"]["max-rounds"] = 3
        role = agent(self.root, command=(), runtimes=(runtime,), outcomes=outcomes,
                     retrospectives=203 if kind == "issue" else None)
        other = agent(self.root, name="reviewer", triggers=("needs-review",), kind="pr",
                      outcomes={"revise": {"add": ("needs-changes",), "remove": ("needs-review",)}})
        candidate = issue() if kind == "issue" else pr()
        github = FakeGitHub(candidate)
        configured = replace(config(self.root, role, other), shared_instructions=self.root / "policy.md")
        loop = Loop(configured, github, "operator", output=lambda *_: None)
        loop.coordinator.clock = lambda: 1000
        loop.coordinator.runtime_available = lambda cli: True
        loop._renewal = Mock()
        plan = loop.coordinator.plan(candidate, role, ())
        snapshot = {"title": "Title \u2603", "body": "First line\nSecond line",
                    "comments": [{"id": 50, "body": "Comment"}],
                    "withheld_counts": {"comments": 1}}
        if kind == "pr":
            snapshot["withheld_counts"] |= {"reviews": 2, "review_comments": 3}
            snapshot |= {"reviews": [{"id": 51, "body": "Review"}],
                         "review_comments": [{"id": 52, "body": "Inline review", "path": "file.py"}]}
        feedback = [{"agent": "reviewer", "outcome": "revise", "summary": "Earlier correction",
                     "candidate_sha": candidate.head, "created": "2026-01-01T00:00:00Z"}]
        earlier = ["earlier/branch"] if kind == "issue" else []
        events = []
        captured = {}
        original_write = Path.write_text

        def write(path, text, *args, **kwargs):
            if path.name in {"context.json", "run.json"}:
                events.append(path.name)
            result = original_write(path, text, *args, **kwargs)
            if path.name == "run.json":
                os.environ["AFTER_RUN_CONFIG"] = "visible"
            return result

        def prepare(workspace):
            workspace.branch = "issue/branch" if kind == "issue" else None
            return self.root

        def execute(command, cwd, environment, run_dir, timeout, stop, prompt, **kwargs):
            events.append("execute")
            captured.update(prompt=prompt, context=(run_dir / "context.json").read_text(),
                            environment=environment)
            self.assertTrue((run_dir / "run.json").is_file())
            return 1

        inherited = {"XDG_STATE_HOME": self.state, "KEEP": "inherited", "ub_agents_lower": "keep",
                     "UB_AGENTS_OLD": "discard", "UB_AGENTS_RUN": "old-run",
                     "UB_AGENTS_CONTEXT": "/old/context", "UB_AGENTS_BRANCH": "old-branch"}
        with patch.dict(os.environ, inherited, clear=True), \
                patch("ub_agents.coordination.uuid.uuid4", return_value=SimpleNamespace(hex="characterized-run")), \
                patch.object(loop.queue_planner, "input_check", return_value=ApprovalCheck(True, "Approved", snapshot=snapshot)), \
                patch.object(loop.coordinator, "feedback", side_effect=lambda *args: events.append("feedback") or feedback), \
                patch("ub_agents.assignment_run.earlier_branches", side_effect=lambda *args: events.append("earlier_branches") or earlier), \
                patch("ub_agents.assignment_run.launcher_report_command", return_value="/launcher/python -I /launcher/report_command.py"), \
                patch("ub_agents.assignment_run.Workspace.prepare", prepare), \
                patch.object(Path, "write_text", write), \
                patch("ub_agents.assignment_run.supervise", side_effect=execute) as executed:
            self.assertTrue(loop._claim_execute(plan, "Role procedure", shared_instructions="Shared merge policy"))
        executed.assert_called_once()
        self.assertEqual(events, ["feedback", "earlier_branches", "context.json", "run.json", "execute"])
        # Only the fixture's private directory locations vary between executions.
        return json.loads(json.dumps(captured).replace(str(self.root), "<root>").replace(self.state, "<state>"))

    def test_issue_prompt_context_text_and_environment(self):
        expected = json.loads((Path(__file__).parent / "fixtures/run_prompt_issue.json").read_text())
        self.assertEqual(self.capture("issue"), expected)

    def test_pr_prompt_context_text_and_environment(self):
        expected = json.loads((Path(__file__).parent / "fixtures/run_prompt_pr.json").read_text())
        self.assertEqual(self.capture("pr"), expected)


class RunPromptBuilderTests(unittest.TestCase):
    def test_builders_use_explicit_inputs_without_io_or_environment_reads(self):
        root = Path("/unused/project")
        role = agent(root, worktree=True)
        configured = config(root, role)
        candidate = pr(head_repository="outside/project")
        plan = Plan(candidate, role, None, "ready", "", 1)
        lease = {"run": "explicit-run", "id": 17, "outcome_rounds": {"done": {"used": 1, "limit": 3}}}
        snapshot = {"title": "Title", "body": "Body", "comments": [], "withheld_counts": {},
                    "reviews": [], "review_comments": []}
        feedback = [{"summary": "Correction"}]
        earlier = []
        environment = {"KEEP": "value", "UB_AGENTS_RUN": "stale", "UB_AGENTS_OTHER": "discard"}
        inputs = (configured, plan, lease, snapshot, feedback, earlier, environment)
        before = deepcopy(inputs)
        with patch("os.environ", None), \
                patch.object(Path, "open", side_effect=AssertionError("file I/O")):
            context = run_context(configured.repository, plan, lease, snapshot, feedback=feedback,
                                  scratch=Path("/explicit/scratch"), report_command="/explicit/report",
                                  branch=None, earlier_branches=earlier)
            prompt = run_prompt(configured, "off", plan, lease, context, "Role procedure")
            env = run_environment(environment, context, lease["id"], root / "context.json", root / "run.json")
        self.assertEqual(inputs, before)
        self.assertIn("refs/ub-agents/runs/explicit-run/head", prompt)
        self.assertIn(json.dumps(context, indent=2), prompt)
        self.assertEqual(env, {
            "KEEP": "value", "UB_AGENTS_REPOSITORY": "org/project", "UB_AGENTS_ASSIGNMENT": "2",
            "UB_AGENTS_RUN": "explicit-run", "UB_AGENTS_LEASE_ID": "17",
            "UB_AGENTS_CONTEXT": "/unused/project/context.json", "UB_AGENTS_RUN_CONFIG": "/unused/project/run.json",
            "UB_AGENTS_REPORT": "/explicit/report", "UB_AGENTS_SCRATCH": "/explicit/scratch",
            "UB_AGENTS_CANDIDATE_SHA": candidate.head, "UB_AGENTS_BRANCH": ""})
