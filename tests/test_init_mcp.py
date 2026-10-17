import json
import subprocess
import unittest

from unittest.mock import patch

from ub_agents.init_mcp import preview
from ub_agents.init_prompts import Cancelled
from tests import test_init_flow
from tests.test_init_flow import Script


class MCPFlowTests(unittest.TestCase):
    setUp = test_init_flow.FlowTests.setUp
    run_flow = test_init_flow.FlowTests.run_flow

    def set_mcp_file(self, cli, content):
        path = self.root / ('.mcp.json' if cli == 'claude' else '.codex/config.toml')
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
        return path

    def ub_writes(self):
        return [call for call, _ in self.runner.calls if call[:3] == ('ub', 'mcp', 'install') and '--print' not in call]

    def test_preview_and_new_registration_follow_chosen_runtime(self):
        self.installed.add('ub')
        code, text = self.run_flow(Script(Which_agent='claude', Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        preview = 'Uberblick MCP · Claude Code · project scope · .mcp.json · add entry'
        self.assertIn(preview, text.split('Write setup?')[0])
        self.assertIn('Uberblick MCP · Claude Code · project scope · .mcp.json · registered', text)
        self.assertIn('Restart running agent sessions', text)
        self.assertEqual(self.ub_writes(), [('ub', 'mcp', 'install', 'claude')])
        data = json.loads((self.root / '.mcp.json').read_text())
        self.assertEqual(data, {'mcpServers': {'uberblick': {'command': 'ub', 'args': ['mcp', 'serve']}}})
        self.assertFalse((self.root / '.uberblick.json').exists())
        for verb in ('add', 'commit'):
            command = next(c for c, _ in self.runner.calls if c[3:4] == (verb,))
            self.assertIn('.mcp.json', command)

    def test_hub_choice_registers_without_a_binding(self):
        self.installed.add('ub')
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='hub'))
        self.assertEqual(code, 0)
        self.assertEqual(self.ub_writes(), [('ub', 'mcp', 'install', 'codex')])
        self.assertIn("Join your team's Uberblick workspace", text)
        self.assertIn('project scope · .codex/config.toml · registered', text)
        added = next(c for c, _ in self.runner.calls if c[3:4] == ('add',))
        self.assertIn('.codex/config.toml', added)
        self.assertNotIn('.uberblick.json', added)

    def test_matching_codex_entry_is_reused_byte_for_byte(self):
        self.installed.add('ub')
        content = '# keep this comment\n[mcp_servers.uberblick]\nargs = ["mcp", "serve"]\ncommand = "ub"\n'
        path = self.set_mcp_file('codex', content)
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(), content)
        self.assertIn('reuse matching entry', text.split('Write setup?')[0])
        self.assertIn('project scope · .codex/config.toml · reused', text)
        self.assertEqual(self.ub_writes(), [])
        self.assertFalse([c for c, _ in self.runner.calls if c[0] == 'ub'])
        self.assertNotIn('.codex/config.toml', next(c for c, _ in self.runner.calls if c[3:4] == ('add',)))

    def test_matching_claude_entry_with_empty_env_is_reused(self):
        self.installed.add('ub')
        content = '{"mcpServers":{"uberblick":{"type":"stdio","command":"ub","args":["mcp","serve"],"env":{}}}}'
        path = self.set_mcp_file('claude', content)
        code, text = self.run_flow(Script(Which_agent='claude', Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(), content)
        self.assertIn('· reused', text)
        self.assertEqual(self.ub_writes(), [])

    def test_unrelated_claude_entries_are_preserved(self):
        self.installed.add('ub')
        other = {'command': 'other', 'args': ['serve']}
        path = self.set_mcp_file('claude', json.dumps({'mcpServers': {'other': other}, 'extra': True}))
        code, text = self.run_flow(Script(Which_agent='claude', Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        data = json.loads(path.read_text())
        self.assertEqual(data['mcpServers']['other'], other)
        self.assertEqual(data['extra'], True)
        self.assertIn('· registered', text)

    def test_conflicting_or_pinned_entry_is_kept_without_revealing_values(self):
        self.installed.add('ub')
        content = ('[mcp_servers.uberblick]\ncommand = "ub"\nargs = ["mcp", "serve"]\n'
                   '[mcp_servers.uberblick.env]\nUB_WORKSPACE_ID = "private-workspace"\nTOKEN = "private-secret"\n')
        path = self.set_mcp_file('codex', content)
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(), content)
        self.assertIn('keep existing entry (conflicting uberblick entry; left unchanged)', text.split('Write setup?')[0])
        self.assertIn('· kept existing', text)
        self.assertIn('Manual command: ub mcp install codex', text)
        self.assertNotIn('private-', text)
        self.assertEqual(self.ub_writes(), [])
        self.assertNotIn('.codex/config.toml', next(c for c, _ in self.runner.calls if c[3:4] == ('add',)))

    def test_old_ub_skips_registration_but_opens_the_setup_pr(self):
        self.installed.add('ub')
        self.runner.responses[('ub', '--version')] = '0.4.2'
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn('not registered (requires ub 0.4.3 or newer with project-scope installation)', text)
        self.assertIn('Manual command: ub mcp install codex', text)
        self.assertIn('Setup PR opened', text)
        self.assertEqual(self.ub_writes(), [])

    def test_wrong_target_or_nonplain_snippet_is_not_registered(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'mcp', 'install', 'codex', '--print')] = subprocess.CompletedProcess(
            [], 0, '[mcp_servers.uberblick]\ncommand="ub"\nargs=["mcp","serve"]',
            'ub mcp install: --print runs nothing — paste this into /some/ancestor/.codex/config.toml\n')
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn("ub did not confirm this checkout's project config path", text)
        self.assertEqual(self.ub_writes(), [])

    def test_pinned_preview_is_not_installed(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'mcp', 'install', 'codex', '--print')] = subprocess.CompletedProcess(
            [], 0, '[mcp_servers.uberblick]\ncommand="ub"\nargs=["mcp","serve"]\n'
            '[mcp_servers.uberblick.env]\nUB_WORKSPACE_ID="private-workspace"\n',
            f'ub mcp install: --print runs nothing — paste this into {self.root / ".codex/config.toml"}\n')
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn('ub did not preview a plain ub mcp serve entry', text)
        self.assertNotIn('private-workspace', text)
        self.assertEqual(self.ub_writes(), [])

    def test_ignored_private_config_does_not_block_or_enter_the_pr(self):
        self.installed.add('ub')
        path = self.set_mcp_file('codex', 'private_token = "private-secret"\n')
        self.runner.responses[('git', '-C', str(self.root), 'check-ignore', '-q', '--', '.codex/config.toml')] = ''
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(), 'private_token = "private-secret"\n')
        self.assertIn('project config is ignored', text)
        self.assertNotIn('private-secret', text)
        self.assertIn('Setup PR opened', text)
        self.assertNotIn('.codex/config.toml', next(c for c, _ in self.runner.calls if c[3:4] == ('add',)))
        self.assertEqual(self.ub_writes(), [])

    def test_config_changed_after_preview_is_kept(self):
        with patch('subprocess.run', self.runner):
            registration = preview(self.root, 'codex')
            self.assertEqual(registration.action, 'add')
            path = self.set_mcp_file('codex', '# changed while answering\n')
            self.assertEqual(registration.apply(), [])
        self.assertEqual(path.read_text(), '# changed while answering\n')
        self.assertEqual(registration.action, 'kept existing')
        self.assertEqual(self.ub_writes(), [])

    def test_success_without_a_written_entry_is_not_reported_as_registered(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'mcp', 'install', 'codex')] = 'paste this snippet'
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn('not registered (client did not write a verified entry', text)
        self.assertIn('Setup PR opened', text)

    def test_registration_failure_preserves_core_setup_and_pr(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'mcp', 'install', 'codex')] = subprocess.CompletedProcess([], 1, '', 'private-secret')
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertTrue(self.path.exists())
        self.assertIn('Setup PR opened', text)
        self.assertIn('· not registered', text)
        self.assertIn('Manual command: ub mcp install codex', text)
        self.assertNotIn('private-secret', text)
        added = next(c for c, _ in self.runner.calls if c[3:4] == ('add',))
        self.assertNotIn('.codex/config.toml', added)

    def test_launch_error_is_optional(self):
        self.installed.add('ub')
        self.runner.responses[('ub', 'mcp', 'install', 'codex')] = OSError('private-secret')
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn('not registered (ub could not be run)', text)
        self.assertIn('Setup PR opened', text)
        self.assertNotIn('private-secret', text)

    def test_cancellation_leaves_mcp_unchanged_and_never_installs(self):
        self.installed.add('ub')
        path = self.set_mcp_file('codex', '# unrelated\nmodel = "example"\n')
        before = path.read_bytes()
        for answer in (False, Cancelled('Write setup?')):
            with self.subTest(answer=answer):
                code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later', Write=answer))
                self.assertEqual(code, 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(self.path.exists())
                self.assertEqual(self.ub_writes(), [])

    def test_declined_switch_leaves_mcp_unwritten(self):
        self.installed.add('ub')
        self.runner.responses[('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')] = 'feature'
        code, text = self.run_flow(Script(Switch=False, Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 1)
        self.assertFalse((self.root / '.codex/config.toml').exists())
        self.assertIn('Switch to main and run ub-agents init again', text)
        self.assertEqual(self.ub_writes(), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse([c for c, _ in self.runner.calls if c[3:4] in {('add',), ('commit',), ('push',)}])

    def test_unattended_skips_mcp_entirely(self):
        self.installed.add('ub')
        code, text = self.run_flow(unattended=True)
        self.assertEqual(code, 0)
        self.assertFalse([c for c, _ in self.runner.calls if c[0] == 'ub'])
        self.assertFalse((self.root / '.codex/config.toml').exists())

    def test_bad_or_symlinked_config_is_kept(self):
        self.installed.add('ub')
        path = self.root / '.codex'
        path.symlink_to(self.root, target_is_directory=True)
        code, text = self.run_flow(Script(Extras=['uberblick'], Set_up='later'))
        self.assertEqual(code, 0)
        self.assertIn('kept existing (config cannot be safely read; left unchanged)', text)
        self.assertFalse((self.root / 'config.toml').exists())
        self.assertEqual(self.ub_writes(), [])
