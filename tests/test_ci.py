"""Local CI must propagate static-check failures before reporting success."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.support import RecordingCITools


class LocalCITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        repository = Path(__file__).resolve().parents[1]
        (self.root / "bin").mkdir()
        shutil.copy2(repository / "bin/ci.sh", self.root / "bin/ci.sh")
        shutil.copy2(repository / "requirements-dev.txt", self.root / "requirements-dev.txt")
        self.tools = RecordingCITools(self.root)

    def run_ci(self):
        return subprocess.run(["sh", str(self.root / "bin/ci.sh")], cwd=self.root,
                              env=self.tools.environment(), text=True, capture_output=True,
                              timeout=15)

    def test_checks_run_in_fresh_worktree_with_shared_tool_pins(self):
        result = self.run_ci()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.tools.calls()
        checks = [(name, args) for name, args, _ in calls
                  if name in {"ruff", "mypy", "python", "ub-agents"}]
        self.assertEqual(checks, [
            ("python", ["-m", "pip", "install", "-q", "-e", ".", "-r", "requirements-dev.txt"]),
            ("ruff", ["check", "src"]), ("mypy", []),
            ("python", ["-m", "tests"]), ("ub-agents", ["doctor", "--offline"]),
        ])
        worktrees = {cwd for name, _, cwd in calls if name in {"ruff", "mypy"}}
        self.assertEqual(len(worktrees), 1)
        self.assertNotIn(str(self.root), worktrees)
        self.assertIn("all checks passed at " + self.tools.sha[:12], result.stdout)
        self.assertEqual(list(self.tools.scratch.iterdir()), [])

    def test_static_check_failure_fails_ci_and_cleans_up(self):
        for tool, check in (("ruff", "Complexity"), ("mypy", "Types")):
            with self.subTest(tool=tool):
                self.tools.log.unlink(missing_ok=True)
                (self.root / "fail").write_text(tool)
                result = self.run_ci()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(check + " failed at " + self.tools.sha[:12], result.stderr)
                self.assertNotIn("all checks passed", result.stdout)
                calls = [(name, args) for name, args, _ in self.tools.calls()]
                self.assertNotIn(("python", ["-m", "tests"]), calls)
                self.assertNotIn(("ub-agents", ["doctor", "--offline"]), calls)
                self.assertEqual(list(self.tools.scratch.iterdir()), [])
