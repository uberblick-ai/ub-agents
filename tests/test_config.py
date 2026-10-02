from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from ub_agents.cli import main
from ub_agents.config import Priority, Queue, load_config
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

    def test_outcome_configuration_and_check_rejections(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n"
        valid = self.load(base + "    outcomes:\n      done: {add: [needs-review], remove: [old]}\n      merged: {}\n")
        self.assertEqual(valid.agents[0].outcomes["merged"], {"add": (), "remove": ()})
        for outcomes in ["[]", "{}", "null", "{done: []}", "{done: {unknown: []}}",
                         "{done: {add: [1]}}", "{done: {remove: [false]}}",
                         "{done: {add: needs-review}}", "{done: {add: [ready]}}",
                         "{done: {remove: [needs-human]}}"]:
            with self.subTest(outcomes=outcomes):
                self.path.write_text(base + f"    outcomes: {outcomes}\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.path), "check"]), 1)
        with self.assertRaisesRegex(AgentError, "remove a stop label"):
            self.load(base.replace("trigger: ready", "trigger: needs-human") + "    outcomes: {done: {}}\n")
        with self.assertRaisesRegex(AgentError, "outcomes must be"):
            self.load(base)

    def test_direct_command_and_distinct_clock_overrides(self):
        result = self.load('''repository: org/project
agents:
  investigate:
    command: [./scripts/task.py, --fast]
    trigger: investigate
    outcomes: {done: {}}
    agent-timeout-minutes: 240
limits:
  max-attempts: 7
cleanup:
  command: [./scripts/cleanup]
  timeout-seconds: 120
''')
        agent = result.agents[0]
        # The lease covers the run, a fifteen-minute grace and the cleanup hook.
        self.assertEqual((agent.lease_seconds, agent.timeout_seconds), (14400 + 900 + 120, 14400))
        self.assertEqual(agent.max_attempts, 7)
        # Relative executables resolve against the configuration's directory.
        self.assertEqual(agent.command, (str(self.root.resolve() / "scripts/task.py"), "--fast"))

    def test_rejects_unknown_duplicates_unsafe_clocks_and_paths(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [true]\n    trigger: ready\n    outcomes: {done: {}}\n"
        for content in [base.replace("[true]", "[echo]") + "    agent-timeout-minutes: .nan\n",
                        base.replace("[true]", "[echo]") + "    lease-minutes: 90\n",
                        base.replace("[true]", "[echo]") + "    mystery: true\n",
                        base + "repository: other/project\n", base + "    instructions: /etc/passwd\n",
                        base + "    agent-timeout-minutes: false\n", base + "    max-attempts: 2.5\n",
                        base.replace("[true]", "[echo]") + "    runtime-args: [--model, other]\n",
                        base.replace("[true]", "[echo]") + "    runtime-args: [-c, model=other]\n",
                        base.replace("[true]", "[echo]") + "    runtime-args: [--config=model_reasoning_effort=low]\n",
                        base + "    runtime: codex:model:high\n", base]:
            with self.subTest(content=content), self.assertRaises(AgentError):
                self.load(content)

    def test_runtime_alternatives_and_unknown_cli(self):
        (self.root / "instructions.md").write_text("Do the task")
        content = '''repository: org/project
agents:
  investigate:
    runtime: [claude:model-a:high, codex:model-b:low]
    trigger: investigate
    outcomes: {done: {}}
    instructions: instructions.md
stop-labels: []
'''
        result = self.load(content)
        self.assertEqual([r.name for r in result.agents[0].runtimes], ["claude:model-a:high", "codex:model-b:low"])
        self.assertEqual(result.stop_labels, ())
        with self.assertRaisesRegex(AgentError, "codex, claude"):
            self.load(content.replace("codex:model-b:low", "example:model-b:low"))

    def test_init_and_installed_template_preservation(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "init", "--repository", "org/project"]), 0)
            original = self.path.read_text()
            self.assertEqual(main(["--config", str(self.path), "init", "--repository", "different/project"]), 1)
        self.assertEqual(self.path.read_text(), original)
        agents = load_config(self.path).agents
        self.assertEqual(len(agents), 4)
        self.assertTrue(all(a.outcomes for a in agents))
        self.assertEqual(agents[0].outcomes['needs-human']['add'], ('needs-human',))
        self.assertEqual(agents[-1].outcomes['maintainer-merge']['add'], ('needs-human',))
        for configured in agents:
            instructions = configured.instructions.read_text()
            self.assertIn('--outcome', instructions)
            self.assertNotIn('--status success', instructions)
        self.assertIn("queue:\n  milestones: ignore\n", original)
        self.assertEqual(load_config(self.path).queue, Queue())
        self.assertIn(".ub-agent/", (self.root / ".gitignore").read_text())

    def test_queue_defaults_and_configured_priority(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        for extra in ("", "queue: {}\n", "queue:\n  milestones: ignore\n"):
            with self.subTest(extra=extra):
                self.assertEqual(self.load(base + extra).queue, Queue())
        self.assertEqual(self.load(base + "queue:\n  milestones: gate\n").queue, Queue("gate"))
        self.assertEqual(self.load(base + "queue:\n  dependencies: wait\n").queue, Queue())
        self.assertEqual(self.load(base + "queue:\n  dependencies: ignore\n").queue,
                         Queue(dependencies="ignore"))
        extra = "queue:\n  priority:\n    labels: [urgent, normal, low]\n"
        self.assertEqual(self.load(base + extra).queue.priority, Priority(("urgent", "normal", "low")))
        self.assertEqual(self.load(base + extra + "    default: normal\n").queue.priority.default, "normal")

    def test_queue_validation_through_check(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        for extra in ("queue: null", "queue: []", "queue:\n  unknown: true",
                      "queue:\n  dependencies: gate", "queue:\n  dependencies: null",
                      "queue:\n  dependencies: []", "queue:\n  dependencies: false",
                      "queue:\n  milestones: oldest", "queue:\n  milestones: null",
                      "queue:\n  milestones: []", "queue:\n  milestones: false",
                      "queue:\n  priority: null", "queue:\n  priority: []",
                      "queue:\n  priority:\n    unknown: true",
                      "queue:\n  priority: {}", "queue:\n  priority:\n    labels: []",
                      "queue:\n  priority:\n    labels: normal",
                      "queue:\n  priority:\n    labels: [normal, normal]",
                      "queue:\n  priority:\n    labels: ['']",
                      "queue:\n  priority:\n    labels: ['   ']",
                      "queue:\n  priority:\n    labels: [null]",
                      "queue:\n  priority:\n    labels: [normal]\n    default: low",
                      "queue:\n  priority:\n    labels: [normal]\n    default: null"):
            with self.subTest(extra=extra):
                self.path.write_text(base + extra + "\n")
                with self.assertRaises(AgentError):
                    load_config(self.path)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.path), "check"]), 1)

    def test_queue_and_outcomes_can_be_configured_together(self):
        configured = self.load('''repository: org/project
queue:
  milestones: gate
  priority:
    labels: [urgent, normal]
    default: normal
agents:
  task:
    command: [echo]
    trigger: ready
    outcomes:
      handed-off: {add: [needs-review], remove: [old]}
''')
        self.assertEqual(configured.queue, Queue("gate", Priority(("urgent", "normal"), "normal")))
        self.assertEqual(configured.agents[0].outcomes,
                         {"handed-off": {"add": ("needs-review",), "remove": ("old",)}})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "check"]), 0)
