from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Input

from tests.support import DoctorGitHub, RecordingRunner, SetupGitHub
from tests.terminal import Terminal
from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.doctor import Doctor
from ub_agents.errors import AgentError
from ub_agents.init_probes import Probes, findings
from ub_agents.init_setup import Choices, apply_plan, make_plan, permission_args
from ub_agents.init_ui import SetupApp, line_wizard
from ub_agents.init_wizard import Wizard
from ub_agents.labels import configured_labels
from ub_agents.init_boards import Boards


class WizardFixture:
    def setup(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / 'setup' / 'workflow.yaml'
        self.github = SetupGitHub()
        self.github.roles['operator'] = 'admin'
        self.plan = make_plan(self.path, 'org/project', Choices('codex:model:high'))
        self.runner = RecordingRunner(self.path.parent)
        doctor = Doctor(runner=self.runner, which=lambda name: '/bin/' + name)
        self.probes = Probes(self.plan, self.github, doctor)
        self.wizard = Wizard(self.plan, self.github, self.probes)
        self.wizard.probe()

    def move(self, target):
        while self.wizard.current != target:
            fields = self.wizard.page().fields
            if self.wizard.page().optional:
                self.wizard.skip()
            else:
                self.wizard.advance({f.name: f.value for f in fields})

    def choose(self, values):
        self.wizard.advance(values)
        self.wizard.advance(values)


class WizardTests(WizardFixture, unittest.TestCase):
    def setUp(self):
        self.setup()

    def test_collection_back_and_cancel_create_no_directories_or_remote_effects(self):
        self.move('review')
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.github.writes, [])
        with self.assertRaisesRegex(AgentError, 'defaults to no'):
            self.wizard.advance({'confirm': False})
        self.wizard.back()
        self.assertEqual(self.wizard.current, 'retrospectives')
        self.wizard.cancelled = True
        self.assertIn('No files, directories', self.wizard.result_text())
        self.assertFalse(self.path.parent.exists())

    def test_apply_defaults_exact_preview_and_manual_recovery_commands(self):
        self.move('review')
        expected = self.wizard.plan.targets.copy()
        self.wizard.advance({'confirm': True})
        self.assertEqual(self.wizard.current, 'result')
        self.assertTrue(self.wizard.applied.ok)
        for path, content in expected.items():
            self.assertEqual(path.read_text(), content)
        config = load_config(self.path)
        self.assertTrue(all(not a.runtime_args for a in config.agents))
        self.assertFalse(config.queue.priority.labels)
        self.assertTrue(all(a.retrospectives is None for a in config.agents))
        text = self.wizard.result_text()
        self.assertIn('launch readiness still requires', text)
        self.assertIn('gh label create needs-preparation', text)
        self.assertEqual(self.github.writes, [])

    def test_explicit_permissions_preview_and_deferral_for_both_runtimes(self):
        for cli in ('codex', 'claude'):
            for enabled in (False, True):
                with self.subTest(cli=cli, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'workflow.yaml'
                    choices = Choices(f'{cli}:model:high', permissions=enabled)
                    plan = make_plan(path, 'org/project', choices)
                    self.assertFalse(path.exists())
                    for a in plan.config.agents:
                        self.assertEqual(list(a.runtime_args_for(a.runtimes[0])), permission_args(cli) if enabled else [])
                    if cli == 'claude':
                        self.assertIn('project check command', plan.preview())
                    self.assertTrue(apply_plan(plan, self.github).ok)
                    self.assertTrue(all(bool(a.runtime_args) == enabled for a in load_config(path).agents))

    def test_runtime_selection_rejects_invalid_and_resets_permission_choice(self):
        self.move('policy')
        self.wizard.choices.permissions = True
        with self.assertRaises(AgentError):
            self.wizard.advance({'runtime': 'unsupported:model:high'})
        self.assertEqual(self.wizard.choices.runtime, 'codex:model:high')
        self.wizard.advance({'runtime': 'claude:opus:high'})
        self.assertFalse(self.wizard.choices.permissions)
        self.assertIn('unattended edits', self.wizard.page().text)
        self.assertIn('Bash({report_command} report *)', self.wizard.page().text)
        self.wizard.advance({'permissions': False})
        self.assertFalse(self.wizard.choices.permissions)
        self.assertEqual(self.wizard.current, 'files')

    def test_workflow_label_opt_in_only_missing_and_preserves_newly_created_label(self):
        self.move('files')
        self.wizard.advance({'workflow_labels': True})
        self.move('review')
        remote = self.wizard.remote_labels()
        self.assertTrue(remote)
        self.assertNotIn('ready', {l.name for l in remote})
        self.assertIn(remote[0].command('org/project'), self.wizard.page().text)
        self.github.label_names.append(remote[0].name)
        self.wizard.advance({'confirm': True})
        self.assertTrue(self.wizard.applied.ok)
        self.assertEqual({write[1] for write in self.github.writes}, {label.name for label in remote[1:]})
        self.assertIn('Kept existing label', self.wizard.result_text())

    def test_optional_screens_and_priority_validation_default_and_separate_label_choice(self):
        self.move('priority')
        self.assertEqual(self.wizard.current, 'priority')
        for labels, default in (('urgent, URGENT', ''), ('urgent, normal', 'other'), ('', '')):
            with self.assertRaises(AgentError):
                self.wizard.advance({'labels': labels, 'default': default})
        self.choose({'labels': 'urgent, normal', 'default': ''})
        self.assertEqual(self.wizard.current, 'priority-labels')
        self.assertFalse(self.wizard.page().fields[0].value)
        self.wizard.skip()
        self.assertEqual(self.wizard.current, 'retrospectives')
        self.assertEqual(self.wizard.plan.config.queue.priority.labels, ('urgent', 'normal'))
        self.assertIsNone(self.wizard.plan.config.queue.priority.default)
        self.assertEqual(self.wizard.remote_labels(), [])
        self.wizard.skip()
        self.wizard.advance({'confirm': True})
        self.assertIn('gh label create urgent', self.wizard.result_text())
        self.assertIn('retrospectives', self.wizard.pages())

    def test_priority_skip_retains_equal_priority_and_existing_default_accepts(self):
        self.move('priority')
        self.choose({'labels': 'urgent, normal', 'default': 'normal'})
        self.wizard.back()
        self.wizard.skip()
        self.assertFalse(self.wizard.plan.config.queue.priority.labels)
        self.assertFalse(self.wizard.choices.create_priority_labels)
        self.assertEqual(self.wizard.current, 'retrospectives')

    def test_priority_label_creation_requires_separate_opt_in(self):
        self.move('priority')
        self.choose({'labels': 'urgent, normal', 'default': 'normal'})
        self.assertEqual(self.github.writes, [])
        self.wizard.advance({'create_priority_labels': True})
        self.assertEqual([label.name for label in self.wizard.remote_labels()], ['urgent', 'normal'])
        self.assertEqual(self.github.writes, [])
        self.wizard.skip()
        self.wizard.advance({'confirm': True})
        self.assertEqual([write[1] for write in self.github.writes], ['urgent', 'normal'])

    def test_local_repository_override_is_effective_without_changing_selected_starter_repository(self):
        self.path.parent.mkdir()
        (self.path.parent / 'ub-agents.local.yaml').write_text('repository: other/project\n')
        plan = make_plan(self.path, 'org/project', self.wizard.choices)
        self.assertEqual(plan.repository, 'org/project')
        self.assertEqual(plan.config.repository, 'other/project')
        wizard = Wizard(plan, DoctorGitHub(), self.probes)
        wizard.started = True
        wizard.advance({})
        self.assertIn('repository: "org/project"', wizard.plan.targets[self.path])
        self.assertEqual(wizard.plan.config.repository, 'other/project')

    def test_dangling_starter_config_symlink_refuses_before_writing(self):
        self.path.parent.mkdir()
        self.path.symlink_to(self.path.parent / 'missing.yaml')
        with self.assertRaisesRegex(AgentError, 'Starter files already exist'):
            make_plan(self.path, 'org/project', Choices('codex:model:high'))
        self.assertFalse((self.path.parent / 'missing.yaml').exists())

    def test_retrospectives_verify_exact_urls_never_post_and_skip_turns_off(self):
        self.move('retrospectives')
        values = {role: '' for role in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        for invalid in ('-1', '0', '1.5', 'https://github.com/org/project/discussions/203'):
            with self.assertRaises(AgentError):
                self.wizard.advance(dict(values, implementer=invalid))
        self.github.boards[203]['url'] = 'https://github.com/other/project/discussions/203'
        with self.assertRaisesRegex(AgentError, 'invalid or unreadable'):
            self.wizard.advance(dict(values, implementer='203'))
        self.github.boards[203]['url'] = 'https://github.com/org/project/discussions/203'
        self.choose(dict(values, implementer='203'))
        self.assertEqual(self.wizard.current, 'review')
        self.assertIn('write capability verified', self.wizard.option_summary())
        self.assertEqual(self.wizard.plan.config.agents[1].retrospectives, 203)
        self.wizard.back()
        self.wizard.skip()
        self.assertTrue(all(a.retrospectives is None for a in self.wizard.plan.config.agents))
        self.assertEqual(self.github.writes, [])

    def test_retrospective_read_only_or_locked_access_pauses_with_retry(self):
        self.move('retrospectives')
        values = {role: '' for role in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        for field in ('viewerCanUpdate', 'locked', 'closed'):
            original = self.github.boards[203][field]
            self.github.boards[203][field] = not original
            with self.assertRaisesRegex(AgentError, 'posting/write access'):
                self.wizard.advance(dict(values, implementer='203'))
            self.assertTrue(self.wizard.board_waiting)
            self.assertFalse(self.path.parent.exists())
            self.assertEqual(self.github.writes, [])
            self.github.boards[203][field] = original
        self.choose(dict(values, implementer='203'))
        self.assertFalse(self.wizard.board_waiting)
        self.assertEqual(self.wizard.current, 'review')

    def test_optional_steps_are_direct_ordered_default_skip_with_benefits(self):
        self.move('knowledge')
        self.assertIn('Uberblick is a shared knowledge base for agents and humans alike. Learn more at https://uberblick.at', self.wizard.page().text)
        for name in ('knowledge', 'priority', 'retrospectives'):
            self.assertEqual(self.wizard.current, name)
            self.assertTrue(self.wizard.page().optional)
            self.assertIn('You can enable this later', self.wizard.page().text)
            self.wizard.skip()
        self.assertNotIn('features', self.wizard.pages())
        self.assertEqual(self.wizard.current, 'review')
        self.assertFalse(self.path.parent.exists())

    def test_selected_config_yaml_preview_precedes_optional_confirmation(self):
        self.move('priority')
        values = {'labels': 'urgent, normal', 'default': 'normal'}
        self.wizard.advance(values)
        self.assertEqual(self.wizard.current, 'priority')
        text = self.wizard.page().text
        self.assertIn(f'This adds to {self.path}:', text)
        self.assertIn('queue:\n  priority:', text)
        self.assertIn('default: normal', text)
        self.assertNotIn('dependencies: wait', text)
        self.wizard.advance(values)
        self.move('retrospectives')
        values = {role: '' for role in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        self.wizard.advance(dict(values, reviewer='203'))
        self.assertEqual(self.wizard.current, 'retrospectives')
        self.assertIn('agents:\n  reviewer:\n    retrospectives: 203', self.wizard.page().text)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.github.writes, [])

    def test_retrospective_access_lost_at_apply_prevents_all_writes(self):
        self.wizard.choices.retrospectives = {'reviewer': 203}
        self.wizard.plan = make_plan(self.path, 'org/project', self.wizard.choices)
        self.wizard.current = 'review'
        with patch.object(Boards, 'verify', side_effect=AgentError('denied')):
            with self.assertRaisesRegex(AgentError, 'no longer be verified'):
                self.wizard.advance({'confirm': True})
        self.assertFalse(self.path.parent.exists())

    def test_requested_creation_is_previewed_then_verified_before_writing_config(self):
        self.move('retrospectives')
        values = {role: '' for role in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        values.update(reviewer='create: Reviewer lessons', category='General')
        self.wizard.advance(values)
        text = self.wizard.page().text
        self.assertIn('Create public discussion in org/project: Reviewer lessons', text)
        self.assertIn('Category: General', text)
        self.assertNotIn('retrospectives:', self.wizard.plan.targets[self.path])
        self.assertEqual(self.github.writes, [])
        self.wizard.advance(values)
        self.assertIn('Reviewer lessons', self.wizard.page().text)
        self.wizard.advance({'confirm': True})
        self.assertTrue(self.wizard.applied.ok, self.wizard.applied.error)
        self.assertEqual(load_config(self.path).agents[2].retrospectives, 204)
        self.assertIn('Created public board', self.wizard.result_text())

    def test_manual_board_creation_pause_retry_and_changed_destination_gets_new_preview(self):
        self.move('retrospectives')
        values = {role: '' for role in ('issue-preparer', 'implementer', 'reviewer', 'integrator')}
        values.update(reviewer='create: Reviewer lessons', category='General')
        self.github.discussion_metadata['viewerPermission'] = 'READ'
        with self.assertRaisesRegex(AgentError, 'create them manually'):
            self.wizard.advance(values)
        self.assertTrue(self.wizard.board_waiting)
        self.assertFalse(self.path.parent.exists())
        self.github.discussion_metadata['viewerPermission'] = 'WRITE'
        self.wizard.advance(values)
        self.assertIn('Reviewer lessons', self.wizard.page().text)
        self.github.boards[204] = self.github.board(204, 'Reviewer lessons')
        self.wizard.advance(values)
        # A changed discovery result must be shown before optional confirmation.
        self.assertEqual(self.wizard.current, 'retrospectives')
        self.assertIn('retrospectives: 204', self.wizard.page().text)
        self.wizard.advance(values)
        self.assertEqual(self.wizard.current, 'review')
        self.assertFalse(self.wizard.choices.board_requests)
        self.assertEqual(self.github.writes, [])

    def test_existing_file_and_ignore_race_prevent_any_apply(self):
        self.move('review')
        self.path.parent.mkdir()
        ignore = self.path.parent / '.gitignore'
        ignore.write_text('changed\n')
        self.wizard.advance({'confirm': True})
        self.assertFalse(self.wizard.applied.ok)
        self.assertEqual(self.wizard.applied.completed, [])
        self.assertFalse(self.path.exists())
        self.assertEqual(ignore.read_text(), 'changed\n')

    def test_local_override_changed_after_review_prevents_writes(self):
        self.move('review')
        self.path.parent.mkdir()
        (self.path.parent / 'ub-agents.local.yaml').write_text('repository: other/project\n')
        self.wizard.advance({'confirm': True})
        self.assertFalse(self.wizard.applied.ok)
        self.assertEqual(self.wizard.applied.completed, [])
        self.assertIn('changed during setup', self.wizard.result_text())
        self.assertFalse(self.path.exists())

    def test_partial_label_failure_records_completed_effects_and_remaining_commands(self):
        self.move('files')
        self.wizard.advance({'workflow_labels': True})
        self.move('review')
        create = self.github.create_label
        def failing(name, description, color):
            if self.github.writes:
                raise AgentError('private response ghp-secret')
            create(name, description, color)
        with patch.object(self.github, 'create_label', side_effect=failing):
            self.wizard.advance({'confirm': True})
        self.assertFalse(self.wizard.applied.ok)
        self.assertTrue(self.path.is_file())
        self.assertEqual(len(self.github.writes), 1)
        text = self.wizard.result_text()
        self.assertIn('Completed effects remain', text)
        self.assertIn('Remaining work', text)
        self.assertNotIn('private response', text)

    def test_partial_file_failure_reports_remaining_files_without_remote_writes(self):
        original = Path.open
        def failing(path, *args, **kwargs):
            if path.name == 'reviewer.md' and args and args[0] == 'x':
                raise PermissionError('write denied')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', failing):
            result = apply_plan(self.plan, self.github)
        self.assertFalse(result.ok)
        self.assertTrue(self.path.exists())
        self.assertFalse((self.path.parent / '.agents/reviewer.md').exists())
        self.assertIn(f'Create {self.path.parent / ".agents/reviewer.md"}', result.remaining)
        self.assertEqual(self.github.writes, [])
        self.wizard.applied = result
        self.assertIn(f'Remaining content for {self.path.parent / ".agents/reviewer.md"}', self.wizard.result_text())

    def test_interrupt_during_apply_reports_completed_effects_instead_of_cancelled_no_writes(self):
        self.move('files')
        self.wizard.advance({'workflow_labels': True})
        self.move('review')
        with patch.object(self.github, 'create_label', side_effect=KeyboardInterrupt):
            self.wizard.advance({'confirm': True})
        self.assertFalse(self.wizard.applied.ok)
        self.assertTrue(self.path.exists())
        self.assertIn('Apply interrupted', self.wizard.result_text())
        self.assertNotIn('No files, directories', self.wizard.result_text())

    def test_failed_probes_and_unreadable_labels_remain_actionable_and_deferred(self):
        self.github.auth_error = AgentError('secret')
        self.github.repository_error = AgentError('secret')
        self.github.label_error = AgentError('secret')
        self.probes.run()
        text = findings(self.probes.prerequisites + self.probes.welcome + self.probes.checkout)
        self.assertIn('FAIL:', text)
        self.assertIn('UNAVAILABLE / SKIPPED', text)
        self.assertIn('blocks launch; setup may continue', text)
        self.assertNotIn('secret', text)
        self.wizard.current = 'files'
        self.assertEqual(self.wizard.page().fields, ())
        self.assertIn('Creation is deferred', self.wizard.page().text)
        self.assertEqual(self.github.writes, [])

    def test_missing_executables_skip_auth_and_version_lookups(self):
        self.probes = Probes(self.plan, self.github, Doctor(runner=self.runner, which=lambda name: None))
        self.github.reads.clear()
        self.probes.run()
        self.assertFalse(self.github.reads)
        self.assertIn('Install gh', findings(self.probes.welcome))
        self.assertIn('login not checked', findings(self.probes.runtime))

    def test_fallback_cancel_and_eof_leave_no_directories(self):
        for answer in ('c', EOFError()):
            with patch('builtins.input', side_effect=[answer]), redirect_stdout(io.StringIO()):
                self.assertTrue(line_wizard(self.wizard))
            self.assertFalse(self.path.parent.exists())


class WizardUITests(WizardFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()

    async def test_reference_sizes_use_full_width_single_footer_row_and_permissions_keys(self):
        for width, height, key in ((92, 19, 'n'), (48, 25, 'y')):
            self.wizard.current = 'permissions'
            app = SetupApp(self.wizard)
            with patch.object(self.wizard, 'probe'):
                async with app.run_test(size=(width, height)) as pilot:
                    await pilot.pause()
                    self.assertEqual(app.query_one('#frame').region.width, width)
                    footer = app.query_one('#actions').region
                    self.assertEqual(footer.height, 1)
                    visible = [button for button in app.query(Button) if button.display]
                    self.assertEqual({button.id for button in visible}, {'continue', 'skip', 'cancel'})
                    self.assertEqual({button.region.y for button in visible}, {footer.y})
                    self.assertEqual(app.focused.id, 'skip')
                    for button in visible:
                        self.assertEqual(button.region.height, 1)
                        self.assertLessEqual(button.region.right, width)
                        self.assertGreaterEqual(button.region.width, len(str(button.label)))
                    if width == 92:
                        self.assertEqual(str(app.query_one('#continue', Button).label), '[Accept and enable permissions (y)]')
                        self.assertEqual(str(app.query_one('#skip', Button).label), '[Continue without permissions (n)]')
                        self.assertEqual(str(app.query_one('#cancel', Button).label), '[Cancel (esc)]')
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(self.wizard.current, 'files')
                    self.assertEqual(self.wizard.choices.permissions, key == 'y')
                    self.assertEqual(app.query_one('#actions').region.height, 1)
                    await pilot.press('escape')
            self.assertFalse(self.path.parent.exists())

    async def test_optional_skip_default_and_uberblick_link_note_outside_frame(self):
        self.move('knowledge')
        app = SetupApp(self.wizard)
        with patch.object(self.wizard, 'probe'):
            async with app.run_test(size=(48, 25)) as pilot:
                await pilot.pause()
                notes = app.query_one('#notes')
                self.assertIn('https://uberblick.at', str(notes.render()))
                self.assertGreaterEqual(notes.region.y, app.query_one('#frame').region.bottom)
                for expected in ('knowledge', 'priority', 'retrospectives'):
                    self.assertEqual(self.wizard.current, expected)
                    self.assertEqual(app.focused.id, 'skip')
                    self.assertEqual(app.query_one('#actions').region.height, 1)
                    for button in app.query(Button):
                        if button.display:
                            self.assertGreaterEqual(button.region.width, len(str(button.label)))
                    await pilot.press('enter')
                    await pilot.pause()
                self.assertEqual(self.wizard.current, 'review')
                await pilot.press('escape')
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.github.writes, [])

    async def test_board_pause_retry_and_preview_do_not_write_before_apply(self):
        self.move('retrospectives')
        self.github.boards[203]['viewerCanUpdate'] = False
        app = SetupApp(self.wizard)
        with patch.object(self.wizard, 'probe'):
            async with app.run_test(size=(92, 19)) as pilot:
                await pilot.pause()
                app.query_one('#field-implementer', Input).value = '203'
                await pilot.press('ctrl+n')
                await pilot.pause()
                self.assertTrue(self.wizard.board_waiting)
                self.assertIn('Retry', str(app.query_one('#continue', Button).label))
                self.github.boards[203]['viewerCanUpdate'] = True
                await pilot.press('ctrl+r')
                await pilot.pause()
                self.assertEqual(self.wizard.current, 'retrospectives')
                self.assertIn('retrospectives: 203', str(app.query_one('#copy').render()))
                await pilot.press('ctrl+n')
                await pilot.pause()
                self.assertEqual(self.wizard.current, 'review')
                await pilot.press('escape')
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.github.writes, [])

    async def test_framed_pages_narrow_resize_keyboard_navigation_cancel_no_writes(self):
        app = SetupApp(self.wizard)
        with patch.object(self.wizard, 'probe'):
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                frame = app.query_one('#frame').region
                footer = app.query_one('#actions').region
                for expected in ('prerequisites', 'checkout', 'policy'):
                    await pilot.press('ctrl+n')
                    await pilot.pause()
                    self.assertEqual(self.wizard.current, expected)
                    self.assertEqual(app.query_one('#frame').region, frame)
                    self.assertEqual(app.query_one('#actions').region, footer)
                await pilot.press('ctrl+b')
                self.assertEqual(self.wizard.current, 'checkout')
                await pilot.resize_terminal(45, 22)
                await pilot.pause()
                self.assertTrue(app.query_one('#frame').has_class('narrow'))
                narrow_frame = app.query_one('#frame').region
                narrow_footer = app.query_one('#actions').region
                await pilot.press('ctrl+b')
                await pilot.pause()
                self.assertEqual(app.query_one('#frame').region, narrow_frame)
                self.assertEqual(app.query_one('#actions').region, narrow_footer)
                for button in app.query(Button):
                    self.assertLessEqual(button.region.right, 45)
                    self.assertLessEqual(button.region.bottom, 22)
                await pilot.press('escape')
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.github.writes, [])

    async def test_review_apply_is_default_no_and_finish_shows_actual_result(self):
        self.move('review')
        app = SetupApp(self.wizard)
        with patch.object(self.wizard, 'probe'):
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                self.assertTrue(app.query_one('#continue', Button).disabled)
                await pilot.press('ctrl+n')
                self.assertFalse(self.path.exists())
                self.assertIn('defaults to no', str(app.query_one('#error').render()))
                app.query_one('#field-confirm', Checkbox).value = True
                await pilot.pause()
                await pilot.press('ctrl+n')
                await pilot.pause()
                self.assertEqual(self.wizard.current, 'result')
                self.assertTrue(self.path.exists())
                self.assertIn('Finish', str(app.query_one('#continue', Button).label))
                await pilot.press('ctrl+n')


class DefaultsTerminalTests(unittest.TestCase):
    def test_defaults_with_existing_retrospective_override_do_not_probe_or_mutate_github(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'workflow.yaml'
            (root / 'ub-agents.local.yaml').write_text('agents:\n  implementer:\n    retrospectives: 203\n')
            with patch('subprocess.run', side_effect=AssertionError('probe')), \
                    patch('builtins.input', side_effect=AssertionError('prompt')), \
                    patch('ub_agents.cli.GitHub', return_value=object()), redirect_stdout(io.StringIO()):
                # Construction is allowed; no read or write is allowed on this path.
                self.assertEqual(main(['init', '--config', str(path), '--repository', 'org/project', '--use-defaults']), 0)
            self.assertEqual(load_config(path).agents[1].retrospectives, 203)

    def test_ci_and_each_non_tty_path_bypass_wizard_without_explicit_defaults(self):
        for stdin_tty, stdout_tty, ci in ((False, True, ''), (True, False, ''), (True, True, '1')):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty, ci=ci), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'workflow.yaml'
                with patch('ub_agents.cli.sys.stdin.isatty', return_value=stdin_tty), \
                        patch('ub_agents.init_ui.run_wizard', side_effect=AssertionError('wizard')), \
                        patch('subprocess.run', side_effect=AssertionError('probe')), \
                        patch('builtins.input', side_effect=AssertionError('prompt')), \
                        patch.dict('os.environ', {'CI': ci}), redirect_stdout(io.StringIO()) as stdout:
                    with patch.object(stdout, 'isatty', return_value=stdout_tty):
                        self.assertEqual(main(['init', '--config', str(path), '--repository', 'org/project']), 0)
                self.assertTrue(path.exists())
                self.assertIn('no GitHub labels created', stdout.getvalue())

    def test_explicit_defaults_in_real_tty_do_not_prompt_probe_or_enter_alternate_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workflow.yaml'
            script = '''
from unittest.mock import patch
from ub_agents.cli import main
import sys
with patch('builtins.input', side_effect=AssertionError('prompt')), patch('subprocess.run', side_effect=AssertionError('probe')):
    sys.exit(main(['--config', sys.argv[1], 'init', '--repository', 'org/project', '--use-defaults']))
'''
            with Terminal(script, path, env={'CI': None}) as terminal:
                terminal.expect(b'Defaults:', b'gh label create')
                terminal.wait_exit(sequences=False)
                self.assertNotIn(b'\x1b[?1049h', terminal.transcript)
            self.assertTrue(path.exists())
