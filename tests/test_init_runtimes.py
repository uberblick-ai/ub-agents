import json
import subprocess
import unittest

import yaml

from tests import test_init_flow
from tests.test_init_flow import Script
from ub_agents.config import load_config
from ub_agents.doctor.doctor import diagnose
from ub_agents.errors import AgentError
from ub_agents.init.flow import RUNTIMES
from ub_agents.init.setup import DEFAULT_CODEX_RUNTIME, permission_args


class RuntimeFlowTests(unittest.TestCase):
    setUp = test_init_flow.FlowTests.setUp
    run_flow = test_init_flow.FlowTests.run_flow

    def check_doctor(self):
        result = diagnose(self.path, offline=True, runner=self.runner, which=lambda name: '/tools/' + name)
        self.assertTrue(result['ok'], result)

    def test_newest_listed_sol_model_fills_option_default_and_codex_reviewers(self):
        models = [{'slug': slug, 'visibility': visibility} for slug, visibility in (
            ('gpt-6.2-sol', 'list'), ('gpt-6.10-sol', 'list'), ('gpt-5-sol', 'list'),
            ('gpt-99-sol', 'hide'), ('gpt-99-sol', None), ('gpt-99-terra', 'list'),
            ('prefix-gpt-99-sol', 'list'), ('gpt-99-sol-preview', 'list'),
            ('gpt-99.1.2-sol', 'list'), ('gpt-99-sol\n', 'list'))]
        models += [None, 'gpt-99-sol', {}, {'slug': 99, 'visibility': 'list'}]
        for cli in ('codex', 'claude'):
            for extra, model in (([], 'gpt-6.10-sol'),
                                 ([{'slug': 'gpt-7-sol', 'visibility': 'list'}], 'gpt-7-sol')):
                with self.subTest(cli=cli, model=model):
                    self.setUp()
                    self.runner.responses[('codex', 'debug', 'models')] = json.dumps({'models': models + extra})
                    script = Script(Extras=[]) if cli == 'codex' else Script(Which_agent='claude', Extras=[])
                    code, text = self.run_flow(script)
                    self.assertEqual(code, 0)
                    option = next(o for o in self.script.options['Which agent runs the loop?'] if o.value == 'codex')
                    self.assertEqual(option.hint, f'{model} · high')
                    self.assertIn(f'{RUNTIMES[cli][0]} is the default;', text)
                    config = load_config(self.path)
                    for agent in config.agents:
                        reviewer = agent.name in {'issue-reviewer', 'reviewer'}
                        uses_codex = reviewer if cli == 'claude' else not reviewer
                        expected = f'codex:{model}:high' if uses_codex else RUNTIMES['claude'][1]
                        self.assertEqual(agent.runtimes[0].name, expected)
                    calls = [kwargs for call, kwargs in self.runner.calls if call == ('codex', 'debug', 'models')]
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]['timeout'], 5)
                    self.assertEqual(calls[0]['stdin'], subprocess.DEVNULL)
                    self.assertTrue(calls[0]['capture_output'])

    def test_model_lookup_failures_use_the_fallback_without_extra_output(self):
        failures = (
            OSError('model lookup failed'), AgentError('model lookup failed'),
            UnicodeError('model lookup failed'), subprocess.TimeoutExpired(['codex', 'debug', 'models'], 5),
            subprocess.CompletedProcess([], 1, 'model lookup failed', 'model lookup failed'),
            'model lookup failed', 'null', '[]', '{}', '{"models": {}}', '{"models": null}',
            '{"models": []}', '{"models": [null, {}, {"slug":"gpt-9-sol","visibility":"hide"},'
            '{"slug":"gpt-9-terra","visibility":"list"}]}')
        baseline = None
        for failure in failures:
            with self.subTest(failure=failure):
                self.setUp()
                self.runner.responses[('codex', 'debug', 'models')] = failure
                code, text = self.run_flow(Script(Extras=[]))
                self.assertEqual(code, 0)
                if baseline is None:
                    baseline = text
                self.assertEqual(text, baseline)
                self.assertNotIn('model lookup failed', text)
                codex = [agent for agent in load_config(self.path).agents if not agent.different_from]
                self.assertEqual({agent.runtimes[0].name for agent in codex}, {DEFAULT_CODEX_RUNTIME})

    def test_missing_or_logged_out_codex_uses_fallback_without_a_model_probe(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                self.setUp()
                if installed:
                    self.runner.responses[('codex', 'login', 'status')] = AgentError('not logged in')
                else:
                    self.installed.remove('codex')
                code, _ = self.run_flow(Script(Which_agent='codex', Extras=[]))
                self.assertEqual(code, 0)
                self.assertEqual({agent.runtimes[0].name for agent in load_config(self.path).agents},
                                 {DEFAULT_CODEX_RUNTIME})
                self.assertFalse([call for call, _ in self.runner.calls if call == ('codex', 'debug', 'models')])

    def test_detected_model_does_not_replace_an_explicit_runtime(self):
        self.runner.responses[('codex', 'debug', 'models')] = '{"models":[{"slug":"gpt-9-sol","visibility":"list"}]}'
        runtime = 'codex:chosen-model:low'
        code, text = self.run_flow(Script(Extras=[]), runtime=runtime)
        self.assertEqual(code, 0)
        self.assertIn('Codex · chosen-model · low', text)
        authors = [agent for agent in load_config(self.path).agents if not agent.different_from]
        self.assertEqual({agent.runtimes[0].name for agent in authors}, {runtime})

    def test_both_logged_in_split_reviewers_and_set_up_both_clis(self):
        for cli, other in (('codex', 'claude'), ('claude', 'codex')):
            for enabled in (False, True):
                with self.subTest(cli=cli, enabled=enabled):
                    self.setUp()
                    runtime = f'{cli}:custom:low'
                    code, text = self.run_flow(Script(Which_agent=cli, Let_agents=enabled, Keep=enabled,
                                                      Extras=['retrospectives']), runtime=runtime)
                    self.assertEqual(code, 0)
                    config = load_config(self.path)
                    for agent in config.agents:
                        source = {'reviewer': 'implementer', 'issue-reviewer': 'issue-preparer'}.get(agent.name)
                        self.assertEqual(agent.runtimes[0].name, RUNTIMES[other][1] if source else runtime)
                        self.assertEqual(agent.different_from, (source,) if source else ())
                        self.assertEqual(list(agent.runtime_args_for(agent.runtimes[0])),
                                         permission_args(other if source else cli, True) if enabled else [])
                    content = self.path.read_text()
                    if not enabled:
                        self.assertIn(f'# runtime-args:\n#   {cli}:', content)
                        block = content[content.index('# runtime-args:'):content.index("# An agent's runtime-args")]
                        uncommented = '\n'.join(line.removeprefix('# ') for line in block.splitlines())
                        args = yaml.safe_load(uncommented)['runtime-args']
                        self.assertEqual(args, {name: permission_args(name, True) for name in (cli, other)})
                        self.assertIsNone(config.runtime_updates)
                        self.assertIn(f'# runtime-updates:\n#   {cli}: auto\n#   {other}: auto\n#   gh: auto', content)
                    else:
                        self.assertEqual({name: config.runtime_updates.policies[name] for name in (cli, other, 'gh')},
                                         {cli: 'auto', other: 'auto', 'gh': 'auto'})
                    reviewer = (self.path.parent / '.agents/reviewer.md').read_text()
                    self.assertIn('with `different-runtime-from: implementer`', reviewer)
                    self.assertNotIn('without claiming independent authorship', reviewer)
                    self.assertIn(f'{RUNTIMES[other][0]} reviews issues and PRs', text)
                    self.assertIn(f'runtime-args for {cli} and {other}', text)
                    self.assertIn('Add your test command to --allowedTools', text)
                    self.assertEqual(len(self.script.asked), 7)
                    self.check_doctor()

    def test_one_logged_in_or_unattended_keeps_single_runtime_setup(self):
        for cli, other in (('codex', 'claude'), ('claude', 'codex')):
            for state in ('not installed', 'not logged in', 'unattended'):
                with self.subTest(cli=cli, state=state):
                    self.setUp()
                    if state == 'not installed':
                        self.installed.remove(other)
                    elif state == 'not logged in':
                        command = ('claude', 'auth', 'status') if other == 'claude' else ('codex', 'login', 'status')
                        self.runner.responses[command] = AgentError('not logged in')
                    self.installed.add('ub')
                    runtime = f'{cli}:custom:high'
                    extras = [] if state == 'unattended' else ['uberblick']
                    code, text = self.run_flow(Script(Which_agent=cli, Extras=extras, Set_up='later'), runtime=runtime,
                                               unattended=state == 'unattended')
                    self.assertEqual(code, 0)
                    config = load_config(self.path)
                    self.assertEqual({agent.runtimes[0].name for agent in config.agents}, {runtime})
                    self.assertTrue(all(not agent.different_from for agent in config.agents))
                    reviewer = (self.root / '.agents/reviewer.md').read_text()
                    self.assertIn('This starter uses the configured runtime without claiming independent authorship.', reviewer)
                    self.assertIn(f'{RUNTIMES[cli][0]} is the default for all agents', text)
                    content = self.path.read_text()
                    if state == 'unattended':
                        self.assertTrue(all(not agent.runtime_args for agent in config.agents))
                        self.assertIsNone(config.runtime_updates)
                    else:
                        self.assertEqual(set(yaml.safe_load(content)['runtime-args']), {cli})
                        self.assertEqual(config.runtime_updates.policies[other], 'off')
                    registrations = [call for call, _ in self.runner.calls
                                     if call[:3] == ('ub', 'mcp', 'install') and '--print' not in call]
                    self.assertEqual(registrations, [] if state == 'unattended' else [('ub', 'mcp', 'install', cli)])
                    self.check_doctor()
