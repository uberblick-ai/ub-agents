from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import shlex
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
from ub_agents.init_prompts import DIM, Cancelled, LinePrompter, Option, Transcript
from ub_agents.init_tailoring import BODY as TAILOR_BODY, TITLE as TAILOR_TITLE, command as tailoring_command


def flat(output):
    return ' '.join(re.sub(r'[│╮╯├─]', ' ', output).split())


class SetupResponses(dict):
    """Answer the setup PR's git and gh commands, whose file order follows the plan."""
    PR = 'https://github.com/org/project/pull/9'
    ISSUE = 'https://github.com/org/project/issues/10'

    def __missing__(self, command):
        if command[:2] == ('gh', 'pr'):
            return self.PR + '\n'
        if command[:3] == ('gh', 'issue', 'create'):
            return self.ISSUE + '\n'
        if command[0] == 'git' and command[3] in {'switch', 'add', 'commit', 'push'}:
            return ''
        raise KeyError(command)


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
        self.notes = getattr(self, 'notes', {})
        self.notes[message] = note
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
        self.runner.responses = SetupResponses(self.runner.responses)
        self.installed = {'git', 'gh', 'ps', 'claude', 'codex'}
        self.runner.responses[('ub', '--version')] = '0.4.3'
        for cli, name in (('claude', '.mcp.json'), ('codex', '.codex/config.toml')):
            self.runner.responses[('git', '-C', str(self.root), 'check-ignore', '-q', '--', name)] = subprocess.CompletedProcess(
                [], 1, '', '')
            snippet = ('{"mcpServers":{"uberblick":{"command":"ub","args":["mcp","serve"]}}}' if cli == 'claude' else
                       '[mcp_servers.uberblick]\ncommand = "ub"\nargs = ["mcp", "serve"]\n')
            self.runner.responses[('ub', 'mcp', 'install', cli, '--print')] = subprocess.CompletedProcess(
                [], 0, snippet, f'ub mcp install: --print runs nothing — paste this into {self.root / name}\n')

            def install(command, *, cwd, **kwargs):
                if command[-1] == 'claude':
                    path = cwd / '.mcp.json'
                    data = json.loads(path.read_text()) if path.exists() else {}
                    data.setdefault('mcpServers', {})['uberblick'] = {'command': 'ub', 'args': ['mcp', 'serve']}
                    path.write_text(json.dumps(data))
                else:
                    path = cwd / '.codex/config.toml'
                    path.parent.mkdir(exist_ok=True)
                    content = path.read_text() if path.exists() else ''
                    path.write_text(content + '\n[mcp_servers.uberblick]\ncommand = "ub"\nargs = ["mcp", "serve"]\n')
                return 'uberblick registered'
            self.runner.responses[('ub', 'mcp', 'install', cli)] = install

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
        self.assertEqual(self.script.asked, ['Which agent runs the loop?', 'Let agents work without asking?',
                                             'Keep codex and gh up to date?', "Who answers the agents' questions?",
                                             'Extras', 'Create 4 missing labels?',
                                             'Write setup?'])
        self.assertIn('Yes · codex and gh update once a day between runs', text)
        self.assertIn('runtime-updates:\n  codex: auto\n  gh: auto', self.path.read_text())
        self.assertIn('✓ Machine', text)
        self.assertIn('✓ Agents claude logged in · codex logged in', text)
        self.assertIn('Codex · gpt-6.1-sol · high', text)
        self.assertIn('Great, Codex is the default for all agents. Review later; '
                      'see https://agents.uberblick.ai/docs/configuration/runtimes', text)
        self.assertIn('Yes · review runtime-args in ub-agents.yaml before you launch', text)
        self.assertIn('Review the agent permissions (runtime-args) in ub-agents.yaml on the PR; see '
                      'https://agents.uberblick.ai/docs/configuration/permissions', text)
        self.assertIn('Want retrospectives later?', text)
        self.assertNotIn('Create the labels yourself', text)
        self.assertIn('Labels and boards are created on GitHub right away; the files go to a new PR.', text)
        self.assertIn('Setup PR opened https://github.com/org/project/pull/9 Back on main', text)
        self.assertIn('ub_agents.md and 5 role files', text)
        self.assertIn('Review and merge the setup PR: https://github.com/org/project/pull/9', text)
        self.assertIn('After the merge, pull main and run ub-agents launch', text)
        commands = [call for call, _ in self.runner.calls if call[:1] == ('gh',) or call[3:4] in {
            ('switch',), ('add',), ('commit',), ('push',)}]
        self.assertEqual([c[3] if c[0] == 'git' else 'gh' for c in commands],
                         ['switch', 'add', 'commit', 'push', 'gh', 'switch', 'gh'])
        self.assertEqual(set(commands[1][5:]), {'ub-agents.yaml', '.gitignore', *(
            f'.agents/{name}.md' for name in ('ub_agents', 'issue-preparer', 'issue-reviewer',
                                            'implementer', 'reviewer', 'integrator'))})
        self.assertEqual(commands[4][3:9], ('--repo', 'org/project', '--base', 'main', '--head', 'setup-ub-agents'))
        config = load_config(self.path)
        self.assertTrue(all(agent.runtime_args for agent in config.agents))
        self.assertEqual({w[1] for w in self.github.writes if w[0] == 'create-label'},
                     {'needs-preparation', 'needs-changes', 'ready-to-merge', 'outside-input'})

    def test_default_tailoring_issue_follows_the_setup_pr_and_appears_in_preview_and_next(self):
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn(f'+ {TAILOR_TITLE} · ready · file after the setup PR opens', text)
        self.assertIn(f'First loop issue: {SetupResponses.ISSUE} · {TAILOR_TITLE}', text.split('Next', 1)[1])
        self.assertNotIn('gh issue create', text)
        commands = [call for call, _ in self.runner.calls]
        issues = [call for call in commands if call[:3] == ('gh', 'issue', 'create')]
        self.assertEqual(issues, [tuple(tailoring_command('org/project'))])
        self.assertLess(next(i for i, call in enumerate(commands) if call[:3] == ('gh', 'pr', 'create')),
                        commands.index(issues[0]))
        for evidence in ('agent guidance', 'README', 'CI configuration', 'setup and release', 'code and tests',
                         'required CI check that gates merges', 'starter does not', 'one to three',
                         'Do not repeat the starter', 'every agent', 'general review advice',
                         'files you actually opened', 'evidence for each proposed rule in the PR body',
                         'merge to a maintainer'):
            self.assertIn(evidence, ' '.join(TAILOR_BODY.split()))

    def assert_manual_tailoring_command(self):
        output = self.output.getvalue()
        command = output[output.index('gh issue create '):output.index('\n│\n', output.index('gh issue create '))]
        self.assertEqual(shlex.split(command), tailoring_command('org/project'))
        self.assertFalse([call for call, _ in self.runner.calls if call[:3] == ('gh', 'issue', 'create')])

    def test_deselected_tailoring_extra_neither_files_nor_prints_a_command(self):
        code, text = self.run_flow(Script(Extras=[]))
        self.assertEqual(code, 0)
        self.assertNotIn(TAILOR_TITLE, text)
        self.assertNotIn('gh issue create', text)
        self.assertFalse([call for call, _ in self.runner.calls if call[:3] == ('gh', 'issue', 'create')])

    def test_missing_ready_label_prints_reason_and_command_without_attempting_to_file(self):
        self.github.label_names.remove('ready')
        code, text = self.run_flow(Script(Create=False))
        self.assertEqual(code, 0)
        self.assertIn('Setup PR opened', text)
        self.assertIn('The ready label is missing', text)
        self.assert_manual_tailoring_command()

    def test_filing_failure_keeps_setup_and_prints_reason_and_the_same_command(self):
        command = tuple(tailoring_command('org/project'))
        self.runner.responses[command] = subprocess.CompletedProcess([], 1, '', 'issues are disabled')
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('issues are disabled', text)
        self.assertIn('Review and merge the setup PR:', text)
        self.assertNotIn('First loop issue:', text)
        self.assertEqual(sum(call == command for call, _ in self.runner.calls), 1)
        output = self.output.getvalue()
        start = output.index('gh issue create ')
        self.assertEqual(shlex.split(output[start:output.index('\n│\n', start)]), list(command))

    def test_no_setup_pr_prints_command_without_filing(self):
        self.github.metadata['default_branch'] = None
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertNotIn('Setup PR opened', text)
        self.assert_manual_tailoring_command()

    def test_preselects_the_logged_in_cli_and_offers_only_claude_and_codex(self):
        self.runner.responses[('codex', 'login', 'status')] = AgentError('not logged in')
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        options = self.script.options['Which agent runs the loop?']
        self.assertEqual([o.value for o in options], ['claude', 'codex'])
        self.assertIn('Claude Code · claude-opus-5-5 · high', text)
        self.assertIn('"claude:claude-opus-5-5:high"', self.path.read_text())

    def test_shared_review_label_is_created_once_with_both_uses(self):
        self.github.label_names = []
        code, _ = self.run_flow()
        self.assertEqual(code, 0)
        label, = [write for write in self.github.writes
                  if write[:2] == ('create-label', 'needs-review')]
        self.assertEqual(label[2], 'ub-agents: starts issue-reviewer on an issue; starts reviewer on a PR')
        self.assertEqual({write[1]: write[3] for write in self.github.writes if write[0] == 'create-label'},
                         {'needs-preparation': 'c5def5', 'ready': '0e8a16', 'needs-review': 'fbca04',
                      'needs-human': 'b60205', 'needs-changes': 'd93f0b', 'ready-to-merge': '1d76db',
                      'outside-input': '1d76db'})

    def test_public_policy_and_optional_label_follow_visibility_and_approvals(self):
        for visibility, override, labelled, merge_rule in (
                ('public', None, True, True), ('private', None, False, False),
                ('internal', None, False, False), ('private', 'on', True, False),
                ('public', 'off', False, True)):
            with self.subTest(visibility=visibility, override=override):
                self.setUp()
                self.github.metadata['visibility'] = visibility
                if override:
                    (self.root / 'ub-agents.local.yaml').write_text(f'approvals: {override}\n')
                code, _ = self.run_flow()
                self.assertEqual(code, 0)
                created = {w[1] for w in self.github.writes if w[0] == 'create-label'}
                self.assertEqual('outside-input' in created, labelled)
                policy = (self.root / '.agents/ub_agents.md').read_text()
                self.assertEqual('- is labelled `outside-input` (built from outside text);' in policy,
                                 merge_rule)

    def test_declined_permissions_stay_commented(self):
        code, text = self.run_flow(Script(Let_agents=False))
        self.assertEqual(code, 0)
        self.assertTrue(all(not agent.runtime_args for agent in load_config(self.path).agents))
        self.assertIn('Grant agent permissions later: uncomment runtime-args', text)

    def test_priorities_write_queue_labels_and_offer_them_for_creation(self):
        code, text = self.run_flow(Script(Extras=['priority'], Priority='p1, p2'))
        self.assertEqual(code, 0)
        self.assertIn('Priority labels, highest first', self.script.asked)
        self.assertIn('Create 6 missing labels?', self.script.asked)
        self.assertEqual({w[1]: w[3] for w in self.github.writes if w[1] in {'p1', 'p2'}},
                         {'p1': 'e0524a', 'p2': 'c98a86'})
        self.assertIn('p1', self.path.read_text())

    def test_retrospectives_create_or_reuse_a_board_per_role(self):
        code, text = self.run_flow(Script(Extras=['retrospectives']))
        self.assertEqual(code, 0)
        created = [w[2]['title'] for w in self.github.writes if w[0] == 'create-board']
        self.assertEqual(sorted(created), ['Integrator retrospectives', 'Issue-preparer retrospectives',
                                           'Issue-reviewer retrospectives',
                                           'Reviewer retrospectives'])
        boards = {agent.name: agent.retrospectives for agent in load_config(self.path).agents}
        self.assertEqual(boards['implementer'], 203)
        self.assertTrue(boards['issue-reviewer'])
        self.assertEqual(len(set(boards.values())), 5)
        self.assertNotIn('Want retrospectives later?', text)

    def test_retrospectives_are_unavailable_when_discussions_are_off(self):
        self.github.discussion_metadata['hasDiscussionsEnabled'] = False
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        retrospectives = self.script.options['Extras'][1]
        self.assertEqual(retrospectives.disabled, 'Discussions are off in org/project')
        self.assertIn('Want retrospectives later? Turn on Discussions if needed, then run '
                      'ub-agents init --create-boards', text)

    def test_retrospectives_label_is_struck_only_when_discussions_are_off(self):
        metadata = self.github.discussion_metadata.copy()
        cases = [({}, ''),
                 ({'hasDiscussionsEnabled': False}, 'Discussions are off in org/project'),
                 ({'viewerPermission': 'READ'}, "this account can't create discussions"),
                 ({'discussionCategories': {'nodes': [], 'pageInfo': {'hasNextPage': False}}},
                  'the repository has no discussion categories'),
                 ({'hasDiscussionsEnabled': None}, "Discussions can't be checked")]
        console = Console()
        for change, disabled in cases:
            with self.subTest(disabled=disabled):
                self.github.discussion_metadata = metadata | change
                self.run_flow(Script(Write=False))
                options = self.script.options['Extras']
                self.assertEqual(options[1].disabled, disabled)
                rendered = Question({'kind': 'multi', 'message': 'Extras', 'options': options,
                                     'cursor': 1, 'checked': {3}}).render_question()
                label_start = rendered.plain.index('Retrospectives')
                label_end = label_start + len('Retrospectives')
                for offset in range(len(rendered)):
                    style = rendered.get_style_at_offset(console, offset)
                    in_label = label_start <= offset < label_end
                    self.assertEqual(bool(style.strike), in_label and change.get('hasDiscussionsEnabled') is False)
                    if in_label and disabled:
                        self.assertEqual(style.color, console.get_style(DIM).color)
                if disabled:
                    self.assertIn(f'Retrospectives ({disabled})', rendered.plain)

    def test_questions_go_to_a_maintaining_team_or_the_logged_in_account(self):
        self.github.team_handles = ['@org/core']
        for script, expected in ((Script(), '@org/core'), (Script(Who_answers='@operator'), '@operator'),
                                 (Script(Who_answers='', **{'Which @user': '@org/leads'}), '@org/leads')):
            with self.subTest(expected=expected):
                for path in [self.path, self.root / '.gitignore']:
                    path.unlink(missing_ok=True)
                for child in sorted(self.root.glob('.agents/*')):
                    child.unlink()
                code, text = self.run_flow(script)
                self.assertEqual(code, 0)
                options = self.script.options["Who answers the agents' questions?"]
                self.assertEqual([o.value for o in options], ['@org/core', '@operator', ''])
                policy = ' '.join((self.root / '.agents' / 'ub_agents.md').read_text().split())
                self.assertIn(f'`{expected}` answers scope and policy questions. An issue', policy)
                self.assertNotIn('@org/maintainers', policy)

    def test_claude_retrospectives_get_the_retrospective_permission(self):
        code, text = self.run_flow(Script(Which_agent='claude', Extras=['retrospectives']),
                                   runtime='codex:gpt-6.1-sol:high')
        self.assertEqual(code, 0)
        self.assertIn('"Bash({report_command} retrospective *)"', self.path.read_text())
        self.assertEqual({runtime.cli for agent in load_config(self.path).agents for runtime in agent.runtimes}, {'claude'})
        self.assertNotIn('--allowedTools so agents can post lessons', text)

    def test_policy_step_shows_approvals_from_repository_visibility(self):
        message = "Who answers the agents' questions?"
        for visibility, expected in (('public', 'on (visibility (public))'),
                                      ('private', 'off (visibility (private))'),
                                      ('internal', 'off (visibility (internal))'),
                                      (None, 'unresolved (repository visibility is unreadable)')):
            with self.subTest(visibility=visibility):
                self.github.metadata['visibility'] = visibility
                code, text = self.run_flow(Script(Write=False))
                self.assertEqual(code, 1)
                self.assertIn('Write setup?', self.script.asked)
                policy = f'Approvals: {expected}; see https://agents.uberblick.ai/docs/configuration/approvals'
                self.assertIn(policy, self.script.notes[message])
                self.assertIn(f"{message} @operator {policy}", text)
                self.assertNotIn('Approvals:', text.split('Which agent runs the loop?')[0])

    def test_policy_step_uses_explicit_local_approvals_without_visibility(self):
        self.github.metadata.pop('visibility')
        for value in ('on', 'off'):
            with self.subTest(value=value):
                (self.root / 'ub-agents.local.yaml').write_text(f'approvals: {value}\n')
                code, text = self.run_flow(Script(Write=False))
                self.assertEqual(code, 1)
                self.assertIn(f'Approvals: {value} (config)', text)
                self.assertNotIn('unresolved', text)

    def test_uberblick_needs_ub(self):
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertEqual(self.script.options['Extras'][2].disabled, 'needs ub: brew install uberblick-ai/tap/uberblick')

    def test_uberblick_creates_a_local_workspace_for_the_setup_pr(self):
        self.installed.add('ub')

        def create(command, **kwargs):
            (kwargs['cwd'] / '.uberblick.json').write_text('{"workspaceId": "w", "hubUrl": null}')
            return ''
        self.runner.responses[('ub', 'workspace', 'create', 'project')] = create
        code, text = self.run_flow(Script(Extras=['uberblick']))
        self.assertEqual(code, 0)
        self.assertIn('Set up an Uberblick workspace?', self.script.asked)
        self.assertIn('+ .uberblick.json local Uberblick workspace', text)
        self.assertIn('Uberblick workspace created', text)
        self.assertIn('Uberblick MCP · Codex · project scope · .codex/config.toml · registered', text)
        self.assertNotIn('Manual command:', text)
        added = next(call for call, _ in self.runner.calls if call[3:4] == ('add',))
        self.assertIn('.uberblick.json', added)
        self.assertIn('.codex/config.toml', added)

    def test_uberblick_workspace_choices(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'workspace', 'create', 'project')] = AgentError('hub unreachable')
        for answer, expected in (('hub', "Join your team's Uberblick workspace: ub auth login, then ub workspace use"),
                                 ('create', 'Bind an Uberblick workspace: ub workspace create <name>')):
            with self.subTest(answer=answer):
                for path in [self.path, self.root / '.gitignore']:
                    path.unlink(missing_ok=True)
                for child in sorted(self.root.glob('.agents/*')):
                    child.unlink()
                code, text = self.run_flow(Script(Extras=['uberblick'], Set_up_an_Uberblick=answer))
                self.assertEqual(code, 0)
                self.assertIn(expected, text)
                self.assertEqual('Uberblick workspace not created' in text, answer == 'create')

    def test_existing_uberblick_workspace_is_not_questioned(self):
        self.installed.add('ub')
        (self.root / '.uberblick.json').write_text('{"workspaceId": "w", "hubUrl": "wss://hub.example/ws"}')
        code, text = self.run_flow(Script(Extras=['uberblick']))
        self.assertEqual(code, 0)
        self.assertNotIn('Set up an Uberblick workspace?', self.script.asked)
        self.assertIn('Uberblick workspace Found .uberblick.json · wss://hub.example/ws', text)
        self.assertNotIn('Bind an Uberblick workspace', text)

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
        self.assertIn('Policy Approvals: unresolved (repository visibility is unreadable); see '
                      'https://agents.uberblick.ai/docs/configuration/approvals', text)
        self.assertNotIn('Create', ' '.join(self.script.asked))
        self.assertNotIn('Discussions', ' '.join(self.script.asked))

    def test_admin_account_gets_the_account_guide(self):
        self.github.roles['operator'] = 'admin'
        code, text = self.run_flow()
        self.assertIn('! Account operator is admin, good for setup', text)
        self.assertIn('! Account operator is admin, good for setup; launch the loop with a write-access agent account See https', text)
        self.assertIn('Launch the loop with a write-access agent account; see '
                      'https://agents.uberblick.ai/docs/best-practices/launcher-account', text)

    def test_unlisted_launcher_gets_local_config_remedy_without_account_change(self):
        (self.root / 'ub-agents.local.yaml').write_text('launchers: [another-account]\n')
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('! Account operator is not in local launchers; it will claim no work', text)
        self.assertIn('Review ub-agents.local.yaml: list this launcher account before launch', text)
        self.assertNotIn('write-access agent account', text)

    def test_other_branch_asks_to_switch_to_the_default_branch(self):
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'feature'
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertEqual(self.script.asked[0], 'Switch this checkout to main?')
        self.assertIn('Yes · now on main', text)
        self.assertIn('Agents always work in worktrees and branches; they never commit to main.', text)
        self.assertIn('Setup PR opened', text)
        self.assertTrue(self.path.exists())
        self.assertIn(('git', '-C', str(self.root), 'switch', 'main'), [call for call, _ in self.runner.calls])

    def test_setup_pr_closing_line_names_the_default_branch(self):
        self.github.metadata['default_branch'] = 'trunk'
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'trunk'
        self.runner.responses[('git', '-C', str(self.root), 'rev-list', '--left-right', '--count',
                               'HEAD...refs/remotes/origin/trunk')] = '0\t0'
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('Back on trunk', text)
        self.assertTrue(text.endswith('└ After the merge, pull trunk and run ub-agents launch'))

    def test_declined_or_failed_switch_stops_before_the_next_question_and_writes_nothing(self):
        head = ('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')
        switch = ('git', '-C', str(self.root), 'switch', 'main')
        self.installed.add('ub')
        for answer, outcome, expected in ((False, None, 'No · init needs main to open the setup PR'),
                                          (True, subprocess.CompletedProcess([], 1, '', 'error: local changes\nAborting'),
                                           'Not switched: error: local changes\n│  Aborting')):
            with self.subTest(answer=answer, outcome=outcome):
                self.runner.calls.clear()
                self.runner.responses[head] = 'feature'
                self.runner.responses[switch] = outcome
                code, text = self.run_flow(Script(Switch=answer, Extras=['priority', 'retrospectives', 'uberblick'],
                                                  Set_up='create'))
                self.assertEqual(code, 1)
                self.assertEqual(self.script.asked, ['Switch this checkout to main?'])
                self.assertIn('▲  Switch this checkout to main?\n│  ' + expected +
                              '\n│  Switch to main and run ub-agents init again\n', self.output.getvalue())
                self.assertNotIn('Setup written', text)
                self.assertEqual((list(self.root.iterdir()), self.github.writes), ([], []))
                commands = [call for call, _ in self.runner.calls]
                self.assertEqual(switch in commands, answer)
                self.assertFalse([call for call in commands if call[:1] == ('gh',) or
                                  call[:3] == ('ub', 'workspace', 'create') or
                                  call[:3] == ('ub', 'mcp', 'install') or
                                  call[3:4] in {('add',), ('commit',), ('push',)}])

    def test_failed_push_keeps_the_files_and_asks_for_a_manual_pr(self):
        self.runner.responses[('git', '-C', str(self.root), 'push', '--quiet', '--set-upstream', 'origin',
                               'setup-ub-agents')] = subprocess.CompletedProcess([], 1, '', 'remote: rejected')
        code, text = self.run_flow()
        self.assertEqual(code, 0)
        self.assertIn('Setup PR not opened remote: rejected Init created setup-ub-agents, committed the files; '
                      'open the PR yourself', text)
        self.assertIn('Open a PR with these files and merge it', text)
        self.assertTrue(text.endswith('└ Then run ub-agents launch'))
        self.assert_manual_tailoring_command()

    def test_unattended_files_stay_local_off_the_default_branch(self):
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'feature'
        code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        self.assertTrue(self.path.exists())
        self.assertIn('files stay local.', text)
        self.assertNotIn('Switch this checkout to main?', self.script.asked)
        self.assertNotIn('Setup PR', text)
        self.assertFalse([call for call, _ in self.runner.calls if call[:1] == ('gh',) or call[3:4] == ('push',)])

    def test_unknown_branch_or_default_branch_does_not_ask_to_switch(self):
        for branch, default in (('', 'main'), ('feature', None)):
            with self.subTest(branch=branch, default=default):
                self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = branch
                self.github.metadata['default_branch'] = default
                code, text = self.run_flow(Script(Write=False))
                self.assertEqual(code, 1)
                self.assertEqual(self.script.asked[0], 'Which agent runs the loop?')
                self.assertNotIn('Switch this checkout', text)
                self.assertIn('Write setup?', self.script.asked)

    def test_local_changes_stop_before_any_question(self):
        self.runner.responses[('git', '-C', str(self.root), '--no-optional-locks', 'status', '--porcelain=v1',
                               '--untracked-files=all')] = '?? notes.txt'
        code, text = self.run_flow()
        self.assertEqual(code, 1)
        self.assertEqual(self.script.asked, [])
        self.assertIn('main · local changes', text)
        self.assertIn('This checkout has local changes; commit or stash them, then run ub-agents init again', text)
        self.assertEqual((list(self.root.iterdir()), self.github.writes), ([], []))

    def test_default_branch_is_not_questioned(self):
        self.run_flow()
        self.assertNotIn('Switch this checkout to main?', self.script.asked)

    def test_unattended_uses_gh_user_and_keeps_permissions_off(self):
        code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.github.reads, self.github.writes), (['repos/org/project/teams', 'user'], []))
        self.assertIn('Checks skipped', text)
        self.assertNotIn('Approvals:', text)
        self.assertIn("Who answers the agents' questions? @operator · current gh user; "
                      'edit .agents/ub_agents.md to change it', text)
        self.assertNotIn("Who answers the agents' questions?", self.script.asked)
        policy = (self.root / '.agents' / 'ub_agents.md').read_text()
        self.assertIn('`@operator` answers scope and policy questions.', policy)
        self.assertNotIn('@org/maintainers', policy)
        self.assertTrue(all(not agent.runtime_args for agent in load_config(self.path).agents))
        self.assertIn('# runtime-updates:\n#   codex: auto\n#   gh: auto', self.path.read_text())
        self.assertIn('gh label create ready --repo org/project', text)
        self.assertIn('Extras Tailor loop policy', text)
        self.assert_manual_tailoring_command()

    def test_unattended_uses_first_maintaining_team_without_reading_actor(self):
        self.github.team_handles = ['@org/core', '@org/admins']
        self.github.auth_error = AgentError('user unreadable')
        code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.github.reads, self.github.writes), (['repos/org/project/teams'], []))
        self.assertIn("Who answers the agents' questions? @org/core · first maintaining team; "
                      'edit .agents/ub_agents.md to change it', text)
        self.assertNotIn("Who answers the agents' questions?", self.script.asked)
        policy = (self.root / '.agents' / 'ub_agents.md').read_text()
        self.assertIn('`@org/core` answers scope and policy questions.', policy)
        self.assertNotIn('@org/maintainers', policy)

    def test_unattended_falls_back_to_gh_user_when_team_read_fails(self):
        with patch.object(self.github, 'teams', side_effect=AgentError('teams unreadable')) as teams:
            code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        teams.assert_called_once_with()
        self.assertEqual(self.github.reads, ['user'])
        self.assertIn('@operator · current gh user; edit .agents/ub_agents.md to change it', text)
        self.assertIn('`@operator` answers scope and policy questions.',
                      (self.root / '.agents' / 'ub_agents.md').read_text())

    def test_unattended_unreadable_owner_keeps_placeholder_and_warns_before_launch(self):
        for error in (AgentError('not logged in'), FileNotFoundError('gh missing'),
                      subprocess.TimeoutExpired('gh', 120), UnicodeError('unreadable response')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                self.path = Path(directory) / 'ub-agents.yaml'
                with patch.object(self.github, 'teams', side_effect=error) as teams, \
                        patch.object(self.github, 'actor', side_effect=error) as actor:
                    code, text = self.run_flow(unattended=True)
                self.assertEqual(code, 0)
                teams.assert_called_once_with()
                actor.assert_called_once_with()
                self.assertIn('Setup written', text)
                self.assertIn('Replace @org/maintainers in .agents/ub_agents.md before launch', text)
                self.assertNotIn("Who answers the agents' questions?", self.script.asked)
                self.assertIn('`@org/maintainers` answers scope and policy questions;',
                              (self.path.parent / '.agents' / 'ub_agents.md').read_text())
                self.assertEqual(self.github.writes, [])

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
        self.assert_manual_tailoring_command()
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

    def test_disabled_extra_explains_why_before_retrying(self):
        options = [Option('priority', 'Priorities'),
                   Option('retrospectives', 'Retrospectives', disabled='Discussions are off in org/project', strike=True)]
        value, output = self.ask('multiselect', ['1,2', '1'], 'Extras', options)
        self.assertEqual(value, ['priority'])
        self.assertIn('  2. Retrospectives (Discussions are off in org/project)\n', output)
        self.assertNotIn('\x1b', output)
        self.assertIn('Retrospectives is unavailable: Discussions are off in org/project', output)

    def test_plain_multiselect_takes_defaults_and_can_deselect_them(self):
        options = [Option('priority', 'Priorities'), Option('tailoring', 'Tailor loop policy')]
        value, output = self.ask('multiselect', [''], 'Extras', options, ('tailoring',))
        self.assertEqual(value, ['tailoring'])
        self.assertIn('Tailor loop policy [selected]', output)
        self.assertEqual(self.ask('multiselect', ['0'], 'Extras', options, ('tailoring',))[0], [])


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
        options = [Option('a', 'A'), Option('b', 'B', disabled='off', strike=True), Option('c', 'C')]
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
                github = SetupGitHub()
                with patch('ub_agents.cli.sys.stdin.isatty', return_value=stdin_tty), \
                        patch('ub_agents.cli.GitHub', return_value=github), \
                        patch('ub_agents.init_prompts.InlinePrompter.run', side_effect=AssertionError('prompt')), \
                        patch('subprocess.run', side_effect=AssertionError('probe')), \
                        patch('builtins.input', side_effect=AssertionError('prompt')), \
                        patch.dict('os.environ', {'CI': ci}), redirect_stdout(io.StringIO()) as stdout:
                    with patch.object(stdout, 'isatty', return_value=stdout_tty):
                        self.assertEqual(main(['init', '--config', str(path), '--repository', 'org/project']), 0)
                self.assertTrue(path.exists())
                output = re.sub(r'\x1b\[[0-9;]*m', '', stdout.getvalue())
                self.assertIn('◇  Checks skipped\n│  Unattended setup\n│\n', output)
                self.assertNotIn('doctor', output)
                self.assertIn('@operator · current gh user', output)
                self.assertEqual(github.reads, ['repos/org/project/teams', 'user'])

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
                terminal.expect(b'up to date?')
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
