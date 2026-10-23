from contextlib import chdir, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.errors import AgentError
from tests.support import RecordingRunner


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / 'ub-agents.yaml'
        self.enterContext(patch('ub_agents.doctor.subprocess.run', RecordingRunner(self.root)))
        self.enterContext(patch('ub_agents.doctor.shutil.which', return_value='/tools/git'))
        self.local = self.root / 'ub-agents.local.yaml'
        (self.root / 'role.md').write_text('Synthetic instructions')
        self.path.write_text('''repository: org/project
poll-seconds: 45
approvals: on
limits:
  max-attempts: 7
  retry-backoff-seconds: 12
queue:
  priority:
    labels: [urgent, normal]
    default: normal
agents:
  implementer:
    runtime: [codex:shared:high, claude:shared-other:low]
    instructions: role.md
    trigger: [ready, needs-changes]
    outcomes:
      handed-off: {add: [needs-review], remove: [old]}
  reviewer:
    runtime: claude:review:high
    instructions: role.md
    trigger: needs-review
    different-runtime-from: implementer
    outcomes: {done: {}}
''')

    def test_missing_empty_and_comment_only_local_files_leave_base_unchanged(self):
        base = load_config(self.path)
        for content in ('', '\n# Machine settings\n', '---\n# Machine settings\n', '{}\n'):
            with self.subTest(content=content):
                self.local.write_text(content)
                self.assertEqual(load_config(self.path), base)
        self.local.unlink()
        self.assertEqual(load_config(self.path), base)

    def test_partial_mappings_inherit_other_keys_and_agents_recursively(self):
        base = load_config(self.path)
        self.local.write_text('''limits: {max-attempts: 9}
queue: {priority: {default: urgent}}
agents:
  implementer:
    agent-timeout-minutes: 10
    outcomes:
      handed-off: {add: [review-on-machine]}
''')
        effective = load_config(self.path)
        implementer = effective.agents[0]
        self.assertEqual(effective.poll_seconds, base.poll_seconds)
        self.assertEqual(effective.repository, base.repository)
        self.assertEqual(effective.queue.priority.labels, ('urgent', 'normal'))
        self.assertEqual(effective.queue.priority.default, 'urgent')
        self.assertEqual(implementer.runtimes, base.agents[0].runtimes)
        self.assertEqual(implementer.instructions, self.root / 'role.md')
        self.assertEqual(implementer.triggers, ('ready', 'needs-changes'))
        self.assertEqual(implementer.max_attempts, 9)
        self.assertEqual(implementer.backoff_seconds, 12)
        self.assertEqual(implementer.timeout_seconds, 600)
        self.assertEqual(implementer.outcomes['handed-off'],
                         {'add': ('review-on-machine',), 'remove': ('old',)})
        self.assertEqual(effective.agents[1].max_attempts, 9)
        self.assertEqual(effective.agents[1].runtimes, base.agents[1].runtimes)

    def test_scalar_and_ordered_runtime_list_replace_the_whole_base_value(self):
        base = load_config(self.path)
        for runtime, expected in (
                ('claude:machine:high', ('claude:machine:high',)),
                ('[claude:machine:high, codex:shared:high]',
                 ('claude:machine:high', 'codex:shared:high')),
                ('[codex:shared:high, codex:machine:low]',
                 ('codex:shared:high', 'codex:machine:low'))):
            with self.subTest(runtime=runtime):
                self.local.write_text(f'agents:\n  implementer:\n    runtime: {runtime}\n')
                effective = load_config(self.path)
                self.assertEqual(tuple(r.name for r in effective.agents[0].runtimes), expected)
                self.assertEqual(effective.agents[1], base.agents[1])
                self.assertEqual(effective.agents[1].different_from, 'implementer')

    def test_other_scalars_and_lists_replace_including_empty_lists(self):
        self.local.write_text('''poll-seconds: 10
stop-labels: []
agents:
  implementer: {trigger: ready}
''')
        effective = load_config(self.path)
        self.assertEqual(effective.poll_seconds, 10)
        self.assertEqual(effective.stop_labels, ())
        self.assertEqual(effective.agents[0].triggers, ('ready',))

    def test_local_only_agents_must_supply_a_complete_definition(self):
        self.local.write_text('''agents:
  machine:
    command: [echo, local]
    trigger: machine-ready
    outcomes: {done: {}}
''')
        effective = load_config(self.path)
        self.assertEqual([a.name for a in effective.agents], ['implementer', 'reviewer', 'machine'])
        self.assertEqual(effective.agents[-1].command, ('echo', 'local'))
        self.local.write_text('agents:\n  machine: {command: [echo]}\n')
        with self.assertRaises(AgentError) as error:
            load_config(self.path)
        self.assertIn(str(self.local), str(error.exception))
        self.assertIn('machine trigger', str(error.exception))

    def test_explicit_null_is_validated_as_a_supplied_value(self):
        for content, reason in (
                ('poll-seconds: null', 'poll-seconds must be a number'),
                ('agents: null', 'agents must be a nonempty mapping'),
                ('agents: {implementer: null}', 'agent implementer must be a mapping'),
                ('agents: {implementer: {runtime: null}}', 'implementer runtime'),
                ('queue: {priority: {default: null}}', 'default must be one of labels')):
            with self.subTest(content=content):
                self.local.write_text(content)
                with self.assertRaises(AgentError) as error:
                    load_config(self.path)
                self.assertIn(str(self.local), str(error.exception))
                self.assertIn(reason, str(error.exception))
        # Nullable fields retain their existing schema behavior.
        self.local.write_text('agents: {reviewer: {different-runtime-from: null}}')
        self.assertIsNone(load_config(self.path).agents[1].different_from)

    def test_invalid_local_yaml_and_schema_errors_name_the_local_file(self):
        for content, reason in (
                ('agents: [', 'Cannot read configuration'),
                ('poll-seconds: 1\npoll-seconds: 2', 'unique strings'),
                ('agents: {implementer: {runtime: codex:a:high, runtime: codex:b:high}}', 'unique strings'),
                ('null', 'configuration must be a mapping'),
                ('~', 'configuration must be a mapping'),
                ('!!null ""', 'configuration must be a mapping'),
                ('!!null', 'configuration must be a mapping'),
                ('[]', 'configuration must be a mapping'),
                ('false', 'configuration must be a mapping'),
                ('unknown: true', 'Unknown configuration keys: unknown'),
                ('agents: {implementer: {unknown: true}}', 'Unknown agent implementer keys: unknown'),
                ('queue: {priority: {unknown: true}}', 'Unknown queue priority keys: unknown'),
                ('agents: {implementer: {runtime: opencode:machine:high}}', 'runtime cli must be one of codex, claude'),
                ('agents: {implementer: {runtime: []}}', 'nonempty list'),
                ('agents: {implementer: {kind: []}}', 'kind must be issue, pr, or either'),
                ('agents: {implementer: {instructions: missing.md}}', 'does not exist')):
            with self.subTest(content=content):
                self.local.write_text(content)
                with redirect_stdout(io.StringIO()) as error:
                    self.assertEqual(main(['--config', str(self.path), 'doctor', '--offline']), 1)
                self.assertIn(str(self.local), error.getvalue())
                self.assertIn(reason, error.getvalue())

    def test_unreadable_local_file_names_the_file(self):
        self.local.write_text('poll-seconds: 10')
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.local:
                raise PermissionError('Synthetic unreadable local file')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertRaises(AgentError) as error:
            load_config(self.path)
        self.assertIn(str(self.local), str(error.exception))
        self.assertIn('Synthetic unreadable local file', str(error.exception))
        self.local.write_bytes(b'\xff')
        with self.assertRaises(AgentError) as error:
            load_config(self.path)
        self.assertIn(str(self.local), str(error.exception))
        self.local.unlink()
        self.local.mkdir()
        with self.assertRaises(AgentError) as error:
            load_config(self.path)
        self.assertIn(str(self.local), str(error.exception))

    def test_local_yaml_preserves_policy_spellings_and_normal_booleans(self):
        self.local.write_text('''approvals: off
runtime-updates: {codex: off, claude: off, gh: off, ub: off}
agents: {implementer: {worktree: true}}
''')
        effective = load_config(self.path)
        self.assertEqual(effective.approvals, 'off')
        self.assertEqual(effective.runtime_updates.policies, {'codex': 'off', 'claude': 'off', 'gh': 'off', 'ub': 'off'})
        self.assertIs(effective.agents[0].worktree, True)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--config', str(self.path), 'doctor', '--offline', '--verbose']), 0)
        self.assertIn('Approvals: off (config)', output.getvalue())

    def test_explicit_config_uses_its_resolved_directory_and_ignores_cwd_local_file(self):
        selected = self.root / 'selected'
        selected.mkdir()
        custom = selected / 'custom.yaml'
        custom.write_text(self.path.read_text())
        (selected / 'role.md').write_text('Selected instructions')
        self.local.write_text('invalid: unrelated local configuration')
        with chdir(self.root), patch('ub_agents.doctor.subprocess.run', RecordingRunner(selected)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(custom), 'doctor', '--offline']), 0)
            self.assertEqual(load_config(custom).poll_seconds, 45)
            (selected / 'ub-agents.local.yaml').write_text('poll-seconds: 9')
            # Follow the selected base symlink before finding its local layer.
            alias = self.root / 'alias.yaml'
            alias.symlink_to(custom)
            effective = load_config(alias)
            self.assertEqual(effective.root, selected)
            self.assertEqual(effective.poll_seconds, 9)
            self.assertEqual(effective.agents[0].instructions, selected / 'role.md')
            self.assertEqual(main(['doctor', '--offline', '--config', str(alias)]), 0)

    def test_local_layer_cannot_replace_a_missing_or_nonmapping_base(self):
        self.local.write_text(self.path.read_text())
        self.path.unlink()
        with self.assertRaises(AgentError) as error:
            load_config(self.path)
        self.assertIn(str(self.path), str(error.exception))
        for content in ('', 'null', '[]'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaisesRegex(AgentError, 'configuration must be a mapping'):
                    load_config(self.path)
