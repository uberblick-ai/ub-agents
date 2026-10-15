from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from tests.support import RecordingRunner, SetupGitHub
from tests.terminal import Terminal
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.doctor import Doctor
from ub_agents.errors import AgentError
from ub_agents.init_flow import Flow
from ub_agents.init_inline import Question
from ub_agents.init_prompts import Cancelled, LinePrompter, Option, Transcript


def flat(output):
    return ' '.join(re.sub(r'[│╮╯├─]', ' ', output).split())


class Script:
    """Answer questions by message prefix; anything unscripted takes its default."""
    def __init__(self, **answers):
        self.answers = {key.replace('_', ' '): value for key, value in answers.items()}
        self.asked = []

    def answer(self, message, default):
        self.asked.append(message)
        for prefix, value in self.answers.items():
            if message.casefold().startswith(prefix.casefold()):
                if isinstance(value, BaseException):
                    raise value
                return value
        return default

    def select(self, message, options, default=0, note=''):
        self.options = getattr(self, 'options', {})
        self.options[message] = options
        return self.answer(message, options[default].value)

    def multiselect(self, message, options, default=()):
        self.options = getattr(self, 'options', {})
        self.options[message] = options
        return self.answer(message, [o.value for o in options if o.value in default])

    def confirm(self, message, default=True, note=()):
        return self.answer(message, default)

    def text(self, message, default='', validate=None, note=()):
        value = self.answer(message, default)
        assert not (validate and validate(value)), validate(value)
        return value


class FlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / 'ub-agents.yaml'
        self.github = SetupGitHub()
        self.github.roles['operator'] = 'write'
        self.github.label_names = ['ready', 'needs-human', 'needs-review']
        self.runner = RecordingRunner(self.root)
        self.installed = {'git', 'gh', 'ps', 'claude', 'codex'}

    def run_flow(self, script=None, *, repository='org/project', runtime=None, unattended=False):
        self.script = script or Script()
        self.output = io.StringIO()
        flow = Flow(self.path, repository, runtime, lambda repo: self.github, self.script,
                    Transcript(Console(file=self.output, width=100, highlight=False, soft_wrap=True)),
                    unattended=unattended)
        flow.doctor = Doctor(runner=self.runner, which=lambda name: '/bin/' + name if name in self.installed else None,
                             github=self.github)
        with patch('subprocess.run', self.runner):
            code = flow.run()
        return code, flat(self.output.getvalue())

    def test_defaults_ask_only_the_decisions_and_write_setup(self):
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertEqual(self.script.asked, ['Which agent runs the loop?', 'Let agents work without asking?', 'Extras',
                                             'Create 3 missing labels?', 'Write setup?'])
        self.assertIn('✓ Machine', text)
        self.assertIn('✓ Agents claude logged in · codex logged in', text)
        self.assertIn('Codex · gpt-6.1-sol · high', text)
        self.assertIn('Great, Codex is the default for all agents. Review later; '
                      'see https://agents.uberblick.ai/docs/configuration/runtimes.html', text)
        self.assertIn('Yes · review runtime-args in ub-agents.yaml before you launch', text)
        self.assertIn('Review the agent permissions (runtime-args) in ub-agents.yaml; see '
                      'https://agents.uberblick.ai/docs/configuration/permissions.html', text)
        self.assertIn('Want retrospectives later?', text)
        self.assertNotIn('Create the labels yourself', text)
        config = load_config(self.path)
        self.assertTrue(all(agent.runtime_args for agent in config.agents))
        self.assertEqual({w[1] for w in self.github.writes if w[0] == 'create-label'},
                         {'needs-preparation', 'needs-changes', 'ready-to-merge'})

    def test_preselects_the_logged_in_cli_and_offers_only_claude_and_codex(self):
        self.runner.responses[('codex', 'login', 'status')] = AgentError('not logged in')
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        options = self.script.options['Which agent runs the loop?']
        self.assertEqual([o.value for o in options], ['claude', 'codex'])
        self.assertIn('Claude Code · claude-opus-5-5 · high', text)
        self.assertIn('"claude:claude-opus-5-5:high"', self.path.read_text())

    def test_declined_permissions_stay_commented(self):
        code, text = self.run_flow(Script(Let_agents=False))
        self.assertEqual(code, 0)
        self.assertTrue(all(not agent.runtime_args for agent in load_config(self.path).agents))
        self.assertIn('Grant agent permissions later: uncomment runtime-args', text)

    def test_priorities_write_queue_labels_and_offer_them_for_creation(self):
        code, text = self.run_flow(Script(Extras=['priority'], Priority='p1, p2'))
        self.assertEqual(code, 0)
        self.assertIn('Priority labels, highest first', self.script.asked)
        self.assertIn('Create 5 missing labels?', self.script.asked)
        self.assertIn('p1', {w[1] for w in self.github.writes if w[0] == 'create-label'})
        self.assertIn('p1', self.path.read_text())

    def test_retrospectives_create_or_reuse_a_board_per_role(self):
        code, text = self.run_flow(Script(Extras=['retrospectives']))
        self.assertEqual(code, 0)
        created = [w[2]['title'] for w in self.github.writes if w[0] == 'create-board']
        self.assertEqual(sorted(created), ['Integrator retrospectives', 'Issue-preparer retrospectives',
                                           'Reviewer retrospectives'])
        boards = {agent.name: agent.retrospectives for agent in load_config(self.path).agents}
        self.assertEqual(boards['implementer'], 203)
        self.assertEqual(len(set(boards.values())), 4)
        self.assertNotIn('Want retrospectives later?', text)

    def test_retrospectives_are_unavailable_when_discussions_are_off(self):
        self.github.discussion_metadata['hasDiscussionsEnabled'] = False
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        retrospectives = self.script.options['Extras'][1]
        self.assertEqual(retrospectives.disabled, 'Discussions are off in org/project')
        self.assertIn('Want retrospectives later? Turn on Discussions if needed, then follow '
                      'https://agents.uberblick.ai/docs/best-practices/retrospectives.html', text)

    def test_declined_labels_print_their_commands(self):
        code, text = self.run_flow(Script(Create=False))
        self.assertEqual(code, 0)
        self.assertFalse([w for w in self.github.writes if w[0] == 'create-label'])
        self.assertIn('Create the labels yourself gh label create needs-preparation --repo org/project', text)

    def test_cancel_or_declined_write_leaves_nothing(self):
        for script in (Script(Which=Cancelled('Which agent runs the loop?')), Script(Write=False),
                       Script(Extras=KeyboardInterrupt())):
            with self.subTest(asked=script.answers):
                code, text = self.run_flow(script)
                self.assertEqual(code, 1)
                self.assertIn('Setup cancelled. Nothing was written.', text)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual(self.github.writes, [])

    def test_existing_starter_files_refuse_before_any_question_or_read(self):
        self.path.write_text('keep')
        with self.assertRaisesRegex(AgentError, 'already exist'):
            self.run_flow()
        self.assertEqual(self.script.asked, [])
        self.assertEqual(self.github.reads, [])

    def test_unknown_repository_is_asked_for(self):
        self.runner.responses[('gh', 'repo', 'view', '--json', 'nameWithOwner')] = subprocess.CompletedProcess([], 1, '', '')
        code, text = self.run_flow(Script(Which_repository='org/project'), repository=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.script.asked[0], 'Which repository?')
        self.assertIn('repository: "org/project"', self.path.read_text())

    def test_failed_checks_are_reported_without_secrets_and_setup_continues(self):
        self.github.auth_error = AgentError('ghp-secret')
        self.installed -= {'claude', 'codex'}
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('Checks · fix these before launch; setup can continue', text)
        self.assertIn('✗ GitHub gh is not logged in Run gh auth login', text)
        self.assertIn('Neither claude nor codex is installed', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('Create', ' '.join(self.script.asked))
        self.assertNotIn('Discussions', ' '.join(self.script.asked))

    def test_admin_account_gets_the_account_guide(self):
        self.github.roles['operator'] = 'admin'
        code, text = self.run_flow()
        self.assertIn('! Account operator is admin on GitHub', text)
        self.assertIn('! Account operator is admin on GitHub; run agents as a write-only account See https', text)
        self.assertIn('Run agents as a separate write-access account; see '
                      'https://agents.uberblick.ai/docs/best-practices/launcher-account.html', text)

    def test_other_branch_asks_to_switch_to_the_default_branch(self):
        head = ('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')
        switch = ('git', '-C', str(self.root), 'switch', 'main')
        for answer, outcome, expected in ((True, '', 'Yes · now on main'),
                                          (True, subprocess.CompletedProcess([], 1, '', 'error: local changes\nAborting'),
                                           'Not switched: Aborting Switch to main before you launch'),
                                          (False, None, 'No · switch to main before you launch')):
            with self.subTest(answer=answer, outcome=outcome):
                for path in self.root.iterdir():
                    if path.is_file():
                        path.unlink()
                    else:
                        for child in sorted(path.rglob('*'), reverse=True):
                            child.unlink() if child.is_file() else child.rmdir()
                        path.rmdir()
                self.runner.calls.clear()
                self.runner.responses[head] = 'feature'
                self.runner.responses[switch] = outcome
                code, text = self.run_flow(Script(Switch=answer))
                self.assertEqual(code, 0)
                self.assertEqual(self.script.asked[0], 'Switch this checkout to main?')
                self.assertIn(expected, text)
                self.assertEqual('Agents always work in worktrees and branches; they never commit to main.' in text,
                                 expected.startswith('Yes'))
                self.assertEqual(switch in [call for call, _ in self.runner.calls], answer)

    def test_default_branch_is_not_questioned(self):
        self.run_flow()
        self.assertNotIn('Switch this checkout to main?', self.script.asked)

    def test_unattended_reads_nothing_and_keeps_permissions_off(self):
        code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.github.reads, self.github.writes), ([], []))
        self.assertIn('Checks skipped', text)
        self.assertTrue(all(not agent.runtime_args for agent in load_config(self.path).agents))
        self.assertIn('gh label create ready --repo org/project', text)

    def test_partial_label_failure_reports_what_is_left(self):
        create = self.github.create_label

        def fail_second(name, description, color):
            if name == 'needs-changes':
                raise AgentError('label failed')
            create(name, description, color)
        self.github.create_label = fail_second
        code, text = self.run_flow()
        self.assertEqual(code, 1)
        self.assertIn('Setup partly written', text)
        self.assertIn('gh label create needs-changes', text)
        self.assertTrue(self.path.exists())


class LinePrompterTests(unittest.TestCase):
    def ask(self, method, answers, *args):
        output = io.StringIO()
        with patch('builtins.input', side_effect=answers), redirect_stdout(output):
            return getattr(LinePrompter(), method)(*args), output.getvalue()

    def test_numbered_answers_defaults_and_disabled_options(self):
        options = [Option('a', 'A'), Option('b', 'B', disabled='off')]
        self.assertEqual(self.ask('select', ['9', '2'], 'Pick', options)[0], 'b')
        self.assertEqual(self.ask('select', [''], 'Pick', options, 1)[0], 'b')
        self.assertEqual(self.ask('multiselect', ['2', '1'], 'Pick', options)[0], ['a'])
        self.assertEqual(self.ask('confirm', ['maybe', 'n'], 'Sure?')[0], False)
        value, output = self.ask('text', ['x', 'ok'], 'Name', '', lambda v: None if v == 'ok' else 'Enter ok')
        self.assertEqual(value, 'ok')
        self.assertIn('Enter ok', output)

    def test_eof_and_interrupt_cancel(self):
        for answer in (EOFError(), KeyboardInterrupt()):
            with self.assertRaises(Cancelled):
                self.ask('confirm', [answer], 'Sure?')


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, question, *keys):
        app = Question(question)
        async with app.run_test() as pilot:
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
        return app.return_value, app

    async def test_select_moves_with_arrows_and_vim_keys(self):
        options = [Option('a', 'A'), Option('b', 'B'), Option('c', 'C')]
        question = {'kind': 'select', 'message': 'Pick', 'options': options, 'cursor': 0}
        self.assertEqual((await self.answer(question, 'down', 'down', 'enter'))[0], 'c')
        self.assertEqual((await self.answer(question, 'up', 'enter'))[0], 'c')
        self.assertEqual((await self.answer(question, 'right', 'l', 'h', 'enter'))[0], 'b')

    async def test_multiselect_toggles_but_skips_disabled(self):
        options = [Option('a', 'A'), Option('b', 'B', disabled='off'), Option('c', 'C')]
        question = {'kind': 'multi', 'message': 'Pick', 'options': options, 'cursor': 0}
        self.assertEqual((await self.answer(question, 'space', 'down', 'space', 'down', 'space', 'enter'))[0],
                         ['a', 'c'])

    async def test_confirm_left_right_and_escape_cancels(self):
        question = {'kind': 'confirm', 'message': 'Sure?', 'options': [Option(True, 'Yes'), Option(False, 'No')],
                    'cursor': 0}
        self.assertIs((await self.answer(question, 'right', 'enter'))[0], False)
        self.assertIsNone((await self.answer(question, 'escape'))[0])

    async def test_text_validates_before_returning(self):
        question = {'kind': 'text', 'message': 'Name', 'default': '',
                    'validate': lambda value: None if value == 'ok' else 'Enter ok'}
        value, app = await self.answer(question, 'x', 'enter')
        self.assertIsNone(value)
        self.assertEqual(app.error, 'Enter ok')
        self.assertEqual((await self.answer(question, 'o', 'k', 'enter'))[0], 'ok')


class TerminalTests(unittest.TestCase):
    def test_ci_and_non_tty_runs_take_defaults_without_prompting(self):
        for stdin_tty, stdout_tty, ci in ((False, True, ''), (True, False, ''), (True, True, '1')):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty, ci=ci), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'workflow.yaml'
                with patch('ub_agents.cli.sys.stdin.isatty', return_value=stdin_tty), \
                        patch('ub_agents.init_prompts.InlinePrompter.run', side_effect=AssertionError('prompt')), \
                        patch('subprocess.run', side_effect=AssertionError('probe')), \
                        patch('builtins.input', side_effect=AssertionError('prompt')), \
                        patch.dict('os.environ', {'CI': ci}), redirect_stdout(io.StringIO()) as stdout:
                    with patch.object(stdout, 'isatty', return_value=stdout_tty):
                        self.assertEqual(main(['init', '--config', str(path), '--repository', 'org/project']), 0)
                self.assertTrue(path.exists())
                self.assertIn('Checks skipped', stdout.getvalue())

    def test_inline_flow_in_a_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ub-agents.yaml'
            script = '''
import sys
from unittest.mock import patch
from tests.support import SetupGitHub
from ub_agents.cli import main
from ub_agents.init_flow import Flow
github = SetupGitHub()
with patch('ub_agents.cli.GitHub', lambda repo: github), \\
        patch.object(Flow, 'check', lambda self: setattr(self, 'github', github)), \\
        patch.object(Flow, 'choose_labels', lambda self, plan: []):
    sys.exit(main(['--config', sys.argv[1], 'init', '--repository', 'org/project']))
'''
            with Terminal(script, path, env={'CI': None}) as terminal:
                terminal.expect(b'Which agent runs the loop?')
                terminal.send(b'\r')
                terminal.expect(b'Let agents work without asking?')
                terminal.send(b'\x1b[B')
                terminal.send(b'\r')
                terminal.expect(b'Extras')
                terminal.send(b'\r')
                terminal.expect(b'Write setup?')
                terminal.send(b'\r')
                terminal.expect(b'ub-agents launch')
                terminal.wait_exit(sequences=False)
                # Inline questions never take over the screen.
                self.assertNotIn(b'\x1b[?1049h', terminal.transcript)
            self.assertTrue(all(not agent.runtime_args for agent in load_config(path).agents))
