from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.errors import AgentError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ub-agent.yaml"

    def load(self, content):
        self.path.write_text(content)
        return load_config(self.path)

    def test_direct_command_and_distinct_clock_overrides(self):
        result = self.load('''repository: org/project
agents:
  investigate:
    command: [python3, task.py]
    trigger: investigate
    lease-minutes: 90
    renewal-minutes: 10
    agent-timeout-minutes: 240
limits:
  max-attempts: 7
''')
        agent = result.agents[0]
        self.assertEqual((agent.lease_seconds, agent.renewal_seconds, agent.timeout_seconds), (5400, 600, 14400))
        self.assertEqual(agent.max_attempts, 7)

    def test_rejects_unknown_duplicates_unsafe_clocks_and_paths(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [true]\n    trigger: ready\n"
        for content in [base.replace("[true]", "[echo]") + "    renewal-minutes: 60\n",
                        base.replace("[true]", "[echo]") + "    lease-minutes: .nan\n",
                        base.replace("[true]", "[echo]") + "    mystery: true\n",
                        base + "repository: other/project\n", base + "    instructions: /etc/passwd\n",
                        base + "    agent-timeout-minutes: false\n", base + "    max-attempts: 2.5\n",
                        base + "    runtime: codex:model:high\n", base]:
            with self.subTest(content=content), self.assertRaises(AgentError):
                self.load(content)

    def test_custom_runtime_and_alternatives_require_no_python_workflow(self):
        (self.root / "instructions.md").write_text("Do the task")
        result = self.load('''repository: org/project
runtimes:
  example:
    provider: example-provider
    command: [example-cli, --model, "{model}", --effort, "{effort}"]
agents:
  investigate:
    runtime: [example:model-a:high, codex:model-b:low]
    trigger: investigate
    instructions: instructions.md
stop-labels: []
''')
        self.assertEqual(len(result.agents[0].runtimes), 2)
        self.assertEqual(result.stop_labels, ())

    def test_init_and_installed_template_preservation(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "init", "--repository", "org/project"]), 0)
            original = self.path.read_text()
            self.assertEqual(main(["--config", str(self.path), "init", "--repository", "different/project"]), 1)
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(len(load_config(self.path).agents), 4)
        self.assertIn(".ub-agent/", (self.root / ".gitignore").read_text())
