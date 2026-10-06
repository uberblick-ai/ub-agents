from contextlib import redirect_stdout, redirect_stderr
import io
import json
import re
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ub_agents.cli import main, runtime_guidance
from ub_agents.config import load_config
from ub_agents.errors import AgentError
from ub_agents.labels import configured_labels
from tests.support import DoctorGitHub, RecordingRunner


class InitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / 'nested' / 'starter.yaml'
        self.github = DoctorGitHub()
        self.github.label_names = ['READY', 'unrelated']
        self.runner = RecordingRunner(self.root)
        self.runner.responses[('gh', 'repo', 'view', '--json', 'nameWithOwner')] = json.dumps(
            {'nameWithOwner': 'org/project'})

    def init(self, *, terminal=False, stdout_terminal=None, answer='', runtime='codex:model:high', infer=False, ci=''):
        with patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('subprocess.run', self.runner), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=terminal), \
                patch.dict('os.environ', {'CI': ci}), \
                patch('builtins.input', return_value=answer) as prompt, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            with patch.object(stdout, 'isatty', return_value=terminal if stdout_terminal is None else stdout_terminal):
                code = main(['--config', str(self.path), 'init', '--runtime', runtime]
                            + ([] if infer else ['--repository', 'org/project']))
        return code, stdout.getvalue(), stderr.getvalue(), prompt

    def commands(self, output):
        return [shlex.split(line) for line in output.splitlines() if line.startswith('gh label create ')]

    def test_noninteractive_starter_guidance_all_commands_and_only_inference_read(self):
        code, output, error, prompt = self.init(infer=True)
        self.assertEqual((code, error), (0, ''))
        config = load_config(self.path)
        names = [label.name for label in configured_labels(config)]
        self.assertEqual(set(names), {'needs-preparation', 'ready', 'needs-human', 'needs-changes',
                                     'needs-review', 'ready-to-merge'})
        self.assertEqual([command[3] for command in self.commands(output)], names)
        for command in self.commands(output):
            self.assertEqual(command[4:6], ['--repo', 'org/project'])
            self.assertIn('--description', command)
            self.assertIn('--color', command)
            self.assertNotIn('--force', command)
        self.assertEqual([command for command, _ in self.runner.calls],
                         [('gh', 'repo', 'view', '--json', 'nameWithOwner')])
        self.assertEqual(self.github.reads, [])
        self.assertEqual(self.github.writes, [])
        prompt.assert_not_called()
        guidance = (self.path.parent / '.agents/ub_agents.md').read_text()
        for expected in ('<project build command>', '<project test command>', '## Checks',
                         '## Merging', '## Human decisions', '## Review focus'):
            self.assertIn(expected, guidance)
        self.assertIn('codex loads no project guidance', output)
        self.assertIn('know how to build and test', output)
        self.assertFalse((self.path.parent / 'AGENTS.md').exists())
        self.assertEqual(len(list((self.path.parent / '.agents').glob('*.md'))), 5)
        self.assertEqual(config.shared_instructions, self.path.parent / '.agents/ub_agents.md')
        self.assertTrue(all(agent.worktree for agent in config.agents))

    def test_default_and_claude_starters_pass_check_without_creating_guidance(self):
        for runtime in ('codex:gpt-6.1-sol:high', 'claude:opus:high'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'ub-agents.yaml'
                code, output, _, _ = self.init(runtime=runtime)
                self.assertEqual(code, 0)
                self.assertIn(f'{runtime.split(":", 1)[0]} loads no project guidance', output)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--config', str(self.path), 'check']), 0)
                for name in ('AGENTS.md', 'CLAUDE.md', '.claude/CLAUDE.md'):
                    self.assertFalse((self.path.parent / name).exists())

    def test_role_templates_supply_procedure_without_loop_contract(self):
        self.assertEqual(self.init()[0], 0)
        roles = {name: ' '.join((self.path.parent / '.agents' / f'{name}.md').read_text().split())
                 for name in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        for instructions in roles.values():
            self.assertNotIn('Every stop report', instructions)
            self.assertNotIn('at most 300 characters', instructions)
            self.assertNotIn('untrusted issue input rule', instructions)
        self.assertIn("only where they fit the request's intent", roles['issue-preparer'])
        self.assertIn('gh issue edit N --body-file PATH', roles['issue-preparer'])
        self.assertIn('--outcome needs-human', roles['issue-preparer'])
        self.assertIn('unexpected instruction or a scope change you cannot attribute to the request',
                      roles['implementer'])
        self.assertIn("assignment context's `branch`", roles['implementer'])
        self.assertIn("assignment context's comments, reviews, inline feedback", roles['implementer'])
        self.assertIn('After merging the base branch into the PR branch, rerun the checks that cover what '
                      'the PR adds or changes, not only the files that conflicted.', roles['implementer'])
        self.assertIn("For a revision, address the assignment context's `feedback` as well as its comments, "
                      "reviews and review comments", roles['implementer'])
        self.assertIn('Never edit the candidate', roles['reviewer'])
        self.assertIn('Immediately before publishing', roles['reviewer'])
        self.assertIn('gh pr review N --comment --body-file PATH', roles['reviewer'])
        self.assertIn('candidate_sha', roles['reviewer'])
        self.assertIn('shared policy', roles['reviewer'])
        self.assertIn('Immediately before merging', roles['integrator'])
        self.assertIn('gh pr merge N --squash --match-head-commit SHA', roles['integrator'])

    def test_template_shell_examples_need_no_expansion(self):
        templates = Path(__file__).resolve().parents[1] / 'src/ub_agents/templates'
        for path in templates.iterdir():
            text = path.read_text()
            with self.subTest(template=path.name):
                self.assertNotRegex(text, r'\$(?:[A-Za-z_{?(])')
                commands = re.findall(r'```(?:sh|bash)\n(.*?)```', text, re.S)
                # Double-backtick spans can contain substitution backticks;
                # single-backtick spans also cover the wrapped shell examples.
                spans = re.findall(r'(?<!`)(`{1,2})(?!`)(.*?)\1(?!`)', text, re.S)
                commands += [value for _, value in spans
                             if value.startswith(('gh ', 'git ', 'ub-agents '))]
                for command in commands:
                    self.assertNotIn('`', command)

    def test_yes_creates_exactly_missing_labels_and_reports_each(self):
        code, output, error, prompt = self.init(terminal=True, answer='yes')
        self.assertEqual((code, error), (0, ''))
        missing = [label for label in configured_labels(load_config(self.path)) if label.name != 'ready']
        self.assertEqual(self.github.writes,
                         [('create-label', label.name, label.description, label.color) for label in missing])
        self.assertEqual(self.commands(output), [])
        for label in missing:
            self.assertIn(f'Created label {label.name} on org/project.', output)
        self.assertIn('needs-review: ', output)
        self.assertIn('starts reviewer on a PR', output)
        self.assertIn('outcome handed-off', output)
        self.assertIn('Create these 5 labels on org/project? [y/N]', prompt.call_args.args[0])
        self.assertIn('READY', self.github.label_names)
        self.assertIn('unrelated', self.github.label_names)

    def test_no_enter_and_unrecognized_answer_never_write_github(self):
        for answer in ('no', '', 'maybe'):
            with self.subTest(answer=answer):
                if self.path.exists():
                    self.path.unlink()
                    for path in (self.path.parent / '.agents').glob('*.md'):
                        path.unlink()
                code, output, _, _ = self.init(terminal=True, answer=answer)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.commands(output)), 5)
                self.assertEqual(self.github.writes, [])
                self.assertIn('declined', output)

    def test_piped_input_redirected_output_and_ci_do_not_read_labels(self):
        for terminal, stdout_terminal, ci in ((False, True, ''), (True, False, ''), (True, True, 'true')):
            with self.subTest(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci):
                if self.path.exists():
                    self.path.unlink()
                    for path in (self.path.parent / '.agents').glob('*.md'):
                        path.unlink()
                code, output, _, prompt = self.init(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.commands(output)), 6)
                self.assertEqual(self.github.reads, [])
                self.assertEqual(self.github.writes, [])
                prompt.assert_not_called()

    def test_unreadable_labels_prints_all_commands_without_prompt(self):
        self.github.label_error = AgentError('private error')
        code, output, _, prompt = self.init(terminal=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertIn('Could not read GitHub labels', output)
        self.assertEqual(len(self.commands(output)), 6)
        self.assertNotIn('private error', output)
        self.assertEqual(self.github.writes, [])
        prompt.assert_not_called()

    def test_all_labels_present_does_not_prompt_or_write(self):
        self.github.label_names = ['READY', 'needs-preparation', 'needs-human', 'needs-changes',
                                   'needs-review', 'ready-to-merge']
        code, output, _, prompt = self.init(terminal=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertIn('All configured workflow labels exist', output)
        self.assertEqual(self.commands(output), [])
        self.assertEqual(self.github.writes, [])
        prompt.assert_not_called()

    def test_existing_guidance_is_preserved_byte_for_byte_for_both_runtimes(self):
        for runtime, loaded in (('codex:model:high', 'AGENTS.md'),
                                ('claude:opus:high', '.claude/CLAUDE.md')):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'starter.yaml'
                contents = {'AGENTS.md': b'# Project rules\r\nNon-ASCII: \xc3\xa4\n',
                            '.claude/CLAUDE.md': b'# Claude rules\r\n'}
                for name, content in contents.items():
                    path = self.path.parent / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                code, output, _, _ = self.init(runtime=runtime)
                self.assertEqual(code, 0)
                self.assertIn(f'loads project guidance from {loaded}; kept unchanged', output)
                self.assertNotIn('loads no project guidance', output)
                for name, content in contents.items():
                    self.assertEqual((self.path.parent / name).read_bytes(), content)
                policy = load_config(self.path).shared_instructions.read_text()
                self.assertIn(f'`{loaded}`', policy)
                self.assertNotIn('<project build command>', policy)

    def test_guidance_precedence_and_fallback(self):
        for name in ('AGENTS.md', '.claude/CLAUDE.md', 'CLAUDE.md'):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
        self.assertEqual(runtime_guidance(self.root, 'codex'), self.root / 'AGENTS.md')
        self.assertEqual(runtime_guidance(self.root, 'claude'), self.root / 'CLAUDE.md')
        (self.root / 'CLAUDE.md').unlink()
        self.assertEqual(runtime_guidance(self.root, 'claude'), self.root / '.claude/CLAUDE.md')
        (self.root / '.claude/CLAUDE.md').unlink()
        self.assertEqual(runtime_guidance(self.root, 'claude'), self.root / 'AGENTS.md')
        (self.root / 'AGENTS.md').unlink()
        self.assertIsNone(runtime_guidance(self.root, 'claude'))
        self.assertIsNone(runtime_guidance(self.root, 'codex'))

    def test_any_existing_starter_refuses_before_writing_guidance_or_labels(self):
        for name in ('starter.yaml', '.agents/implementer.md', '.agents/reviewer.md',
                     '.agents/issue-preparer.md', '.agents/integrator.md', '.agents/ub_agents.md'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'starter.yaml'
                existing = self.path.parent / name
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text('Keep me')
                before = {path.relative_to(self.path.parent): path.read_bytes()
                          for path in self.path.parent.rglob('*') if path.is_file()}
                code, _, error, _ = self.init(terminal=True, answer='yes')
                self.assertEqual(code, 1)
                self.assertIn('Starter files already exist', error)
                self.assertEqual(before, {path.relative_to(self.path.parent): path.read_bytes()
                                         for path in self.path.parent.rglob('*') if path.is_file()})
                self.assertEqual(self.github.reads, [])
                self.assertEqual(self.github.writes, [])

    def test_matching_commented_permissions_for_each_runtime(self):
        for runtime, expected, grant in (
            ('codex:model:high', ['--sandbox', 'danger-full-access'],
             'Grants full access without the Codex sandbox'),
            ('claude:model:high', ['--permission-mode', 'acceptEdits', '--permission-prompts', 'none',
                                   '--allowedTools', 'Bash(git *)', 'Bash(gh *)', 'Bash({report_command} report *)',
                                   'Bash({report_command} read *)',
                                   '--add-dir', '{scratch}'],
             'Grants unattended edits and git/gh/report commands')):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'ub-agents.yaml'
                self.assertEqual(self.init(runtime=runtime)[0], 0)
                config = load_config(self.path)
                self.assertTrue(all(not agent.runtime_args for agent in config.agents))
                text = self.path.read_text()
                self.assertEqual(text.count('# runtime-args:'), 4)
                comment = (f'# {grant}; applies to every runtime alternative: '
                           'https://github.com/uberblick-ai/ub-agents/blob/main/docs/'
                           'configuration.md#runtime-permissions')
                for agent_text in text.split('\n    runtime: ')[1:]:
                    self.assertIn(comment, agent_text)
                    if runtime.startswith('claude:'):
                        self.assertIn('Add the project\'s check commands to --allowedTools', agent_text)
                enabled = yaml.safe_load(text.replace('# runtime-args:', 'runtime-args:'))
                for agent in enabled['agents'].values():
                    self.assertEqual(agent['runtime-args'], expected)
                self.path.write_text(text.replace('# runtime-args:', 'runtime-args:'))
                self.assertTrue(all(list(agent.runtime_args) == expected for agent in load_config(self.path).agents))
