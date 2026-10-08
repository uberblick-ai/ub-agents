from contextlib import chdir, redirect_stdout, redirect_stderr
from importlib.metadata import distribution
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Priority, Queue, load_config
from ub_agents.errors import AgentError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve like load_config does: macOS temporary paths live under /private/var.
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "ub-agents.yaml"

    def load(self, content):
        self.path.write_text(content)
        return load_config(self.path)

    def test_package_exposes_only_the_ub_agents_command(self):
        scripts = {entry.name: entry.value for entry in distribution("ub-agents").entry_points
                   if entry.group == "console_scripts"}
        self.assertEqual(scripts, {"ub-agents": "ub_agents.cli:main"})

    def test_default_config_requires_the_named_file_and_explicit_paths_work(self):
        custom = self.root / "custom.yaml"
        custom.write_text("repository: org/project\nagents:\n  task:\n    command: [echo]\n"
                          "    trigger: ready\n    outcomes: {done: {}}\n")
        with chdir(self.root), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["check"]), 1)
            self.assertIn("No ub-agents.yaml here; run ub-agents init", error.getvalue())
            self.assertEqual(main(["--config", str(custom), "check"]), 0)
            custom.rename(self.path)
            self.assertEqual(main(["check"]), 0)

    def test_shared_instructions_are_optional_and_resolve_like_role_paths(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        self.assertIsNone(self.load(base).shared_instructions)
        policy = self.root / 'policy.md'
        policy.write_text('Project loop policy')
        alias = self.root / 'alias.md'
        alias.symlink_to('policy.md')
        configured = self.load(base + 'shared-instructions: alias.md\n')
        self.assertEqual(configured.shared_instructions, alias)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--config', str(self.path), 'check']), 0)

    def test_check_rejects_invalid_shared_paths_and_unreadable_text(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        policy = self.root / 'policy.md'
        policy.write_bytes(b'\xff')
        alias = self.root / 'alias.md'
        alias.symlink_to('../outside.md')
        for value, message in (('missing.md', 'does not exist'), ('../outside.md', 'inside the project'),
                               (str(self.root.parent / 'outside.md'), 'inside the project'),
                               ('alias.md', 'inside the project'), ('policy.md', 'unreadable'),
                               ('null', 'nonempty string'), ('[]', 'nonempty string')):
            with self.subTest(value=value):
                self.path.write_text(base + f'shared-instructions: {value}\n')
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(['--config', str(self.path), 'check']), 1)
                self.assertIn('shared-instructions', error.getvalue())
                self.assertIn(message, error.getvalue())
        policy.write_text('Readable policy')
        self.path.write_text(base + 'shared-instructions: policy.md\n')
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == policy:
                raise PermissionError('Synthetic unreadable policy')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(['--config', str(self.path), 'check']), 1)
        self.assertIn('shared-instructions is unreadable', error.getvalue())

    def test_repository_guidance_keeps_project_rules_and_shared_file_holds_loop_policy(self):
        root = Path(__file__).resolve().parents[1]
        project = (root / 'AGENTS.md').read_text()
        shared = (root / '.agents/ub_agents.md').read_text()
        for section in ('Inside the loop', 'Retrospectives', 'Merging', 'Human decisions', 'Review focus'):
            self.assertNotIn('## ' + section, project)
        for section in ('Checks', 'Merging', 'Human decisions', 'Review focus'):
            self.assertIn('## ' + section, shared)
        policy = ' '.join(shared.split())
        self.assertIn('`--match-head-commit` set to the assigned SHA or the head of its own clean base merge',
                      policy)
        self.assertIn('That clean base merge keeps the review and needs no new review', policy)
        self.assertIn('a green `signoff` status from local CI at the head it merges', policy)
        self.assertEqual(load_config(root / 'ub-agents.yaml').shared_instructions, root / '.agents/ub_agents.md')
        for path in (root / '.agents').rglob('*.md'):
            text = path.read_text()
            self.assertNotIn('Every stop report', text)
            self.assertNotIn('Stop reports need', text)
            self.assertNotIn('at most 300 characters', text)

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

    def test_retrospectives_requires_a_positive_integer_and_check_stays_offline(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        self.assertIsNone(self.load(base).agents[0].retrospectives)
        self.assertEqual(self.load(base + "    retrospectives: 203\n").agents[0].retrospectives, 203)
        with patch("ub_agents.cli.GitHub", side_effect=AssertionError("offline check")):
            for value in ("203", "0", "-1", "203.0", ".inf", ".nan", "true", "false", "null",
                          "'203'", "[]", "{}", "''"):
                with self.subTest(value=value):
                    self.path.write_text(base + f"    retrospectives: {value}\n")
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                        self.assertEqual(main(["--config", str(self.path), "check"]), 0 if value == "203" else 1)
                    if value != "203":
                        self.assertIn("task retrospectives", stderr.getvalue())

    def test_launchers_requires_a_nonempty_list_of_unique_nonblank_logins(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        self.assertIsNone(self.load(base).launchers)
        self.assertEqual(self.load(base + "launchers: [bot-a, Alice]\n").launchers, ("bot-a", "Alice"))
        for value in ("alice", "null", "[]", "{}", "[1]", "[false]", "[null]", "['']", "['   ']",
                      "[alice, alice]", "[Alice, ALICE]"):
            with self.subTest(value=value):
                self.path.write_text(base + f"launchers: {value}\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(["--config", str(self.path), "check"]), 1)
                self.assertIn("launchers", stderr.getvalue())

    def test_health_check_requires_argv_on_command_and_runtime_agents_without_running_it(self):
        (self.root / 'task.md').write_text('Task')
        for execution in ('command: [echo]', 'runtime: codex:model:high\n    instructions: task.md'):
            base = ('repository: org/project\nagents:\n  task:\n    ' + execution +
                    '\n    trigger: ready\n    outcomes: {done: {}}\n')
            self.assertEqual(self.load(base).agents[0].health_check, ())
            configured = self.load(base + '    health-check: [./scripts/check-corpus, --cheap]\n')
            self.assertEqual(configured.agents[0].health_check,
                             (str(self.root / 'scripts/check-corpus'), '--cheap'))
            self.assertEqual(self.load(base + '    health-check: [probe]\n').agents[0].health_check, ('probe',))
            self.assertEqual(self.load(base + '    health-check: [/check-corpus]\n').agents[0].health_check,
                             ('/check-corpus',))
            with patch('ub_agents.agent_health.run_check', side_effect=AssertionError('validation only')):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--config', str(self.path), 'check']), 0)
                for value in ('null', 'true', '1', 'probe', '{}', '[]', '[false]', '[1]', '[null]',
                              "['']", "['   ']", "[probe, '']", '["probe\\0"]'):
                    with self.subTest(execution=execution, value=value):
                        self.path.write_text(base + f'    health-check: {value}\n')
                        with redirect_stderr(io.StringIO()) as error:
                            self.assertEqual(main(['--config', str(self.path), 'check']), 1)
                        self.assertIn('task health-check', error.getvalue())

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
        # Execution and cleanup timeouts do not change the fixed lease.
        self.assertEqual((agent.lease_seconds, agent.timeout_seconds), (1800, 14400))
        self.assertEqual(agent.max_attempts, 7)
        # Relative executables resolve against the configuration's directory.
        self.assertEqual(agent.command, (str(self.root.resolve() / "scripts/task.py"), "--fast"))

    def test_runtime_updates_validation_and_check(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        self.assertIsNone(self.load(base).runtime_updates)
        settings = self.load(base + "runtime-updates:\n  claude: auto\n  codex: off\n  timeout-seconds: 30\n")
        self.assertEqual(settings.runtime_updates.policies, {"claude": "auto", "codex": "off", "gh": "off"})
        self.assertEqual(settings.runtime_updates.timeout_seconds, 30)
        settings = self.load(base + "runtime-updates: {codex: {command: [./update, codex]}}\n")
        self.assertEqual(settings.runtime_updates.policies["codex"], (str(self.root.resolve() / "update"), "codex"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "check"]), 0)
        for value in ["[]", "null", "{unknown: auto}", "{codex: true}", "{codex: false}",
                      "{claude: install}", "{codex: [npm, update]}", "{codex: {command: []}}",
                      "{codex: {command: shell}}", "{codex: {command: [sudo, update]}}",
                      "{codex: {command: [echo], extra: 1}}", "{timeout-seconds: 0}",
                      "{timeout-seconds: 3601}", "{timeout-seconds: .nan}",
                      "{timeout-seconds: true}", "{timeout-seconds: -1}"]:
            with self.subTest(value=value):
                self.path.write_text(base + f"runtime-updates: {value}\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.path), "check"]), 1)

    def test_repository_opts_into_daily_runtime_updates(self):
        settings = load_config(Path(__file__).resolve().parents[1] / "ub-agents.yaml")
        self.assertEqual(settings.runtime_updates.policies, {"claude": "auto", "codex": "auto", "gh": "off"})

    def test_launchers_and_runtime_updates_can_be_configured_together(self):
        settings = self.load('''repository: org/project
launchers: [bot-a, Alice]
runtime-updates: {codex: auto, timeout-seconds: 30}
agents:
  task:
    command: [echo]
    trigger: ready
    outcomes: {done: {}}
''')
        self.assertEqual(settings.launchers, ("bot-a", "Alice"))
        self.assertEqual(settings.runtime_updates.policies, {"claude": "off", "codex": "auto", "gh": "off"})
        self.assertEqual(settings.runtime_updates.timeout_seconds, 30)

    def test_gh_runtime_updates_policies_and_check(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        for value, expected in (("auto", "auto"), ("off", "off"), ("'off'", "off"),
                                ("{command: [./update, gh]}", (str(self.root.resolve() / "update"), "gh"))):
            with self.subTest(value=value):
                settings = self.load(base + f"runtime-updates: {{gh: {value}}}\n")
                self.assertEqual(settings.runtime_updates.policies["gh"], expected)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.path), "check"]), 0)
        for value in ("true", "false", "on", "null", "install", "[brew, upgrade, gh]",
                      "{command: []}", "{command: shell}", "{command: [echo], extra: 1}",
                      "{command: [sudo, update]}", "{command: [/usr/bin/su, update]}",
                      "{command: [doas, update]}", "{command: [pkexec, update]}"):
            with self.subTest(value=value):
                self.path.write_text(base + f"runtime-updates: {{gh: {value}}}\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--config", str(self.path), "check"]), 1)

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

    def test_check_rejects_claude_output_format_overrides(self):
        (self.root / "instructions.md").write_text("Do the task")
        base = '''repository: org/project
agents:
  task:
    runtime: RUNTIME
    instructions: instructions.md
    trigger: ready
    outcomes: {done: {}}
    runtime-args: ARGS
'''
        for runtime in ("claude:model:high", "[codex:model:high, claude:model:high]"):
            for args in ("[--output-format, text]", "[--output-format=json]"):
                with self.subTest(runtime=runtime, args=args):
                    self.path.write_text(base.replace("RUNTIME", runtime).replace("ARGS", args))
                    stderr = io.StringIO()
                    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                        self.assertEqual(main(["--config", str(self.path), "check"]), 1)
                    self.assertIn("task: runtime-args must not set --output-format", stderr.getvalue())
                    self.assertIn("stream-json", stderr.getvalue())

        accepted = "[--verbose, --allowedTools, 'Bash(git *)']"
        configured = self.load(base.replace("RUNTIME", "claude:model:high").replace("ARGS", accepted))
        self.assertEqual(configured.agents[0].runtime_args, ("--verbose", "--allowedTools", "Bash(git *)"))
        for args in ("[--output-format, text]", "[--output-format=json]"):
            with self.subTest(runtime="codex:model:high", args=args):
                configured = self.load(base.replace("RUNTIME", "codex:model:high").replace("ARGS", args))
                self.assertTrue(configured.agents[0].runtime_args)

    def test_runtime_args_reject_unknown_word_placeholders(self):
        (self.root / "instructions.md").write_text("Do the task")
        base = '''repository: org/project
agents:
  task:
    runtime: claude:model:high
    instructions: instructions.md
    trigger: ready
    outcomes: {done: {}}
'''
        for word in ("scrach", "SCRATCH", "scratch-dir", "scratch_dir", "123"):
            placeholder = "{" + word + "}"
            for arg in (placeholder, "--add-dir=" + placeholder, "{scratch}" + placeholder):
                with self.subTest(arg=arg), self.assertRaises(AgentError) as caught:
                    self.load(base + f"    runtime-args: ['{arg}']\n")
                self.assertIn("task: unknown runtime-args placeholder", str(caught.exception))
                self.assertIn(placeholder, str(caught.exception))

    def test_runtime_args_accept_scratch_and_non_placeholder_braces(self):
        (self.root / "instructions.md").write_text("Do the task")
        configured = self.load('''repository: org/project
agents:
  task:
    runtime: codex:model:high
    instructions: instructions.md
    trigger: ready
    outcomes: {done: {}}
    runtime-args: [--allowedTools, "Bash({report_command} report *)", --add-dir, "{scratch}", "--add-dir={scratch}",
                   --settings, '{"a": 1}', --config, 'x={y=true}', '{}', '{two words}']
''')
        self.assertEqual(configured.agents[0].runtime_args,
                         ("--allowedTools", "Bash({report_command} report *)", "--add-dir", "{scratch}", "--add-dir={scratch}", "--settings", '{"a": 1}',
                          "--config", "x={y=true}", "{}", "{two words}"))

    def test_command_arguments_accept_literal_word_placeholders(self):
        configured = self.load('''repository: org/project
agents:
  task:
    command: [echo, "{scratch}", "{other}"]
    trigger: ready
    outcomes: {done: {}}
''')
        self.assertEqual(configured.agents[0].command, ("echo", "{scratch}", "{other}"))

    def test_codex_usage_requires_persistent_sessions(self):
        (self.root / "instructions.md").write_text("Do the task")
        base = '''repository: org/project
agents:
  task:
    runtime: RUNTIME
    instructions: instructions.md
    trigger: ready
    outcomes: {done: {}}
    runtime-args: [--ephemeral]
'''
        for runtime in ("codex:model:high", "[codex:model:high, claude:model:high]"):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(AgentError, "must not set --ephemeral"):
                self.load(base.replace("RUNTIME", runtime))

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
        self.assertEqual(agents[-1].outcomes['changes-requested']['add'], ('needs-changes',))
        for configured in agents:
            instructions = configured.instructions.read_text()
            self.assertIn('--outcome', instructions)
            self.assertNotIn('--status success', instructions)
        self.assertIn("queue:\n  milestones: ignore\n", original)
        self.assertEqual(load_config(self.path).queue, Queue())
        self.assertIn(".ub-agents/", (self.root / ".gitignore").read_text())

    def test_queue_defaults_and_configured_priority(self):
        base = "repository: org/project\nagents:\n  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n"
        for extra in ("", "queue: {}\n", "queue:\n  milestones: ignore\n"):
            with self.subTest(extra=extra):
                self.assertEqual(self.load(base + extra).queue, Queue())
        self.assertEqual(self.load(base + "queue:\n  milestones: order\n").queue, Queue("order"))
        self.assertEqual(self.load(base + "queue:\n  milestones: gate\n").queue, Queue("gate"))
        self.assertEqual(self.load(base + "queue:\n  dependencies: wait\n").queue, Queue())
        self.assertEqual(self.load(base + "queue:\n  dependencies: ignore\n").queue,
                         Queue(dependencies="ignore"))
        extra = "queue:\n  priority:\n    labels: [urgent, normal, low]\n"
        self.assertEqual(self.load(base + extra).queue.priority, Priority(("urgent", "normal", "low")))
        self.assertEqual(self.load(base + extra + "    default: normal\n").queue.priority.default, "normal")

    def test_check_accepts_all_milestone_modes_and_names_them_for_invalid_values(self):
        for mode in ("gate", "order", "ignore", "oldest"):
            with self.subTest(mode=mode):
                self.path.write_text(f"repository: org/project\nqueue:\n  milestones: {mode}\nagents:\n"
                                     "  task:\n    command: [echo]\n    trigger: ready\n    outcomes: {done: {}}\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(["--config", str(self.path), "check"]),
                                     1 if mode == "oldest" else 0)
                if mode == "oldest":
                    self.assertIn("milestones must be gate, order or ignore", errors.getvalue())

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
  milestones: order
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
        self.assertEqual(configured.queue, Queue("order", Priority(("urgent", "normal"), "normal")))
        self.assertEqual(configured.agents[0].outcomes,
                         {"handed-off": {"add": ("needs-review",), "remove": ("old",)}})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "check"]), 0)
