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

    def init(self, *, terminal=False, stdout_terminal=None, answer='', permissions_answer='no',
             runtime=None, infer=False, ci=''):
        def respond(question):
            response = permissions_answer if question.startswith('Enable starter permissions') else answer
            if isinstance(response, Exception):
                raise response
            return response

        with patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('subprocess.run', self.runner), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=terminal), \
                patch.dict('os.environ', {'CI': ci}), \
                patch('builtins.input', side_effect=respond) as prompt, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            with patch.object(stdout, 'isatty', return_value=terminal if stdout_terminal is None else stdout_terminal):
                code = main(['--config', str(self.path), 'init']
                            + (['--runtime', runtime] if runtime else [])
                            + ([] if infer else ['--repository', 'org/project']))
        return code, stdout.getvalue(), stderr.getvalue(), prompt

    def commands(self, output):
        return [shlex.split(line) for line in output.splitlines() if line.startswith('gh label create ')]

    def test_noninteractive_starter_guidance_all_commands_and_only_inference_read(self):
        code, output, error, prompt = self.init(infer=True)
        self.assertEqual((code, error), (0, ''))
        config = load_config(self.path)
        self.assertIsNone(config.checkout_setup)
        self.assertIn('# checkout-setup:', self.path.read_text())
        self.assertIn('#   when-changed: [pnpm-lock.yaml, mise.toml]', self.path.read_text())
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

    def test_default_and_claude_starters_pass_check_with_and_without_permissions(self):
        for runtime in (None, 'claude:opus:high'):
            for enabled in (False, True):
                with self.subTest(runtime=runtime, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                    self.path = Path(directory) / 'ub-agents.yaml'
                    code, output, _, _ = self.init(runtime=runtime, terminal=enabled, permissions_answer='yes')
                    self.assertEqual(code, 0)
                    cli = runtime.split(':', 1)[0] if runtime else 'codex'
                    self.assertIn(f'{cli} loads no project guidance', output)
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(main(['--config', str(self.path), 'check']), 0)
                    for name in ('AGENTS.md', 'CLAUDE.md', '.claude/CLAUDE.md'):
                        self.assertFalse((self.path.parent / name).exists())

    def test_one_permission_question_covers_all_agents_and_explains_each_runtime(self):
        for runtime, grant, expected in (
            (None, 'Codex starter permissions grant full access without the sandbox',
             ['--sandbox', 'danger-full-access']),
            ('claude:opus:high', 'Claude starter permissions grant unattended edits plus git, gh and report commands',
             ['--permission-mode', 'acceptEdits', '--permission-prompts', 'none', '--allowedTools',
              'Bash(git *)', 'Bash(gh *)', 'Bash({report_command} report *)', 'Bash({report_command} read *)',
              '--add-dir', '{scratch}'])):
            for answer in ('yes', ' Y ', 'no', '', 'maybe', EOFError()):
                with self.subTest(runtime=runtime, answer=answer), tempfile.TemporaryDirectory() as directory:
                    self.path = Path(directory) / 'ub-agents.yaml'
                    code, output, error, prompt = self.init(terminal=True, runtime=runtime, permissions_answer=answer)
                    self.assertEqual((code, error), (0, ''))
                    permission_questions = [call.args[0] for call in prompt.call_args_list
                                            if call.args[0].startswith('Enable starter permissions')]
                    self.assertEqual(permission_questions, ['Enable starter permissions for all four agents? [y/N] '])
                    self.assertIn(grant, output)
                    enabled = answer in ('yes', ' Y ')
                    for agent in load_config(self.path).agents:
                        self.assertEqual(list(agent.runtime_args_for(agent.runtimes[0])), expected if enabled else [])
                    self.assertEqual(self.path.read_text().count('# runtime-args:'), 0 if enabled else 1)
                    generated = yaml.safe_load(self.path.read_text())
                    if enabled:
                        cli = runtime.split(':', 1)[0] if runtime else 'codex'
                        self.assertEqual(generated['runtime-args'], {cli: expected})
                    self.assertTrue(all('runtime-args' not in agent for agent in generated['agents'].values()))
                    self.assertEqual('uncomment or customize' in output, not enabled)
                    if runtime:
                        self.assertIn("the project's check commands must still be added to --allowedTools", output)
                        self.assertIn("add check commands to --allowedTools", output)
                    self.assertEqual(self.github.writes, [])

    def test_output_names_relative_files_and_ordered_setup_steps(self):
        for runtime in ('codex:model:high', 'claude:opus:high'):
            for enabled in (False, True):
                with self.subTest(runtime=runtime, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                    self.path = Path(directory) / 'starter.yaml'
                    code, output, error, _ = self.init(runtime=runtime, terminal=enabled,
                                                     permissions_answer='yes')
                    self.assertEqual((code, error), (0, ''))
                    lines = output.splitlines()
                    start = lines.index('config: starter.yaml')
                    self.assertEqual(lines[start:start + 3], [
                        'config: starter.yaml',
                        'policy: .agents/ub_agents.md (checks, merge policy, decision-makers, review priorities)',
                        'roles: .agents/ (issue-preparer, implementer, reviewer, integrator)',
                    ])
                    steps = lines[start + 3]
                    self.assertTrue(steps.startswith('next: fill in the checks and policy in .agents/ub_agents.md;'))
                    self.assertEqual('grant agent permissions' in steps, not enabled)
                    self.assertEqual('--allowedTools' in steps, runtime.startswith('claude:'))
                    self.assertTrue(steps.endswith('run ub-agents check; commit and push the starter files; '
                                                  'run ub-agents doctor.'))
                    self.assertNotIn(str(self.path.parent), output)

    def test_starter_policy_is_neutral_and_has_no_repeated_sentences(self):
        for runtime in ('codex:model:high', 'claude:opus:high'):
            for guidance in (None, 'AGENTS.md', '.claude/CLAUDE.md'):
                with self.subTest(runtime=runtime, guidance=guidance), tempfile.TemporaryDirectory() as directory:
                    self.path = Path(directory) / 'ub-agents.yaml'
                    if guidance:
                        path = self.path.parent / guidance
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('# Checks\nRun project tests.\n')
                    self.assertEqual(self.init(runtime=runtime)[0], 0)
                    policy = load_config(self.path).shared_instructions.read_text()
                    customized = ' '.join(policy.replace('@org/maintainers', '@acme/core').split())
                    self.assertIn('`@acme/core` answers scope and policy questions;', customized)
                    self.assertIn('Leave changes to workflow, permissions and release policy to `@acme/core`;',
                                  customized)
                    self.assertNotIn('maintainer team', policy)
                    self.assertEqual(policy.split('## Review focus\n\n')[1].strip(),
                                     "Replace this section with the project's review priorities and required evidence.")
                    self.assertEqual(policy.count('before handoff'), 1)
                    prose = ' '.join(line for line in policy.splitlines() if not line.startswith('#'))
                    sentences = re.split(r'(?<=[.!?])\s+', prose.strip())
                    self.assertEqual(len(sentences), len(set(sentences)))

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
        self.assertIn('record the wait as a blocked-by relationship on the assigned issue',
                      roles['issue-preparer'])
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

    def test_base_merge_procedure_in_starter_and_repository_roles(self):
        self.assertEqual(self.init()[0], 0)
        root = Path(__file__).resolve().parents[1]
        for directory in (self.path.parent / '.agents', root / '.agents'):
            with self.subTest(directory=directory):
                implementer = ' '.join((directory / 'implementer.md').read_text().split())
                for expected in (
                    "Before reporting the handoff outcome, fetch the PR's base branch",
                    'git merge-tree --write-tree <base> HEAD',
                    'If it reports conflicts, merge the base into the PR branch (no rebase or force push)',
                    'resolve them and, after the checks below pass, push before handing off',
                    'A head that merges cleanly needs no merge, even if it is behind the base',
                ):
                    self.assertIn(expected, implementer)
                rerun = ('After merging the base branch into the PR branch, rerun the checks that cover what '
                         'the PR adds or changes, not only the files that conflicted.')
                self.assertEqual(implementer.count(rerun), 1)

                integrator = ' '.join((directory / 'integrator.md').read_text().split())
                for expected in (
                    "Before changing the PR branch, check `gh pr view N --json headRefOid` against the "
                    "assignment context's `candidate_sha`; report blocked if they differ",
                    'git merge-tree --write-tree <base> HEAD',
                    'If the candidate is behind the base but merges cleanly, do not send it back',
                    'merge the base into the PR branch with a merge commit (no rebase or force push)',
                    'git push origin HEAD:refs/heads/BRANCH',
                    'Your own clean base merge needs no new review',
                    'Run the declared final checks at the resulting head',
                    '`candidate_sha` or the SHA of the base merge you pushed yourself; '
                    'any other change reports blocked',
                    'merge exactly the verified head',
                    'Only if the candidate conflicts with the base branch, a declared check fails for a '
                    "cause that code or tests in the repository can fix, or the project's shared guidance "
                    'asks PRs to carry changelog entries and the entry for a user-facing change is missing '
                    'or inaccurate, send it back to the implementer:',
                ):
                    self.assertIn(expected, integrator)

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
        self.assertIn('added when implementer reports handed-off', output)
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

    def test_piped_input_redirected_output_and_ci_leave_permissions_commented_without_prompting(self):
        for terminal, stdout_terminal, ci in ((False, True, ''), (True, False, ''), (True, True, 'true')):
            with self.subTest(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci):
                if self.path.exists():
                    self.path.unlink()
                    for path in (self.path.parent / '.agents').glob('*.md'):
                        path.unlink()
                code, output, _, prompt = self.init(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci,
                                                  permissions_answer='yes')
                self.assertEqual(code, 0)
                self.assertEqual(len(self.commands(output)), 6)
                self.assertEqual(self.github.reads, [])
                self.assertEqual(self.github.writes, [])
                prompt.assert_not_called()
                self.assertTrue(all(not agent.runtime_args for agent in load_config(self.path).agents))
                self.assertEqual(self.path.read_text().count('# runtime-args:'), 1)
                self.assertIn('grant agent permissions: uncomment or customize', output)

    def test_unreadable_labels_prints_all_commands_without_label_prompt(self):
        self.github.label_error = AgentError('private error')
        code, output, _, prompt = self.init(terminal=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertIn('Could not read GitHub labels', output)
        self.assertEqual(len(self.commands(output)), 6)
        self.assertNotIn('private error', output)
        self.assertEqual(self.github.writes, [])
        prompt.assert_called_once_with('Enable starter permissions for all four agents? [y/N] ')

    def test_all_labels_present_does_not_prompt_for_labels_or_write(self):
        self.github.label_names = ['READY', 'needs-preparation', 'needs-human', 'needs-changes',
                                   'needs-review', 'ready-to-merge']
        code, output, _, prompt = self.init(terminal=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertIn('All configured workflow labels exist', output)
        self.assertEqual(self.commands(output), [])
        self.assertEqual(self.github.writes, [])
        prompt.assert_called_once_with('Enable starter permissions for all four agents? [y/N] ')

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

    def test_guidance_detection_matrix_points_to_existing_checks_and_warns_about_unloaded_files(self):
        cases = (
            ((), None, None),
            (('AGENTS.md',), 'AGENTS.md', 'AGENTS.md'),
            (('CLAUDE.md',), 'CLAUDE.md', 'CLAUDE.md'),
            (('.claude/CLAUDE.md',), '.claude/CLAUDE.md', '.claude/CLAUDE.md'),
            (('AGENTS.md', 'CLAUDE.md'), 'AGENTS.md', 'CLAUDE.md'),
            (('AGENTS.md', '.claude/CLAUDE.md'), 'AGENTS.md', '.claude/CLAUDE.md'),
            (('CLAUDE.md', '.claude/CLAUDE.md'), 'CLAUDE.md', 'CLAUDE.md'),
            (('AGENTS.md', 'CLAUDE.md', '.claude/CLAUDE.md'), 'AGENTS.md', 'CLAUDE.md'),
        )
        for names, codex_checks, claude_checks in cases:
            for cli, checks in (('codex', codex_checks), ('claude', claude_checks)):
                with self.subTest(names=names, cli=cli), tempfile.TemporaryDirectory() as directory:
                    self.path = Path(directory) / 'ub-agents.yaml'
                    for name in names:
                        path = self.path.parent / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f'# Checks in {name}\n')
                    code, output, error, _ = self.init(runtime=f'{cli}:model:high')
                    self.assertEqual((code, error), (0, ''))
                    policy = load_config(self.path).shared_instructions.read_text()
                    if checks:
                        self.assertIn(f'Project checks are documented in `{checks}`.', policy)
                        self.assertNotIn('<project build command>', policy)
                    else:
                        self.assertIn('<project build command>', policy)
                    loaded = checks
                    if cli == 'codex' and 'AGENTS.md' not in names:
                        loaded = None
                    self.assertEqual(runtime_guidance(self.path.parent, cli),
                                     self.path.parent / loaded if loaded else None)
                    if loaded:
                        self.assertIn(f'{cli} loads project guidance from {loaded}; kept unchanged.', output)
                        self.assertNotIn('Warning:', output)
                    elif checks:
                        self.assertIn(f'Warning: {cli} loads no project guidance; checks are documented in {checks}. '
                                      f'Add a one-line AGENTS.md: "Read {checks}".', output)
                    else:
                        choices = 'AGENTS.md' if cli == 'codex' else 'CLAUDE.md, .claude/CLAUDE.md or AGENTS.md'
                        self.assertIn(f'Warning: {cli} loads no project guidance; add {choices} '
                                      'so agents know how to build and test.', output)
                    for name in names:
                        self.assertEqual((self.path.parent / name).read_text(), f'# Checks in {name}\n')

    def test_label_meanings_match_lists_commands_and_created_descriptions(self):
        self.github.label_names = []
        meanings = {
            'ready': 'starts implementer on an issue or PR; added when issue-preparer reports prepared',
            'needs-review': 'starts reviewer on a PR; added when implementer reports handed-off',
            'needs-human': 'parks an issue or PR until a person decides; added when issue-preparer reports needs-human',
            'needs-changes': 'starts implementer on an issue or PR; added when reviewer reports changes-requested; '
                             'added when integrator reports changes-requested',
        }
        descriptions = {
            'ready': 'ub-agents: starts implementer on an issue or PR; added when issue-preparer reports prepared',
            'needs-review': 'ub-agents: starts reviewer on a PR; added when implementer reports handed-off',
            'needs-human': 'ub-agents: parks an issue or PR until a person decides',
            'needs-changes': 'ub-agents: starts implementer on an issue or PR; added when reviewer reports changes-requested',
        }
        for terminal, answer in ((False, ''), (True, 'no'), (True, 'yes')):
            with self.subTest(terminal=terminal, answer=answer), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'ub-agents.yaml'
                self.github.label_names = []
                self.github.writes.clear()
                code, output, error, _ = self.init(terminal=terminal, answer=answer)
                self.assertEqual((code, error), (0, ''))
                commands = {command[3]: command for command in self.commands(output)}
                for name, meaning in meanings.items():
                    if terminal:
                        self.assertIn(f'  {name}: {meaning}', output)
                    description = descriptions[name]
                    self.assertLessEqual(len(description), 100)
                    if answer == 'yes':
                        self.assertIn(('create-label', name, description,
                                       'd876e3' if name == 'needs-human' else '1d76db'), self.github.writes)
                    else:
                        command = commands[name]
                        self.assertEqual(command[command.index('--description') + 1], description)

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
                code, _, error, prompt = self.init(terminal=True, answer='yes')
                self.assertEqual(code, 1)
                self.assertIn('Starter files already exist', error)
                self.assertEqual(before, {path.relative_to(self.path.parent): path.read_bytes()
                                         for path in self.path.parent.rglob('*') if path.is_file()})
                self.assertEqual(self.github.reads, [])
                self.assertEqual(self.github.writes, [])
                prompt.assert_not_called()

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
                self.assertEqual(text.count('# runtime-args:'), 1)
                comment = (f'# {grant}: '
                           'https://github.com/uberblick-ai/ub-agents/blob/main/docs/'
                           'configuration.md#runtime-permissions')
                self.assertIn(comment, text)
                self.assertIn("An agent's runtime-args list or mapping replaces these defaults entirely", text)
                if runtime.startswith('claude:'):
                    self.assertIn('Add the project\'s check commands to --allowedTools', text)
                    self.assertIn('"Bash(<project check command>)"', text)
                cli = runtime.split(':', 1)[0]
                uncommented = text.replace('# runtime-args:', 'runtime-args:').replace(f'#   {cli}:', f'  {cli}:')
                enabled = yaml.safe_load(uncommented)
                self.assertEqual(enabled['runtime-args'], {cli: expected})
                self.assertTrue(all('runtime-args' not in agent for agent in enabled['agents'].values()))
                self.path.write_text(uncommented)
                for agent in load_config(self.path).agents:
                    self.assertEqual(list(agent.runtime_args_for(agent.runtimes[0])), expected)
