from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from rich.console import Console
import yaml

from tests import test_init_flow
from tests.test_init_flow import Script, flat
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.doctor import Doctor
from ub_agents.errors import AgentError
from ub_agents.init.edit import RULE, UnsafeEdit, additions
from ub_agents.init.extend import ExistingFlow
from ub_agents.init.flow import Flow
from ub_agents.init.prompts import Transcript
from ub_agents.init.setup import Choices, apply_plan, make_plan


class ExistingSetupTests(unittest.TestCase):
    setUp = test_init_flow.FlowTests.setUp

    def starter(self):
        plan = make_plan(self.path, 'org/project', Choices('claude:model:high', permissions=True))
        result = apply_plan(plan, self.github)
        self.assertTrue(result.ok, result.error)
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'feature'
        self.runner.responses[('git', '-C', str(self.root), 'for-each-ref', '--format=%(refname:short)',
                               'refs/heads/', 'refs/remotes/origin/')] = ''
        self.runner.responses[('git', '-C', str(self.root), 'ls-remote', '--heads', 'origin')] = ''
        self.before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def run_flow(self, *, boards=True, priorities=False, script=None, unattended=False):
        self.script = script or Script()
        self.output = io.StringIO()
        flow = ExistingFlow(self.path, None, None, lambda repo: self.github, self.script,
                            Transcript(Console(file=self.output, width=120, highlight=False, soft_wrap=True)),
                            unattended=unattended, create_boards=boards, create_priorities=priorities)
        flow.doctor = Doctor(runner=self.runner, which=lambda name: '/bin/' + name if name in self.installed else None,
                             github=self.github)
        with patch('subprocess.run', self.runner):
            code = flow.run()
        return code, flat(self.output.getvalue())

    def test_starter_reuses_and_creates_boards_then_rerun_is_noop(self):
        self.starter()
        code, text = self.run_flow()
        self.assertEqual(code, 0, text)
        configured = load_config(self.path)
        self.assertEqual(len([role for role in configured.agents if role.retrospectives]), 5)
        self.assertEqual(next(role for role in configured.agents if role.name == 'implementer').retrospectives, 203)
        for role in configured.agents:
            self.assertIn(RULE, role.runtime_args_for(role.runtimes[0]))
        result = Doctor(runner=self.runner, which=lambda name: '/bin/' + name).run(self.path, offline=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(sum(write[0] == 'create-board' for write in self.github.writes), 4)
        self.assertEqual(self.script.asked, ['Write setup?'])
        self.assertNotIn('Tailor loop policy', text)
        self.assertNotIn('gh issue create', text)
        # Removing just the insertions recovers every byte of the original file.
        original = self.path.read_text()
        for role in configured.agents:
            original = original.replace(f'    retrospectives: {role.retrospectives}\n', '')
        original = original.replace(', ' + json.dumps(RULE), '')
        self.assertEqual(original.encode(), self.before[self.path.relative_to(self.root)])
        for path, content in self.before.items():
            if path != self.path.relative_to(self.root):
                self.assertEqual((self.root / path).read_bytes(), content)
        self.assertEqual({path.relative_to(self.root) for path in self.root.rglob('*') if path.is_file()}, set(self.before))
        writes = self.github.writes.copy()
        before = self.path.read_bytes()
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('Nothing to do', text)
        self.assertEqual(self.github.writes, writes)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.script.asked, [])

    def test_custom_agents_own_arguments_codex_and_existing_boards(self):
        self.starter()
        content = self.path.read_text().replace('  implementer:\n', '  implementer:\n    retrospectives: 203\n')
        content = content.replace('  integrator:\n    runtime: "claude:model:high"',
                                  '  integrator:\n    runtime: "codex:model:high"\n'
                                  '    runtime-args: [--sandbox, danger-full-access]')
        custom = ('\n  custom:\n    runtime: ["claude:model:high", "codex:model:high"]\n'
                  '    trigger: custom\n    outcomes: {done: {}}\n    instructions: .agents/implementer.md\n'
                  '    runtime-args:\n      claude:\n        - --allowedTools # keep this comment\n'
                  '        - "Bash(git *)"\n        - --add-dir\n        - "{scratch}"\n'
                  '      codex: [--sandbox, danger-full-access]\n')
        content = content.replace('\nlimits:', custom + '\nlimits:')
        self.path.write_text(content)
        code, text = self.run_flow()
        self.assertEqual(code, 0, text)
        roles = {role.name: role for role in load_config(self.path).agents}
        self.assertIn(RULE, roles['custom'].runtime_args['claude'])
        self.assertEqual(roles['custom'].runtime_args['codex'], ('--sandbox', 'danger-full-access'))
        self.assertEqual(roles['integrator'].runtime_args, ('--sandbox', 'danger-full-access'))
        self.assertEqual(roles['implementer'].retrospectives, 203)
        self.assertIn('--allowedTools # keep this comment', self.path.read_text())
        titles = [write[2]['title'] for write in self.github.writes if write[0] == 'create-board']
        self.assertIn('Custom retrospectives', titles)
        self.assertNotIn('Implementer retrospectives', titles)

    def test_own_list_and_arguments_without_allowed_tools(self):
        self.starter()
        content = self.path.read_text().replace('  implementer:\n',
                    '  implementer:\n    runtime-args: [--allowedTools, "Bash(git *)", --add-dir, "{scratch}"]\n')
        content = content.replace('  reviewer:\n', '  reviewer:\n    runtime-args: [--permission-mode, acceptEdits]\n')
        self.path.write_text(content)
        code, text = self.run_flow()
        self.assertEqual(code, 0, text)
        roles = {role.name: role for role in load_config(self.path).agents}
        self.assertIn(RULE, roles['implementer'].runtime_args)
        self.assertEqual(roles['reviewer'].runtime_args, ('--permission-mode', 'acceptEdits'))

    def test_priorities_insert_inside_queue_or_add_queue_without_other_edits(self):
        for existing_queue in (True, False):
            with self.subTest(existing_queue=existing_queue):
                if not self.path.exists():
                    self.starter()
                content = self.before[self.path.relative_to(self.root)].decode()
                if not existing_queue:
                    content = content.replace('queue:\n  milestones: ignore\n  dependencies: wait\n', '')
                self.path.write_text(content)
                self.github.label_names = ['P1']
                code, text = self.run_flow(boards=False, priorities=True, script=Script(Priority='p1, p3, p2'))
                self.assertEqual(code, 0, text)
                self.assertEqual(yaml.safe_load(self.path.read_text())['queue']['priority']['labels'], ['p1', 'p3', 'p2'])
                self.assertEqual([write[1] for write in self.github.writes if write[0] == 'create-label'], ['p3', 'p2'])
                self.assertEqual([write[3] for write in self.github.writes if write[0] == 'create-label'],
                                 ['c98a86', 'd4d4d4'])
                self.assertEqual(self.script.asked, ['Priority labels, highest first', 'Write setup?'])
                self.assertEqual(self.path.read_text().replace('  priority:\n    labels: ["p1", "p3", "p2"]\n', '')
                                 .replace('queue:\n', '' if not existing_queue else 'queue:\n'), content)
                self.github.writes.clear()
                before = self.path.read_bytes()
                code, text = self.run_flow(boards=False, priorities=True)
                self.assertEqual(code, 0)
                self.assertIn('Nothing to do', text)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.github.writes, [])

    def test_both_flags_confirm_once_open_one_pr_and_avoid_old_branch_names(self):
        self.starter()
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'main'
        self.runner.responses[('git', '-C', str(self.root), 'for-each-ref', '--format=%(refname:short)',
                               'refs/heads/', 'refs/remotes/origin/')] = 'main\nsetup-ub-agents\n'
        self.runner.responses[('git', '-C', str(self.root), 'ls-remote', '--heads', 'origin')] = 'abc\trefs/heads/setup-ub-agents-2\n'
        code, text = self.run_flow(priorities=True, unattended=True)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.script.asked, ['Priority labels, highest first', 'Write setup?'])
        commands = [command for command, _ in self.runner.calls]
        prs = [command for command in commands if command[:3] == ('gh', 'pr', 'create')]
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0][prs[0].index('--head') + 1], 'setup-ub-agents-3')
        adds = [command for command in commands if command[:4] == ('git', '-C', str(self.root), 'add')]
        self.assertEqual(adds[0][4:], ('--', 'ub-agents.yaml'))
        self.assertIn('Checks', text)
        self.assertIn('Setup PR opened', text)

    def test_no_config_plain_init_refusal_and_cancel_write_nothing(self):
        with self.assertRaisesRegex(AgentError, 'run plain ub-agents init'):
            self.run_flow()
        self.assertEqual(self.github.reads, [])
        self.starter()
        with self.assertRaisesRegex(AgentError, 'Starter files already exist'):
            Flow(self.path, 'org/project', None, lambda repo: self.github, Script(),
                 Transcript(Console(file=io.StringIO())), unattended=True).run()
        code, text = self.run_flow(priorities=True, script=Script(Write=False))
        self.assertEqual(code, 1)
        self.assertIn('Nothing was written', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])
        self.assertEqual(self.github.writes, [])

    def test_invalid_config_reports_a_configuration_error_before_github(self):
        self.path.write_text('agents: [broken')
        with self.assertRaisesRegex(AgentError, 'Cannot read proposed configuration'):
            self.run_flow()
        self.assertEqual(self.github.reads, [])

    def test_dirty_checkout_stops_even_with_default_answers(self):
        self.starter()
        self.runner.responses[('git', '-C', str(self.root), '--no-optional-locks', 'status',
                               '--porcelain=v1', '--untracked-files=all')] = '?? local.txt'
        code, text = self.run_flow(priorities=True, unattended=True)
        self.assertEqual(code, 1)
        self.assertIn('commit or stash', text)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])

    def test_board_preflight_errors_prevent_all_writes(self):
        self.starter()
        disabled = ("GitHub Discussions are turned off in org/project, so retrospective boards can't be created.\n"
                    'Turn them on under Settings > General > Features > Discussions '
                    '(https://github.com/org/project/settings; needs admin),\n'
                    'then run ub-agents init --create-boards again.')
        for problem, remedy in [('disabled', disabled), ('permission', 'cannot create requested boards'),
                                ('duplicate', 'set the board numbers'), ('locked', 'repair Discussions write access')]:
            with self.subTest(problem=problem):
                self.github.discussion_metadata['hasDiscussionsEnabled'] = problem != 'disabled'
                self.github.discussion_metadata['viewerPermission'] = 'READ' if problem == 'permission' else 'WRITE'
                self.github.boards = {203: self.github.board(203, 'Implementer retrospectives')}
                if problem == 'duplicate':
                    self.github.boards[204] = self.github.board(204, 'Implementer retrospectives')
                if problem == 'locked':
                    self.github.boards[203]['locked'] = True
                with self.assertRaises(AgentError) as error:
                    self.run_flow(priorities=True)
                self.assertIn(remedy, str(error.exception))
                if problem == 'disabled':
                    self.assertEqual(str(error.exception), remedy)
                self.assertEqual(self.github.writes, [])
                self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])

    def test_cli_disabled_discussions_error_preserves_line_breaks(self):
        self.starter()
        self.github.discussion_metadata['hasDiscussionsEnabled'] = False
        doctor = Doctor(runner=self.runner, which=lambda name: '/bin/' + name if name in self.installed else None,
                        github=self.github)
        with patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('ub_agents.init.probes.Doctor', return_value=doctor), \
                patch('subprocess.run', self.runner), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            code = main(['--config', str(self.path), 'init', '--create-boards', '--use-defaults'])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(),
                         "ub-agents: GitHub Discussions are turned off in org/project, "
                         "so retrospective boards can't be created.\n"
                         'Turn them on under Settings > General > Features > Discussions '
                         '(https://github.com/org/project/settings; needs admin),\n'
                         'then run ub-agents init --create-boards again.\n')
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])

    def test_unusual_layout_keeps_whole_file_and_prints_manual_entries(self):
        self.starter()
        data = yaml.safe_load(self.path.read_text())
        content = yaml.safe_dump(data, default_flow_style=True)
        self.path.write_text(content)
        code, text = self.run_flow(priorities=True)
        self.assertEqual(code, 1)
        self.assertIn('manual edit', text)
        self.assertIn('agents.implementer:', text)
        self.assertIn('retrospectives: 203', text)
        self.assertIn('labels:', text)
        self.assertIn(RULE, text)
        self.assertEqual(self.path.read_text(), content)
        self.assertEqual(self.github.writes, [])

    def test_partial_board_failure_reports_effects_and_can_retry(self):
        self.starter()
        self.github.after_create = lambda board: setattr(self.github, 'create_board_error', AgentError('creation failed'))
        code, text = self.run_flow(priorities=True)
        self.assertEqual(code, 1)
        self.assertIn('Created board https://github.com/org/project/discussions/204', text)
        self.assertIn('Board creation may have completed', text)
        self.assertIn('agents.issue-preparer: retrospectives: 204', text)
        self.assertIn('queue: priority: labels:', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])
        self.github.after_create = self.github.create_board_error = None
        code, text = self.run_flow(priorities=True)
        self.assertEqual(code, 0, text)
        self.assertEqual(sum(write[0] == 'create-board' for write in self.github.writes), 4)

    def test_partial_label_failure_reports_created_labels_and_all_config_entries(self):
        self.starter()
        create = self.github.create_label
        def fail_second(name, description, color):
            if name == 'p2':
                raise AgentError('label failed')
            create(name, description, color)
        with patch.object(self.github, 'create_label', side_effect=fail_second):
            code, text = self.run_flow(priorities=True, script=Script(Priority='p1, p2'))
        self.assertEqual(code, 1)
        self.assertIn('Created label p1', text)
        self.assertIn('labels: [p1, p2]', text)
        self.assertIn('retrospectives: 203', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])

    def test_config_changed_during_confirmation_is_preserved(self):
        self.starter()
        script = Script()
        def confirm(*args):
            self.path.write_text(self.path.read_text() + '# concurrent change\n')
            return True
        script.confirm = confirm
        code, text = self.run_flow(script=script)
        self.assertEqual(code, 1)
        self.assertIn('changed during setup', text)
        self.assertTrue(self.path.read_text().endswith('# concurrent change\n'))
        self.assertEqual(self.github.writes, [])

    def test_local_overrides_changed_during_confirmation_are_preserved(self):
        self.starter()
        script = Script()
        def confirm(*args):
            (self.root / 'ub-agents.local.yaml').write_text('poll-seconds: 17\n')
            return True
        script.confirm = confirm
        code, text = self.run_flow(script=script)
        self.assertEqual(code, 1)
        self.assertIn('ub-agents.local.yaml changed', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])
        self.assertEqual(self.github.writes, [])

    def test_newly_duplicated_board_title_prevents_all_mutations(self):
        self.starter()
        script = Script()
        def confirm(*args):
            self.github.boards[204] = self.github.board(204, 'Implementer retrospectives')
            return True
        script.confirm = confirm
        code, text = self.run_flow(priorities=True, script=script)
        self.assertEqual(code, 1)
        self.assertIn('Several boards', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])
        self.assertEqual(self.github.writes, [])

    def test_existing_priorities_are_kept_when_only_boards_are_missing(self):
        self.starter()
        content = self.path.read_text().replace('queue:\n', 'queue:\n  priority: {labels: [p2, p1], default: p1} # keep\n')
        self.path.write_text(content)
        code, text = self.run_flow(priorities=True)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.script.asked, ['Write setup?'])
        self.assertIn('priority: {labels: [p2, p1], default: p1} # keep', self.path.read_text())
        self.assertFalse(any(write[0] == 'create-label' for write in self.github.writes))

    def test_failed_atomic_replace_keeps_file_and_reports_remaining_entries(self):
        self.starter()
        with patch('ub_agents.init.extend.os.replace', side_effect=OSError('disk error')):
            code, text = self.run_flow(priorities=True)
        self.assertEqual(code, 1)
        self.assertIn('disk error', text)
        self.assertIn('Created label prio:urgent', text)
        self.assertIn('Entries still to add', text)
        self.assertEqual(self.path.read_bytes(), self.before[self.path.relative_to(self.root)])
        self.assertEqual(list(self.root.glob('.ub-agents-init-*')), [])

    def test_cli_routes_both_flags_and_non_tty_uses_defaults(self):
        self.starter()
        with patch.object(ExistingFlow, 'run', autospec=True, return_value=0) as run:
            self.assertEqual(main(['--config', str(self.path), 'init', '--create-boards', '--create-priorities',
                                   '--use-defaults']), 0)
        flow = run.call_args.args[0]
        self.assertTrue(flow.create_boards)
        self.assertTrue(flow.create_priorities)
        self.assertTrue(flow.unattended)


class EditTests(unittest.TestCase):
    def test_existing_rule_is_not_duplicated_and_line_endings_stay_intact(self):
        content = ('# keep\r\nrepository: org/project\r\nagents:\r\n  worker:\r\n'
                   '    runtime-args: [--allowedTools, ' + json.dumps(RULE) + '] # keep\r\n')
        result = additions(content, {'worker': 203}, (), ['worker'])
        self.assertEqual(result.count(RULE), 1)
        self.assertEqual(result.replace('    retrospectives: 203\r\n', ''), content)

    def test_aliases_require_manual_edit(self):
        content = 'agents:\n  worker: &worker\n    runtime-args: []\n  other: *worker\n'
        with self.assertRaisesRegex(UnsafeEdit, 'anchors or aliases'):
            additions(content, {'worker': 203}, (), [])
