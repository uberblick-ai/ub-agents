import asyncio
import base64
import io
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from xml.etree import ElementTree
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from rich.console import Console
from rich.text import Text
from tests.test_view_data import event, fixture, publish_snapshot
from tests.test_log_reader import FIXTURE, progress, record, result, tool
from tests.test_view_github import reply
from tests.support import MemoryPublisher, RecordingDescriptionTransport, agent, config, issue
from ub_agents.coordination import Plan
from ub_agents.observations import Observations
from ub_agents.view_github import DescriptionLoads, Response, parse_response

from textual.geometry import Size
from textual.widgets import Markdown, Static, Tab, TabbedContent, TabPane, Tabs, Tree
from ub_agents.view_ui import (ItemTabs, KeyHelp, LogPane, MAX_RENDER_LINES, RecentActivity,
                               RawAccess, UpdateBanner, View, pane_line)
from ub_agents.view_worker import LocalWorker
from ub_agents.view_data import Session, work_pane
from ub_agents.view_theme import theme_style
from ub_agents.view_unblock import ACTION_MARKER, ActionComment


class ViewUITests(unittest.IsolatedAsyncioTestCase):
    async def test_mouse_release_and_y_copy_exact_selection_in_panes_and_overlays(self):
        launcher = Mock()
        app = View(self.root, self.path, launcher=launcher)
        value = '  café α\nsecond line  '
        selected = 'café α\nsecond line'
        sequence = '\x1b]52;c;' + base64.b64encode(selected.encode('utf-8')).decode('ascii') + '\a'
        with (patch('ub_agents.view_ui.local_pbcopy', return_value=None),
              patch.object(app, 'raw_details', return_value=value)):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                await app.query_one('#issue VerticalScroll').mount(
                    Static(Text(value), id='copy_sample'), before=app.query_one('#issue_text'))
                for key, selector in (('2', '#copy_sample'), ('p', '#raw_details'), ('?', '#raw_details')):
                    with self.subTest(surface=key):
                        await pilot.press(key)
                        widget = app.screen.query_one(selector, Static)
                        widget.update(Text(value))
                        await pilot.pause()
                        footer = app.screen.query_one('#raw_status' if key != '2' else '#status', Static)
                        with patch.object(app._driver, 'write') as write:
                            def clipboard_writes():
                                return [call.args[0] for call in write.call_args_list
                                        if call.args[0].startswith('\x1b]52;')]

                            await pilot.mouse_down(widget, offset=(2, 0))
                            await pilot.hover(widget, offset=(10, 1))
                            self.assertEqual(clipboard_writes(), [])
                            await pilot.mouse_up(widget, offset=(10, 1))
                            self.assertEqual(app.screen.get_selected_text(), selected)
                            self.assertEqual(app.clipboard, selected)
                            self.assertEqual(clipboard_writes(), [sequence])
                            self.assertEqual(footer.render().plain.strip(), f'copied {len(selected)} characters')
                            self.assertEqual(footer.render().cell_length, footer.size.width)
                            self.assertFalse(app._exit)
                            self.assertIsNone(app.shutdown)
                            if key == '2':
                                await self.ready(app, pilot, lambda: 'copied' not in footer.render().plain)
                                self.assertIn('q quit', footer.render().plain)
                            await pilot.press('y')
                            self.assertEqual(clipboard_writes(), [sequence, sequence])
                            self.assertEqual(footer.render().plain.strip(), f'copied {len(selected)} characters')
                        if key != '2':
                            await pilot.press('escape')
                launcher.drain.assert_not_called()
                launcher.interrupt.assert_not_called()
                with patch.object(app, 'copy_to_clipboard') as copy:
                    await pilot.press('ctrl+c')
                    copy.assert_not_called()
                launcher.interrupt.assert_called_once_with()
                self.assertFalse(app._exit)
                app.exit()
        app.worker.thread.join(2)

    async def test_click_empty_selection_and_y_without_selection_copy_nothing(self):
        app = View(self.root, self.path)
        with (patch.object(app, 'copy_to_clipboard') as copy,
              patch('ub_agents.view_ui.local_pbcopy') as pbcopy):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                for key in ('2', 'p', '?'):
                    await pilot.press(key)
                    selector = '#issue_text' if key == '2' else '#raw_details'
                    widget = app.screen.query_one(selector, Static)
                    await pilot.click(widget)
                    await pilot.press('y')
                    # A drag across blank text yields an empty string rather than None.
                    with patch.object(app.screen, 'get_selected_text', return_value=''):
                        app.action_copy_selection()
                    copy.assert_not_called()
                    pbcopy.assert_not_called()
                    self.assertEqual(app.copy_notice, '')
                    self.assertFalse(app._exit)
                    if key != '2':
                        await pilot.press('escape')
                await pilot.press('ctrl+c')
            self.assertTrue(app._exit)
            copy.assert_not_called()
        app.worker.thread.join(2)

    async def test_pbcopy_worker_does_not_delay_osc52_keyboard_or_stop(self):
        launcher = Mock()
        app = View(self.root, self.path, launcher=launcher)
        process = Mock(returncode=None, wait=AsyncMock())
        started = asyncio.Event()

        async def communicate(value):
            started.set()
            await asyncio.Event().wait()

        process.communicate = AsyncMock(side_effect=communicate)
        with (patch('ub_agents.view_ui.local_pbcopy', return_value='/usr/bin/pbcopy'),
              patch('ub_agents.view_clipboard.asyncio.create_subprocess_exec',
                    new=AsyncMock(return_value=process))):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                with (patch.object(app.screen, 'get_selected_text', return_value='selected text'),
                      patch.object(app, 'copy_to_clipboard') as copy):
                    await pilot.press('y')
                    copy.assert_called_once_with('selected text')
                    await asyncio.wait_for(started.wait(), timeout=1)
                    self.assertIn('copied 13 characters', app.query_one('#status', Static).render().plain)
                    self.assertFalse(app._exit)
                    await pilot.press('2')
                    self.assertEqual(app.query_one(TabbedContent).active, 'issue')
                    await pilot.press('ctrl+c')
                    launcher.interrupt.assert_called_once_with()
                    self.assertFalse(app._exit)
                    copy.assert_called_once_with('selected text')
                    app.exit()
            process.kill.assert_called_once_with()
            process.wait.assert_awaited_once_with()
        app.worker.thread.join(2)

    async def test_shutdown_replaces_every_surface_keeps_elapsed_time_and_allows_interrupt(self):
        now = datetime.now(timezone.utc)
        self.state['histories']['114']['kind'] = 'pr'
        self.state['histories']['114']['runs'][-1]['time'] = (now - timedelta(seconds=252)).isoformat()
        self.state['update'] = {'text': 'An update is available'}
        publish_snapshot(self.path, self.state)
        launcher = Mock()
        app = View(self.root, self.path, launcher=launcher)
        with patch('ub_agents.view_work.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                # The selected item and help overlay must not replace the running item.
                app.select(next(k for k, row in app.rows.items() if row.item == 12))
                await pilot.press('?')
                await pilot.press('q')
                shutdown = app.query_one('#shutdown', Static)
                expected = ('Shutting down the launcher\n\n'
                            'Waiting for ⌥114 (implementer, 04:12) to finish.\n'
                            'No new work will be claimed. Press Ctrl-C to stop now.')
                self.assertEqual(shutdown.render().plain, expected)
                self.assertEqual(len(app.screen_stack), 1)
                self.assertFalse(app._exit)
                launcher.drain.assert_called_once_with()
                launcher.interrupt.assert_not_called()
                for size in ((110, 32), (60, 16), (59, 15), (160, 45)):
                    await pilot.resize_terminal(*size)
                    self.assertEqual(shutdown.region.size, app.size)
                    self.assertEqual(shutdown.styles.content_align, ('center', 'middle'))
                    lines = [shutdown.render_line(y).text for y in range(app.size.height)]
                    visible = [(y, line) for y, line in enumerate(lines) if line.strip()]
                    self.assertEqual([line.strip() for _, line in visible],
                                     [line for line in expected.splitlines() if line])
                    self.assertLessEqual(abs(visible[0][0] - (app.size.height - 4) // 2), 1)
                    for _, line in visible:
                        self.assertLessEqual(abs(len(line) - len(line.lstrip())
                                                 - (app.size.width - Text(line.strip()).cell_len) // 2), 1)
                    for selector in ('#body', '#status', '#size_warning', '#update'):
                        self.assertFalse(app.query_one(selector).display, selector)
                clock.now.return_value = now + timedelta(seconds=3)
                app.update_status()
                self.assertIn('04:15', shutdown.render().plain)
                # Outcome time must not reset the elapsed assignment time during cleanup.
                self.state['histories']['114']['runs'][-1].update(
                    acceptance='finalized', time=clock.now.return_value.isoformat())
                self.state['assignment']['process'] = 'exited'
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: app.session.data['assignment']['process'] == 'exited')
                self.assertIn('04:15', shutdown.render().plain)
                await pilot.press('q', '?', 'escape', 'r', '2', 'tab')
                launcher.drain.assert_called_once_with()
                launcher.poll.assert_not_called()
                self.assertEqual(len(app.screen_stack), 1)
                await pilot.press('ctrl+c')
                self.assertEqual(shutdown.render().plain, 'Stopping the launcher\n\n'
                                 'Terminating ⌥114 (implementer) and releasing its claim…')
                self.assertFalse(app._exit)
                launcher.interrupt.assert_called_once_with()
                await pilot.press('q', 'ctrl+c', 'ctrl+c')
                launcher.drain.assert_called_once_with()
                launcher.interrupt.assert_called_once_with()
                app.exit()  # The launcher's lifetime channel closes the actual view.
        app.worker.thread.join(2)

    async def test_stop_screens_name_issues_recovery_and_idle_assignments(self):
        for running in (True, False):
            for key, title in (('q', 'Shutting down the launcher'), ('ctrl+c', 'Stopping the launcher')):
                with self.subTest(running=running, key=key):
                    if running:
                        self.state['assignment']['process'] = 'recovery'
                    else:
                        self.state['assignment'] = None
                    publish_snapshot(self.path, self.state)
                    app = View(self.root, self.path, launcher=Mock())
                    async with app.run_test(size=(60, 16)) as pilot:
                        await self.ready(app, pilot, lambda: app.session is not None)
                        await pilot.press(key)
                        message = app.query_one('#shutdown', Static).render().plain
                        self.assertTrue(message.startswith(title + '\n\n'))
                        self.assertIn('#114 (implementer' if running else 'No run in progress.', message)
                        self.assertFalse(app._exit)
                        app.exit()
                    app.worker.thread.join(2)

    async def test_tick_ignores_pending_result_during_screen_teardown(self):
        for attached in (False, True):
            with self.subTest(attached=attached):
                app = View(self.root, self.path, launcher=Mock() if attached else None)
                close_all = app._close_all

                async def close_all_with_late_tick():
                    # run_test has begun shutdown, but the refresh timer can
                    # still fire while Textual removes the screen's widgets.
                    await app.query_one(UpdateBanner).remove()
                    with (patch.object(app.descriptions, 'poll') as poll,
                          patch.object(app.worker.results, 'get_nowait',
                                       return_value=Mock(session=app.session)) as result,
                          patch.object(app.worker, 'request') as request):
                        app.tick()
                        poll.assert_not_called()
                        result.assert_not_called()
                        request.assert_not_called()
                    await close_all()

                with patch.object(app, '_close_all', side_effect=close_all_with_late_tick) as teardown:
                    async with app.run_test(size=(110, 32)) as pilot:
                        await self.ready(app, pilot)
                        if attached:
                            await pilot.press('q')
                            self.assertFalse(app._exit)
                    teardown.assert_awaited_once()
                self.assertTrue(app.worker.stopping.is_set())
                app.worker.thread.join(2)

    async def test_poll_key_on_every_tab_pane_and_overlay_only_for_attached_launcher(self):
        self.state['latest_pass']['rows'].append(
            {'item': 20, 'agent': 'worker', 'state': 'blocked', 'reason': 'Needs a decision'})
        publish_snapshot(self.path, self.state)
        for attached in (False, True):
            with self.subTest(attached=attached):
                launcher = Mock() if attached else None
                app = View(self.root, self.path, launcher=launcher)
                async with app.run_test(size=(160, 45)) as pilot:
                    await self.ready(app, pilot)
                    app.select(next(k for k, row in app.rows.items() if row.group == 'Needs attention'))
                    await self.ready(app, pilot, lambda: app.unblock_visible)
                    status = app.query_one('#status', Static)
                    self.assertEqual('r poll now' in status.render().plain, attached)
                    presses = 0
                    for tab in ('1', '2', '3', '4'):
                        await pilot.press(tab)
                        for pane in ('#work', '#recent', '#output'):
                            app.query_one(pane).focus()
                            await pilot.press('r')
                            presses += 1
                    await pilot.press('?')
                    help_text = app.screen.query_one('#raw_details', Static).render().plain
                    self.assertEqual('r   Poll GitHub now' in help_text, attached)
                    await pilot.press('r', 'escape', '1', 'p', 'r', 'escape')
                    presses += 2
                    await pilot.resize_terminal(60, 16)
                    await self.ready(app, pilot, lambda: app.narrow and 'r poll now' not in status.render().plain)
                    self.assertNotIn('r poll now', status.render().plain)
                    await pilot.press('r', 'enter', 'r')
                    presses += 2
                    if attached:
                        self.assertEqual(launcher.poll.call_count, presses)
                        launcher.interrupt.assert_not_called()
                    await pilot.press('q')
                app.worker.thread.join(2)

    async def test_poll_footer_cooldown_countdown_and_rate_limit_in_local_time(self):
        now = datetime.now(timezone.utc)
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=400)).isoformat()}
        self.state['poll_now'] = {'cooldown_until': (now + timedelta(seconds=8)).isoformat(),
                                  'rate_limit_until': None}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path, launcher=Mock())
        with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(180, 45)) as pilot:
                await self.ready(app, pilot)
                status = app.query_one('#status', Static)
                self.assertIn('next poll 400s · poll now available in 8s', status.render().plain)
                clock.now.return_value = now + timedelta(seconds=3)
                app.update_status()
                self.assertIn('poll now available in 5s', status.render().plain)
                clock.now.return_value = now + timedelta(seconds=9)
                app.update_status()
                self.assertNotIn('poll now available', status.render().plain)
                reset = now + timedelta(seconds=400)
                self.state['poll_now']['rate_limit_until'] = reset.isoformat()
                self.state['activity'] = {'state': 'running assignment'}
                publish_snapshot(self.path, self.state)
                label = f'rate limited until {reset.astimezone():%H:%M} · r unavailable'
                await self.ready(app, pilot, lambda: f'running assignment · {label}' in status.render().plain)
                self.assertNotIn('poll now available', status.render().plain)
                await pilot.press('r')
                app.launcher.poll.assert_called_once_with()
                await pilot.press('q')
        app.worker.thread.join(2)

    async def test_running_poll_feedback_is_immediate_and_replaced_by_next_snapshot(self):
        now = datetime.now(timezone.utc)
        self.state['base_version'] = '9.8.7'
        for size in ((180, 45), (109, 31)):
            with self.subTest(size=size):
                self.state['activity'] = {'state': 'running assignment'}
                self.state['poll_now'] = {'cooldown_until': None, 'rate_limit_until': None, 'waiting': True}
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path, launcher=Mock())
                with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                    clock.now.return_value = now
                    async with app.run_test(size=size) as pilot:
                        await self.ready(app, pilot)
                        status = app.query_one('#status', Static)
                        prefix = 'ub-agents ' if size[0] >= 110 else ''
                        label = prefix + 'v9.8.7 · running assignment · polling'
                        app.action_poll_now()
                        self.assertIn(label, status.render().plain)
                        app.action_poll_now()  # A repeated press while refreshing keeps the label.
                        self.assertIn(label, status.render().plain)
                        await pilot.pause(0.2)  # Re-reading the same snapshot is not a new snapshot.
                        self.assertIn(label, status.render().plain)
                        self.state['poll_now']['refreshing'] = True
                        self.state['poll_now']['waiting'] = False
                        publish_snapshot(self.path, self.state)
                        await self.ready(app, pilot, lambda: not app.poll_feedback)
                        self.assertIn(label, status.render().plain)
                        clock.now.return_value = now + timedelta(seconds=3)
                        self.state['poll_now']['refreshing'] = False
                        self.state['poll_now']['waiting'] = True
                        publish_snapshot(self.path, self.state)
                        await self.ready(app, pilot, lambda: '· polling' not in status.render().plain)
                        app.action_poll_now()  # The last press still supplies the local cooldown.
                        self.assertIn('running assignment · poll now available in 7s', status.render().plain)
                        self.assertNotIn('· polling', status.render().plain)
                        self.state['poll_now']['cooldown_until'] = (now + timedelta(seconds=9)).isoformat()
                        publish_snapshot(self.path, self.state)
                        await self.ready(app, pilot, lambda: not app.poll_feedback)
                        self.assertIn('poll now available in 6s', status.render().plain)
                        clock.now.return_value = now + timedelta(seconds=11)
                        app.action_poll_now()
                        self.assertIn(label, status.render().plain)
                        self.assertEqual(app.launcher.poll.call_count, 4)
                        app.exit()
                app.worker.thread.join(2)

    async def test_dropped_running_poll_press_without_waiter_has_no_feedback_or_cooldown(self):
        for control in (None, {}, {'waiting': False, 'refreshing': False}):
            with self.subTest(control=control):
                self.state['activity'] = {'state': 'running assignment'}
                self.state['poll_now'] = control
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path, launcher=Mock())
                async with app.run_test(size=(180, 45)) as pilot:
                    await self.ready(app, pilot)
                    status = app.query_one('#status', Static)
                    await pilot.press('r', 'r')
                    self.assertIn('running assignment', status.render().plain)
                    self.assertNotIn('· polling', status.render().plain)
                    self.assertNotIn('poll now available', status.render().plain)
                    self.assertEqual(app.poll_feedback, {})
                    self.assertIsNone(app.poll_next_allowed)
                    self.assertEqual(app.launcher.poll.call_count, 2)
                    # No dropped press should prevent immediate feedback when
                    # queue planning next installs a waiter.
                    self.state['poll_now'] = {'waiting': True}
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: app.session.data.get('poll_now') == {'waiting': True})
                    app.action_poll_now()
                    self.assertIn('running assignment · polling', status.render().plain)
                    app.exit()
                app.worker.thread.join(2)

    async def test_reopened_view_shows_running_refresh_until_snapshot_clears_it(self):
        for attached in (False, True):
            with self.subTest(attached=attached):
                self.state['activity'] = {'state': 'running assignment'}
                self.state['poll_now'] = {'refreshing': True}
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path, launcher=Mock() if attached else None)
                async with app.run_test(size=(180, 45)) as pilot:
                    await self.ready(app, pilot)
                    status = app.query_one('#status', Static)
                    self.assertIn('running assignment · polling', status.render().plain)
                    await pilot.press('r')
                    self.assertIn('running assignment · polling', status.render().plain)
                    self.state['poll_now']['refreshing'] = False
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: '· polling' not in status.render().plain)
                    self.assertIn('running assignment', status.render().plain)
                    app.exit()
                app.worker.thread.join(2)

    async def test_poll_press_before_assignment_retains_local_cooldown(self):
        now = datetime.now(timezone.utc)
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=30)).isoformat(),
                                  'reason': 'next poll or runtime pause'}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path, launcher=Mock())
        with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(180, 45)) as pilot:
                await self.ready(app, pilot)
                status = app.query_one('#status', Static)
                await pilot.press('r')
                self.assertIn('next poll 30s', status.render().plain)
                self.assertNotIn('· polling', status.render().plain)
                clock.now.return_value = now + timedelta(seconds=2)
                self.state['activity'] = {'state': 'running assignment'}
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: 'running assignment' in status.render().plain)
                app.action_poll_now()
                self.assertIn('running assignment · poll now available in 8s', status.render().plain)
                self.assertNotIn('· polling', status.render().plain)
                app.exit()
        app.worker.thread.join(2)

    async def test_running_poll_press_respects_snapshot_cooldown_and_rate_limit(self):
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=8)
        for limited in (False, True):
            with self.subTest(limited=limited):
                self.state['activity'] = {'state': 'running assignment'}
                self.state['poll_now'] = {'cooldown_until': until.isoformat(),
                                          'rate_limit_until': until.isoformat() if limited else None}
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path, launcher=Mock())
                with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                    clock.now.return_value = now
                    async with app.run_test(size=(180, 45)) as pilot:
                        await self.ready(app, pilot)
                        status = app.query_one('#status', Static)
                        await pilot.press('r')
                        label = (f'rate limited until {until.astimezone():%H:%M} · r unavailable' if limited else
                                 'poll now available in 8s')
                        self.assertIn('running assignment · ' + label, status.render().plain)
                        self.assertNotIn('· polling', status.render().plain)
                        app.launcher.poll.assert_called_once_with()
                        app.exit()
                app.worker.thread.join(2)

    async def test_rate_limit_footer_keeps_standalone_countdown(self):
        now = datetime.now(timezone.utc)
        reset = now + timedelta(seconds=400)
        self.state['activity'] = {'state': 'waiting', 'until': reset.isoformat(),
                                  'reason': 'rate-limit reset'}
        for control in (None, {'rate_limit_until': reset.isoformat(), 'cooldown_until': None}):
            self.state['poll_now'] = control
            publish_snapshot(self.path, self.state)
            for attached in (False, True):
                with self.subTest(control=control, attached=attached):
                    app = View(self.root, self.path, launcher=Mock() if attached else None)
                    with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                        clock.now.return_value = now
                        async with app.run_test(size=(180, 45)) as pilot:
                            await self.ready(app, pilot)
                            value = app.query_one('#status', Static).render().plain
                            if attached:
                                self.assertIn(f'rate limited until {reset.astimezone():%H:%M} · r unavailable', value)
                            else:
                                self.assertIn('next poll 400s', value)
                                self.assertNotIn('rate limited', value)
                                self.assertNotIn('r unavailable', value)
                                self.assertNotIn('r poll now', value)
                            await pilot.press('q')
                    app.worker.thread.join(2)

    async def test_eligible_limit_heading_order_priority_and_stopping(self):
        self.state['assignment'] = None
        self.state['outcomes'] = []
        order = [30, 18, 42, 15, 9, 31, 22, 13, 37, 5] + list(range(100, 113))
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': n, 'agent': 'worker', 'kind': 'issue', 'title': 'Work', 'priority': 'urgent',
             'state': 'backoff' if n == 18 else 'ready'} for n in order]}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(160, 45)) as pilot:
            tree = app.query_one('#work', Tree)
            await self.ready(app, pilot, lambda: 'Eligible' in app.groups and
                             'urgent' in tree.render_line(app.groups['Eligible'].children[0]._line + 1 -
                                                         tree.scroll_offset.y).text)
            group = app.groups['Eligible']
            self.assertEqual(group.label.plain, 'Eligible · 23 · showing 10')
            self.assertEqual([app.rows[node.data].item for node in group.children],
                             [n for n in order if n != 18][:10])
            first = group.children[0]
            rendered = tree.render_line(first._line + 1 - tree.scroll_offset.y)
            urgent = next(segment for segment in rendered if 'urgent' in segment.text)
            self.assertEqual(urgent.style.color, theme_style(app, 'view-priority-urgent').color)
            self.assertTrue(tree.render_line(first._line - tree.scroll_offset.y).text.endswith('next'))
            self.state['activity'] = {'state': 'stopping'}
            publish_snapshot(self.path, self.state)
            label = 'Eligible · 23 · showing 10 · not claimed while stopping'
            await self.ready(app, pilot, lambda: group.label.plain == label)
            self.assertEqual(len(group.children), 10)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def asyncSetUp(self):
        # IsolatedAsyncioTestCase starts its loop in debug mode, which slows
        # Textual by about a third and reports every slow callback.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root, count=600)

    async def ready(self, app, pilot, condition=None):
        for _ in range(150):
            await pilot.pause(0.03)
            if condition() if condition else app.reading.page is not None:
                return
        self.fail('View did not become ready')

    def themed_fixture(self):
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': 20, 'agent': 'worker', 'state': 'blocked', 'reason': 'Needs a decision'},
            {'item': 21, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
        ]}
        self.state['omitted'] = {'plans': 2}
        self.state['update'] = {'text': 'New version available'}
        self.state['histories']['114']['runs'].append(
            {'agent': 'worker', 'result': 'blocked', 'time': self.state['published_at']})
        publish_snapshot(self.path, self.state)
        self.log.write_bytes(record(timestamp='2026-10-03T12:00:00Z', content=[{**tool(name='Edit'), 'input': {
            'file_path': 'a.py', 'new_string': 'one\ntwo\n', 'old_string': 'old'}}]))

    def screenshot_text(self, svg):
        return ''.join(ElementTree.fromstring(svg).itertext()).replace('\xa0', ' ')

    async def test_work_pane_width_follows_terminal_resizes(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            work, panes = app.query_one('#work_pane'), app.query_one('#panes')
            self.assertEqual(work.region.width, 46)
            for size, width in (((150, 32), 50), ((152, 32), 50), ((189, 32), 63),
                                ((200, 32), 64), ((109, 32), 109), ((200, 31), 200),
                                ((60, 16), 60),
                                ((110, 32), 46)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await self.ready(app, pilot, lambda: work.region.width == width)
                    self.assertEqual(work.region.width, width)
                    self.assertEqual(panes.display, not app.narrow)
                    if not app.narrow:
                        self.assertEqual(panes.region.width, size[0] - width - 1)
                        self.assertEqual(panes.region.x, work.region.right + 1)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_theme_screenshot_at_110_by_32_and_numbered_inert_mode_indicator(self):
        self.themed_fixture()
        with patch.dict(os.environ):
            os.environ.pop('NO_COLOR', None)
            app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one(Tree)
            tree.focus()
            await pilot.pause()
            svg = app.export_screenshot()
            (self.root / 'terminal-theme.svg').write_text(svg)
            visible = self.screenshot_text(svg)
            for value in ('╭', '╮', '╰', '╯', 'Work · pass complete',
                          'Log', '1 Log', '2 Issue', '3 Runs', 'Formatted', 'Raw',
                          'Running', 'Needs attention', 'Eligible', '┄'):
                self.assertIn(value, visible)
            self.assertNotIn('Launcher work', visible)
            self.assertEqual(app.theme, 'ub-agents')
            self.assertEqual(app.title, 'ub-agents launch — example/repo')
            work, panes = app.query_one('#work_pane'), app.query_one('#panes')
            self.assertEqual(work.border_title, 'Work · pass complete · omitted 2')
            self.assertEqual(app.query_one('#log_mode', Static).render().plain,
                             '│ Formatted  Raw')
            self.assertEqual(work.styles.border_top[0], 'round')
            self.assertEqual(panes.styles.border_top[0], 'round')
            self.assertEqual(work.styles.border_top[1].hex.lower(), '#b79cff')
            self.assertEqual(panes.styles.border_top[1].hex.lower(), '#2a303b')
            strips = app.screen._compositor.render_strips()
            border = strips[work.region.y].crop(work.region.x, work.region.x + 1)
            self.assertEqual(next(iter(border)).style.color.name, '#b79cff')
            self.assertEqual((work.region.width, panes.region.width), (46, 63))
            self.assertEqual(panes.region.x, work.region.right + 1)
            for strip in strips[work.region.y:work.region.bottom]:
                self.assertEqual(strip.crop(work.region.right, panes.region.x).text, ' ')
            for pane in (work, panes):
                self.assertEqual(strips[pane.region.y + 1].crop(
                    pane.region.x + 1, pane.region.right - 1).text.strip(), '')
                for strip in strips[pane.region.y + 1:pane.region.bottom - 1]:
                    self.assertEqual(strip.crop(pane.region.x + 1, pane.region.x + 3).text, '  ')
                    self.assertEqual(strip.crop(pane.region.right - 3, pane.region.right - 1).text, '  ')
            work_row = strips[tree.region.y + app.nodes[app.selected]._line - int(tree.scroll_y)]
            self.assertEqual(work_row.crop(work.region.x, work.region.x + 3).text, '│  ')
            self.assertIn(work_row.crop(work.region.x + 3, work.region.x + 4).text,
                          '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
            output = app.query_one(LogPane)
            log_row = strips[output.region.y]
            self.assertEqual(log_row.crop(panes.region.x, panes.region.x + 3).text, '│  ')
            self.assertEqual(output.region.x, panes.region.x + 3)
            self.assertEqual(log_row.crop(output.region.x, output.region.x + 10).text,
                             output.lines[0].text[:10])
            self.assertRegex(output.lines[0].text[1:9], r'^\d\d:\d\d:\d\d$')
            self.assertEqual(len(output.lines), 1)
            for heading, color in (('Running', '#6cb6ff'), ('Needs attention', '#ff8b7f'),
                                   ('Eligible', '#7ee2a0')):
                line = tree.render_line(app.groups[heading]._line - int(tree.scroll_y))
                self.assertTrue(any(heading in segment.text and segment.style.color.name == color
                                    for segment in line))
                self.assertIn(color, svg)
            indicator = app.query_one('#log_mode', Static)
            tabs = app.query_one('#panes Tabs', Tabs)
            self.assertEqual(tabs.region.x, panes.region.x + 3)
            self.assertEqual(tabs.region.y, panes.region.y + 2)
            self.assertEqual(indicator.region.y, tabs.region.y)
            last_tab = [tab for tab in app.query('#panes Tab') if tab.display][-1]
            self.assertEqual(indicator.region.x, last_tab.region.right)
            self.assertEqual(strips[tabs.region.y].crop(last_tab.region.right - 1, indicator.region.x + 1).text,
                             ' │')
            self.assertEqual(strips[tabs.region.y].crop(tabs.region.x, tabs.region.x + 7).text, ' 1 Log ')
            self.assertFalse(indicator.can_focus)
            self.assertEqual(len([tab for tab in app.query('#panes Tab') if tab.display]), 3)
            self.assertTrue(all(not widget.display for widget in app.query('#panes Underline')))
            mode = indicator.render()
            self.assertTrue(mode.get_style_at_offset(mode.plain.index('Formatted')).underline)
            active = app.query_one('#panes Tab.-active', Tab)
            self.assertEqual(active.styles.color, app.screen.styles.background)
            self.assertEqual(active.styles.background.hex.lower(), '#d4d9e1')
            active_strip = strips[tabs.region.y].crop(active.region.x, active.region.right)
            self.assertEqual(active_strip.text, ' 1 Log ')
            self.assertTrue(all(segment.style.bgcolor.name == '#d4d9e1' for segment in active_strip))
            selected = app.selected
            await pilot.click('#log_mode')
            self.assertEqual(app.query_one(TabbedContent).active, 'log')
            self.assertEqual(app.selected, selected)
            self.assertIsNot(app.focused, indicator)
            await pilot.press('u')
            mode = indicator.render()
            self.assertFalse(mode.get_style_at_offset(mode.plain.index('Formatted')).underline)
            self.assertTrue(mode.get_style_at_offset(mode.plain.index('Raw')).reverse)
            app.query_one(LogPane).focus()
            await pilot.pause()
            self.assertEqual(panes.styles.border_top[1].hex.lower(), '#b79cff')
            self.assertEqual(work.styles.border_top[1].hex.lower(), '#2a303b')
            for key, title in (('2', 'Issue'), ('3', 'Runs'), ('1', 'Log')):
                await pilot.press(key)
                self.assertEqual(panes.border_title, title)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_narrow_rows_split_navigation_and_last_active_tab(self):
        self.state['histories']['114']['title'] = 'Long title ' * 20
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            tree, recent = app.query_one(Tree), app.query_one(RecentActivity)
            tree.get_node_at_line(0)
            self.assertEqual(app.query_one('#work_pane').region.width, 80)
            self.assertEqual(tree.region.x, 3)
            self.assertEqual(tree.region.y, 2)
            self.assertEqual(tree.region.width, 74)
            self.assertFalse(app.query_one(ItemTabs).display)
            self.assertEqual(tree._get_label_region(app.nodes[app.selected]._line).height, 1)
            self.assertEqual(tree.virtual_size.height, 5)  # Two headings, two rows, one separator.
            line = tree.render_line(app.nodes[app.selected]._line - tree.scroll_offset.y).text
            self.assertIn('#114', line)
            self.assertIn('…', line)
            self.assertRegex(line, r'\d\d:\d\d$')
            self.assertNotIn('this launcher', line)
            self.assertEqual(len(recent.render().plain.splitlines()), 1 + len(recent.visible_rows))
            self.assertEqual(tree.region.bottom, recent.region.y)
            self.assertLessEqual(abs(tree.size.height - recent.size.height), 1)
            tree.focus()
            tree.move_cursor(app.nodes[app.selected])
            await pilot.press('enter')
            self.assertTrue(app.item_view)
            self.assertFalse(app.query_one('#work_pane').display)
            self.assertEqual(app.query_one(ItemTabs).region.width, 80)
            self.assertEqual(app.query_one(ItemTabs).region.x, 0)
            for selector in ('#panes Tabs', '#tab_rule', '#item_header', '#output', '#run_status'):
                self.assertEqual(app.query_one(selector).region.x, 3)
                self.assertEqual(app.query_one(selector).region.width, 74)
            self.assertEqual(app.query_one('#panes Tabs').region.y, 2)
            for value in ('1 Log', '2 Issue', '3 Runs', 'Formatted', 'Raw', '#114', 'no outcome reported'):
                self.assertIn(value, self.screenshot_text(app.export_screenshot()))
            await pilot.press('2', 'escape')
            self.assertFalse(app.item_view)
            self.assertEqual(tree.cursor_node.data, app.selected)
            await pilot.press('enter')
            self.assertEqual(app.query_one(TabbedContent).active, 'issue')
            await pilot.press('?', 'escape')
            self.assertTrue(app.item_view)  # Escape closes help before returning
            await pilot.press('escape')
            tree.move_cursor(app.nodes['plan:12'])
            await pilot.press('down')
            self.assertIs(app.focused, recent)
            await pilot.press('enter')
            self.assertEqual(app.selected, 'outcome:previous-run')
            self.assertTrue(app.item_view)
            await pilot.press('escape')
            self.assertIs(app.focused, recent)
            self.assertEqual(recent.cursor, app.selected)
            await pilot.press('up')
            self.assertIs(app.focused, tree)
            self.assertEqual(tree.cursor_node.data, 'plan:12')
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_resizes_preserve_item_focus_tab_and_paused_raw_log_position(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            output.focus()
            await pilot.press('f', 'u', 'home', 'pagedown')
            await self.settled(app, output)
            selected, page, anchor = app.selected, app.reading.page, output.anchor()
            for size in ((109, 32), (110, 31), (60, 16), (59, 16), (60, 15), (110, 32)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                await self.settled(app, output)
                self.assertEqual(app.selected, selected)
                self.assertIs(app.reading.page, page)
                self.assertFalse(app.reading.follow)
                self.assertTrue(app.reading.raw)
                self.assertEqual(output.anchor()[0], anchor[0])
                if not app.too_small:
                    self.assertIs(app.focused, output)
                    self.assertTrue(app.query_one(ItemTabs).display)
                    self.assertEqual(app.query_one('#work_pane').display, not app.narrow)
            await pilot.press('2')
            app.query_one('#issue VerticalScroll').focus()
            await pilot.resize_terminal(80, 24)
            self.assertTrue(app.item_view)
            self.assertEqual(app.query_one(TabbedContent).active, 'issue')
            await pilot.press('escape')
            await pilot.resize_terminal(110, 32)
            self.assertIs(app.focused, app.query_one(Tree))
            self.assertEqual(app.query_one(TabbedContent).active, 'issue')
            await pilot.resize_terminal(80, 24)
            self.assertFalse(app.item_view)
            await pilot.press('enter')
            self.assertEqual(app.query_one(TabbedContent).active, 'issue')
            await pilot.press('1', 'p', 'escape')
            self.assertTrue(app.item_view)
            await pilot.press('escape', 'q')
        app.worker.thread.join(2)

    async def test_floor_is_one_centered_line_and_restores_list_and_overlays(self):
        self.state['update'] = {'text': 'New version available'}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(60, 16)) as pilot:
            await self.ready(app, pilot)
            selected = app.selected
            for size in ((59, 16), (60, 15), (30, 10)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertTrue(app.too_small)
                for selector in ('#body', '#status', '#update'):
                    self.assertFalse(app.query_one(selector).display)
                visible = [strip.text.strip() for strip in app.screen._compositor.render_strips()
                           if strip.text.strip()]
                self.assertEqual(len(visible), 1, visible)
                self.assertTrue(visible[0].startswith('Please enlarge'))
                rows = app.screen._compositor.render_strips()
                self.assertLessEqual(abs(next(i for i, strip in enumerate(rows) if strip.text.strip())
                                         - size[1] // 2), 1)
                await pilot.press('enter', '2', 'f', '?')
                self.assertFalse(app.item_view)
                self.assertEqual(app.query_one(TabbedContent).active, 'log')
                self.assertTrue(app.reading.follow)
            await pilot.resize_terminal(60, 16)
            await pilot.pause()
            self.assertFalse(app.too_small)
            self.assertFalse(app.item_view)
            self.assertEqual(app.selected, selected)
            self.assertTrue(app.query_one(UpdateBanner).display)
            await pilot.press('?', 'escape', 'enter', '?')
            self.assertIsInstance(app.screen, KeyHelp)
            await pilot.resize_terminal(59, 16)
            await pilot.pause()
            visible = [strip.text.strip() for strip in app.screen._compositor.render_strips()
                       if strip.text.strip()]
            self.assertEqual(visible, ['Please enlarge the terminal to at least 60×16.'])
            await pilot.resize_terminal(60, 16)
            await pilot.pause()
            self.assertIsInstance(app.screen, KeyHelp)
            await pilot.press('escape')
            self.assertTrue(app.item_view)
            await pilot.resize_terminal(59, 16)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_narrow_footer_activity_item_keys_and_paused_key_shortening(self):
        now = datetime.now(timezone.utc)
        self.state['base_version'] = '9.8.7'
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=26)).isoformat()}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                footer = app.query_one('#status', Static)
                self.assertTrue(footer.render().plain.startswith('v9.8.7 · poll 26s'))
                self.assertTrue(footer.render().plain.endswith('↑↓ select ⏎ open ? keys q quit'))
                await pilot.press('enter')
                self.assertTrue(footer.render().plain.endswith('Esc back 1-3 tabs g reload ? keys q quit'))
                await pilot.press('f')
                self.assertTrue(footer.render().plain.endswith('f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'))
                await pilot.resize_terminal(60, 16)
                await pilot.pause()
                self.assertTrue(footer.render().plain.startswith('v9.8.7 · poll 26s'))
                self.assertTrue(footer.render().plain.endswith('f follow h older u raw ? keys q quit'))
                self.assertEqual(footer.render().cell_length, 60)
                await pilot.press('escape')
                for state in ('stopping', 'polling'):
                    self.state['activity'] = {'state': state}
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: f'· {state}' in footer.render().plain)
                    if state == 'stopping':
                        tree = app.query_one(Tree)
                        tree.get_node_at_line(0)
                        line = tree.render_line(app.nodes[app.selected]._line - tree.scroll_offset.y).text
                        self.assertTrue(line.startswith('■ #114'))
                        self.assertTrue(line.endswith('stopping'))
                        node = app.nodes['plan:12']
                        self.assertTrue(tree.render_line(node._line - tree.scroll_offset.y).text.endswith('held'))
                self.state.update(assignment=None, outcomes=[], latest_pass={'state': 'complete', 'rows': []})
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: app.idle_node is not None and not app.nodes)
                tree = app.query_one(Tree)
                tree.move_cursor(app.idle_node)
                self.assertEqual(tree.virtual_size.height, 2)
                self.assertIn('Idle · nothing eligible', tree.render_line(app.idle_node._line).text)
                await pilot.press('enter')
                self.assertFalse(app.item_view)
                await pilot.press('q')
        app.worker.thread.join(2)

    async def test_view_started_below_floor_restores_keyboard_focus_when_grown(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(59, 16)) as pilot:
            await self.ready(app, pilot)
            self.assertTrue(app.too_small)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertIs(app.focused, app.query_one(Tree))
            await pilot.press('enter')
            self.assertTrue(app.item_view)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_light_theme_recolors_cached_log_runs_and_all_pane_styles(self):
        self.themed_fixture()
        with self.log.open('ab') as stream:
            stream.write(record(content=[{'type': 'text', 'text': 'Closing summary\nContinued summary'}]))
        with patch.dict(os.environ):
            os.environ.pop('NO_COLOR', None)
            app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            await pilot.press('f', 'home')
            await self.settled(app, output)
            summaries = [segment for line in output.lines for segment in line if 'summary' in segment.text]
            self.assertEqual(len(summaries), 2)
            self.assertTrue(all(segment.style.italic and not segment.style.dim and
                                segment.style.color.name == '#c8cdd6' for segment in summaries))
            anchor, selected, focus = output.anchor(), app.selected, app.focused
            app.theme = 'textual-light'
            await pilot.pause()
            await self.settled(app, output)
            self.assertEqual((output.anchor(), app.selected, app.focused), (anchor, selected, focus))
            colors = app.theme_variables
            self.assertNotEqual(colors['view-assistant'].lower(), '#c8cdd6')
            summaries = [segment for line in output.lines for segment in line if 'summary' in segment.text]
            self.assertEqual(len(summaries), 2)
            self.assertTrue(all(segment.style.italic and not segment.style.dim and
                                segment.style.color == theme_style(app, 'view-assistant').color
                                for segment in summaries))
            for value, variable in (('+2', 'view-success'), ('-1', 'view-error')):
                self.assertTrue(any(value in segment.text and segment.style.color.name.lower() == colors[variable].lower()
                                    for line in output.lines for segment in line))
            banner = app.query_one(UpdateBanner)
            self.assertEqual(banner.styles.background.hex, colors['view-warning'])
            await pilot.press('3')
            svg = app.export_screenshot()
            (self.root / 'terminal-light.svg').write_text(svg)
            for color in ('#b79cff', '#1b2030', '#6cb6ff', '#ff8b7f', '#7ee2a0', '#2a303b'):
                self.assertNotIn(color, svg)
            self.assertIn(colors['view-success'].lower(), svg)
            self.assertIn(colors['view-error'].lower(), svg)
            self.assertIn('✓', self.screenshot_text(svg))
            self.assertIn('✗', self.screenshot_text(svg))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_no_color_screenshot_is_monochrome(self):
        self.themed_fixture()
        for index, row in enumerate(self.state['latest_pass']['rows']):
            row['priority'] = ('urgent', 'high', 'low')[index % 3]
        publish_snapshot(self.path, self.state)
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            self.assertTrue(app.no_color)
            svg = app.export_screenshot()
            (self.root / 'terminal-no-color.svg').write_text(svg)
            # The compositor applies Textual's monochrome filter to Rich and CSS alike.
            colors = [segment.style.color.get_truecolor() for strip in app.screen._compositor.render_strips()
                      for segment in strip if segment.style and segment.style.color]
            for color in colors:
                self.assertEqual(color.red, color.green)
                self.assertEqual(color.green, color.blue)
            self.assertIn('1 Log', self.screenshot_text(svg))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_update_banner_is_one_snapshot_row_and_preserves_focus_and_selection(self):
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            banner = app.query_one(UpdateBanner)
            self.assertFalse(banner.display)
            await pilot.press('f', '2')
            selected, focus, page = app.selected, app.focused, app.reading.page
            for value in (
                    {'text': '⬆ ub-agents 0.1.12 is available · you run 0.1.11 · '
                             'brew update && brew upgrade ub-agents, then restart the launcher',
                     'released_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()},
                    {'text': '⬆ This launcher runs code 2 commits behind origin/main · restart the launcher'},
                    {'text': '[bold]inert[/bold]\nnext\x1b[31m'}):
                self.state['update'] = value
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: banner.banner == value and banner.size.height == 1
                                 and app.query_one('#body').region.y == 1)
                self.assertTrue(banner.display)
                self.assertEqual(banner.size.height, 1)
                self.assertEqual(banner.region.y, 0)
                self.assertEqual(app.query_one('#body').region.y, 1)
                header = app.query_one('#item_header')
                self.assertGreater(header.region.y, banner.region.y)
                self.assertEqual(header.size.height, 3)
                self.assertIn('#114', header.render().plain)
                footer = app.query_one('#status', Static)
                self.assertEqual(footer.size.height, 1)
                self.assertEqual(footer.region.y, 31)
                self.assertLess(header.region.y, footer.region.y)
                self.assertTrue(footer.render().plain.endswith(
                    'f follow h older u raw PgUp/PgDn scroll ? keys q quit'))
                self.assertFalse(banner.can_focus)
                self.assertEqual(app.selected, selected)
                self.assertIs(app.focused, focus)
                self.assertEqual(app.reading.page, page)
                self.assertFalse(app.reading.follow)
                self.assertLessEqual(banner.render().cell_len, 108)
                if 'released_at' in value:
                    self.assertTrue(banner.render().plain.endswith('released 2 days ago'))
                if 'inert' in value['text']:
                    self.assertIn('[bold]inert[/bold]', banner.render().plain)
                    self.assertIn(r'\n', banner.render().plain)
                    self.assertNotIn('\x1b', banner.render().plain)
                await pilot.resize_terminal(70, 32)
                await pilot.pause()
                self.assertEqual(banner.size.height, 1)
                self.assertLessEqual(banner.render().cell_len, 68)
                await pilot.resize_terminal(170, 32)
                await pilot.pause()
                self.assertEqual(banner.size.height, 1)
                await pilot.resize_terminal(110, 32)
            self.state['update'] = None
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: not banner.display and app.query_one('#body').region.y == 0)
            self.assertEqual(app.query_one('#body').region.y, 0)
            self.assertEqual(app.selected, selected)
            self.assertIs(app.focused, focus)
            self.assertEqual(transport.calls, [])

    async def settled(self, app, pane):
        # A pilot pause can return on a busy machine before the after-refresh
        # callbacks that restore the pane's anchor. Queue behind them and wait.
        events = [asyncio.Event(), asyncio.Event()]
        app.call_after_refresh(events[0].set)
        pane.call_after_refresh(events[1].set)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), 5)

    async def test_compact_claude_styles_single_line_tools_and_hidden_anchor(self):
        # Replay recorded messages and synthetic counts, omitting only the huge
        # successful result so this page fits the bounded initial attachment.
        recorded = [line for line in FIXTURE.read_bytes().splitlines(keepends=True) if len(line) < 4000]
        edit = record(content=[{**tool(name='Edit'), 'input': {
            'file_path': 'long/' * 50, 'new_string': 'one\ntwo\n', 'old_string': 'old'}}])
        # Keep the first hidden record inside the raw render budget at 110×32.
        self.log.write_bytes(b''.join(recorded) + edit + b''.join(event(i, 20) for i in range(5)))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            joined = '\n'.join(line.text for line in output.lines)
            self.assertIn('· thinking', joined)
            self.assertIn('✗ Exit code 1', joined)
            self.assertIn('✓ run finished', joined)
            self.assertNotIn('producer=', joined)
            self.assertNotIn('thinking_tokens', joined)
            call_ref = next(ref for ref in app.reading.page.refs if '▸ Edit' in ref.value.text)
            self.assertEqual(sum(start == call_ref.start for start, _ in output.positions), 1)
            self.assertTrue(next(line.text for line, pos in zip(output.lines, output.positions)
                                 if pos[0] == call_ref.start).endswith('… +2 -1'))
            # Render a shorter call too, proving Rich styles reach terminal segments.
            call = record(content=[{**tool(name='Edit'), 'input': {
                'file_path': 'a.py', 'new_string': 'one\ntwo\n', 'old_string': 'old'}}])
            with self.log.open('ab') as stream:
                stream.write(call)
            await self.ready(app, pilot, lambda: app.reading.page.refs[-1].value.text.endswith('+2 -1'))
            with self.log.open('ab') as stream:
                stream.write(json.dumps({'type': 'error', 'message': 'x' * 300}).encode() + b'\n')
            await self.ready(app, pilot, lambda: app.reading.page.refs[-1].value.kind == 'runtime ERROR')
            self.assertTrue(all(line.cell_length <= output.render_width for line in output.lines))
            segments = [segment for line in output.lines for segment in line]
            self.assertTrue(any('+2' in segment.text and segment.style.color.name == '#7ee2a0' for segment in segments))
            self.assertTrue(any('-1' in segment.text and segment.style.color.name == '#ff8b7f' for segment in segments))
            self.assertTrue(any('✗' in segment.text and segment.style.color.name == '#ff8b7f' for segment in segments))
            self.assertTrue(any('· thinking' in segment.text and segment.style.dim for segment in segments))
            self.assertTrue(any('SPIKE111_MESSAGE_BEGIN' in segment.text and segment.style.italic and
                                not segment.style.dim and segment.style.color.name == '#c8cdd6'
                                for segment in segments))
            # In raw mode Home lands on a hidden system record. Keep its byte
            # anchor through formatted mode and resize, then recover it with u.
            await pilot.press('f', 'u')
            await self.settled(app, output)
            await pilot.press('home')
            await self.settled(app, output)
            anchor = output.anchor()
            self.assertEqual(anchor[0], app.reading.page.refs[0].start)
            self.assertEqual(app.reading.page.refs[0].value.display(), '')
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor(), anchor)
            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            await self.settled(app, output)
            self.assertEqual(output.anchor(), anchor)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], anchor[0])
            # Failed result anchors are retained on both projections too.
            failed = next(ref for ref in app.reading.page.refs if ref.value.kind == 'tool ERROR')
            app.reading.anchor = (failed.start, 0)
            output.reflow()
            await self.settled(app, output)
            await pilot.press('u')
            await self.settled(app, output)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], failed.start)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_formatted_hidden_records_do_not_change_page_accounting(self):
        leading = json.dumps({'type': 'system', 'subtype': 'init'}).encode() + b'\n'
        self.log.write_bytes(leading + b''.join(event(i, 20) for i in range(30)) +
                             record(content=[tool()]))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            trailing = (json.dumps({'type': 'system', 'subtype': 'task_notification'}).encode() + b'\n' +
                        json.dumps({'type': 'rate_limit_event'}).encode() + b'\n' +
                        record('user', [result(content='x' * 3000)]))
            with self.log.open('ab') as stream:
                stream.write(trailing)
            await self.ready(app, pilot, lambda: app.reading.page.end == self.log.stat().st_size)
            for raw in (False, True, False):
                with self.subTest(raw=raw):
                    if app.reading.raw != raw:
                        await pilot.press('u')
                    status = str(app.query_one('#status', Static).render())
                    self.assertNotIn('FOLLOW', status)
                    self.assertTrue(app.reading.follow)
                    self.assertEqual(app.log_lag(), (0, 0))
                    self.assertFalse(app.query_one('#log_state').display)
                    self.assertFalse(app.query_one('#log_note', Static).display)
                    self.assertEqual(output.visible_refs, app.reading.page.refs)
                    self.assertEqual(output.hidden, 0)
                    if not raw:
                        displayed = {start for start, _ in output.positions}
                        self.assertNotIn(app.reading.page.refs[0].start, displayed)
                        self.assertTrue(all(ref.start not in displayed for ref in app.reading.page.refs[-3:]))
                    await pilot.press('p')
                    details = app.screen.query_one('#raw_details', Static).render().plain
                    self.assertIn(f'bytes 0–{self.log.stat().st_size}', details)
                    self.assertIn(f'Rendered limit {MAX_RENDER_LINES}: 0 entries hidden', details)
                    await pilot.press('escape')
            # Leading hidden records are already on this page, not an older one.
            app.action_history()
            self.assertEqual(app.reading.notice, 'Beginning of file (byte zero).')
            self.assertIsNone(app.pending_history)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_progress_suffix_survives_truncation_and_pause_raw_and_resize(self):
        self.log.write_bytes(record(content=[{**tool(name='Edit'), 'input': {
            'file_path': 'long/' * 80, 'new_string': 'one\ntwo\n', 'old_string': 'old'}}]))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            with self.log.open('ab') as stream:
                stream.write(progress(45))
            await self.ready(app, pilot, lambda: app.reading.page.refs[0].value.text.endswith(' · 45s'))
            await self.settled(app, output)
            self.assertEqual(len(output.lines), 1)
            self.assertTrue(output.lines[0].text.endswith('… +2 -1 · 45s'))
            self.assertTrue(any('45s' in segment.text and segment.style.dim for segment in output.lines[0]))
            await pilot.press('f')
            paused = app.reading.page
            with self.log.open('ab') as stream:
                stream.write(progress(60) + progress(119) + record('user', [result()]))
            await self.ready(app, pilot, lambda: app.reading.latest.refs[0].value.text.endswith(' · 1m'))
            self.assertIs(app.reading.page, paused)
            self.assertTrue(output.lines[0].text.endswith(' · 45s'))
            await pilot.resize_terminal(120, 36)
            await self.settled(app, output)
            self.assertTrue(output.lines[0].text.endswith('… +2 -1 · 45s'))
            await pilot.press('u')
            await self.settled(app, output)
            raw_text = '\n'.join(line.text for line in output.lines)
            self.assertIn('tool_progress', raw_text)
            self.assertNotIn(' · 45s', raw_text)
            await pilot.press('u', 'f')
            await self.settled(app, output)
            self.assertTrue(output.lines[0].text.endswith('… +2 -1 · 1m'))
            self.assertEqual(len(output.lines), 1)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_time_column_and_assistant_wrapped_continuations_align(self):
        await self.assistant_wrapped_continuations('claude')

    async def test_codex_assistant_wrapped_continuations_align(self):
        await self.assistant_wrapped_continuations('codex')

    async def assistant_wrapped_continuations(self, runtime):
        self.path, self.log, self.state = fixture(self.root, runtime=runtime + ':synthetic-model:high')

        def message(text='hello café', **extra):
            if runtime == 'claude':
                return record(content=[{'type': 'text', 'text': text}], **extra)
            return (json.dumps({'type': 'item.completed', 'item': {
                'id': 'message', 'type': 'agent_message', 'text': text}, **extra}) + '\n').encode()

        self.log.write_bytes(message('exact ' + 'word ' * 30 + '\nnext',
                                    timestamp='2026-10-03T12:00:00Z') + message())
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            with self.log.open('ab') as stream:
                stream.write(message())
            await self.ready(app, pilot, lambda: len(app.reading.page.refs) == 3)
            await self.settled(app, output)
            lines = [line.text for line in output.lines]
            self.assertEqual(lines[0][0], ' ')
            self.assertEqual(lines[0][9:16], ' exact ')
            self.assertTrue(all(line.startswith(' ' * 10) for line in lines[1:-1]))
            self.assertEqual(lines[-1][0], '~')
            self.assertEqual(lines[-1][9:], ' hello café')
            self.assertIn('          next', lines)
            bodies = [segment for line in output.lines for segment in line if segment.text.strip()
                      and segment.style.italic]
            self.assertTrue(bodies)
            self.assertTrue(all(not segment.style.dim and segment.style.color.name == '#c8cdd6'
                                for segment in bodies))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_page_of_only_hidden_records_retains_raw_anchor(self):
        self.log.write_bytes(b''.join(line for line in FIXTURE.read_bytes().splitlines(keepends=True)
                                     if json.loads(line)['type'] in ('system', 'rate_limit_event')))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            self.assertEqual(output.lines, [])
            self.assertEqual(output.visible_refs, app.reading.page.refs)
            self.assertEqual(output.hidden, 0)
            await pilot.press('f', 'u')
            await self.settled(app, output)
            await pilot.press('home')
            await self.settled(app, output)
            anchor = output.anchor()
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.lines, [])
            self.assertEqual(output.anchor(), anchor)
            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            await self.settled(app, output)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], anchor[0])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_hidden_tail_uses_preceding_entry_and_scrolling_releases_anchor(self):
        hidden = json.dumps({'type': 'system', 'subtype': 'task_notification',
                             'fixture_source': 'synthetic', 'payload': 'x' * 1800}).encode() + b'\n'
        self.log.write_bytes(b''.join(event(i, 20) for i in range(40)) + hidden)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            await pilot.press('f', 'u')
            tail = app.reading.page.refs[-1].start
            app.reading.anchor = (tail, 0)
            output.reflow()
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], tail)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], tail)
            self.assertEqual(output.visible_refs[-1].start, tail)
            self.assertEqual(output.positions[-1][0], app.reading.page.refs[-2].start)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], tail)
            await pilot.press('u')
            await self.settled(app, output)
            output.action_scroll_up()
            await self.settled(app, output)
            moved = output.anchor()[0]
            self.assertNotEqual(moved, tail)
            await pilot.press('u')
            await self.settled(app, output)
            self.assertEqual(output.anchor()[0], moved)
            await pilot.press('q')
        app.worker.thread.join(2)

    def description_source(self, source, title, body):
        if source == 'snapshot':
            row = self.state['latest_pass']['rows'][0]
            row['title'] = title
            row['description'] = {'available': True, 'text': body}
            publish_snapshot(self.path, self.state)
        elif source == 'run context.json':
            (self.log.parent / 'context.json').write_text(json.dumps({'title': title, 'body': body}))
        else:
            (self.log.parent / 'context.json').unlink()
        transport = RecordingDescriptionTransport()
        transport.response = parse_response(reply(title, body), b'', 0, 1000)
        return transport

    async def test_checkout_setup_activity_names_triggering_file(self):
        self.state['activity'] = {'state': 'checkout setup running: pnpm-lock.yaml changed'}
        self.state['assignment'] = None
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(160, 32)) as pilot:
            await self.ready(app, pilot, lambda: 'checkout setup running' in
                             app.query_one('#status', Static).render().plain)
            self.assertIn('checkout setup running: pnpm-lock.yaml changed',
                          app.query_one('#status', Static).render().plain)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_one_line_footer_version_activity_and_session_diagnostics(self):
        now = datetime.now(timezone.utc)
        self.state['base_version'] = '9.8.7'
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=30)).isoformat()}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                footer = app.query_one('#status', Static)
                keys = '↑↓ select ⏎ open 1-3 tabs g reload ? keys q quit'
                self.assertIn('ub-agents v9.8.7 · next poll 30s', footer.render().plain)
                self.assertTrue(footer.render().plain.endswith(keys))
                self.assertEqual(footer.size.height, 1)
                self.assertEqual(footer.render().cell_length, 110)
                for removed in ('snapshot', 'Local files', 'GitHub', 'FOLLOW', 'FORMATTED', 'unread', 'lag'):
                    self.assertNotIn(removed, footer.render().plain)
                clock.now.return_value = now + timedelta(seconds=4)
                app.update_status()
                self.assertIn('next poll 26s', footer.render().plain)
                clock.now.return_value = now + timedelta(seconds=40)
                app.update_status()
                self.assertIn('next poll 0s', footer.render().plain)
                for state in ('polling', 'running assignment', 'stopping'):
                    self.state['activity'] = {'state': state}
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: f'· {state}' in footer.render().plain)
                    self.assertTrue(footer.render().plain.endswith(keys))
                self.state['published_at'] = (now - timedelta(seconds=60)).isoformat()
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: 'stale' in footer.render().plain)
                self.assertNotIn('snapshot', footer.render().plain)
                self.state['ended'] = True
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: 'ended' in footer.render().plain)
                self.path.write_text('{broken')
                await self.ready(app, pilot, lambda: 'malformed:' in footer.render().plain)
                self.assertIn(app.session.error[:20], footer.render().plain)
                await pilot.resize_terminal(80, 24)
                app.update_status()
                self.assertNotIn('minimum', footer.render().plain)
                self.assertTrue(footer.render().plain.startswith('malformed:'))
                self.assertTrue(footer.render().plain.endswith('↑↓ select ⏎ open ? keys q quit'))
                await pilot.press('q')
        app.worker.thread.join(2)

    async def test_log_pill_only_when_paused_or_behind_and_contextual_footer_keys(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            pill = app.query_one('#log_state', Static)
            footer = app.query_one('#status', Static)
            note = app.query_one('#log_note', Static)
            self.assertFalse(pill.display)
            self.assertFalse(note.display)
            await pilot.press('f')
            self.assertTrue(pill.display)
            self.assertIn('⏸ PAUSED', pill.render().plain)
            self.assertIn('f follow', pill.render().plain)
            self.assertNotIn('0 new', pill.render().plain)
            self.assertNotIn('0B lag', pill.render().plain)
            self.assertTrue(footer.render().plain.endswith('f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'))
            self.assertEqual(footer.render().cell_length, 110)
            await pilot.press('u')
            self.assertIn('RAW', pill.render().plain)
            with self.log.open('ab') as stream:
                stream.write(event(9000))
            await self.ready(app, pilot, lambda: app.log_lag()[0] > 0)
            unread, lag = app.log_lag()
            self.assertIn(f'{unread} new ↓', pill.render().plain)
            self.assertIn(f'{lag}B lag', pill.render().plain)
            self.assertEqual(pill.region.bottom, app.query_one('#run_status').region.y)
            self.assertLessEqual(app.query_one('#run_status').region.bottom,
                                 footer.region.y)
            self.assertEqual(pill.size.height, 1)
            self.assertLessEqual(pill.region.bottom, footer.region.y)
            self.assertNotIn('PAUSED', app.query_one('#run_status', Static).render().plain)
            for tab in ('2', '3'):
                await pilot.press(tab)
                self.assertIn('f follow h older', footer.render().plain)
                self.assertNotIn('g reload', footer.render().plain)
                self.assertNotIn('PAUSED', footer.render().plain)
            await pilot.press('1', 'f')
            await self.ready(app, pilot, lambda: not pill.display)
            self.assertTrue(app.reading.raw)
            self.assertTrue(footer.render().plain.endswith('↑↓ select ⏎ open 1-3 tabs g reload ? keys q quit'))
            # Ingestion can lag while following; it must be visible without a
            # persistent FOLLOW or RAW badge when caught up.
            app.reading.log = replace(app.reading.log, unread_bytes=8192)
            app.update_status()
            self.assertTrue(pill.display)
            self.assertIn('↓ BEHIND', pill.render().plain)
            self.assertIn('8192B lag', pill.render().plain)
            self.assertIn('RAW', pill.render().plain)
            self.assertNotIn('BEHIND', app.query_one('#run_status', Static).render().plain)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_height_changes_preserve_paused_position_with_a_pending_anchor_save(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f', 'home', 'pagedown')
            await pilot.pause()
            output = app.query_one(LogPane)
            page, lines = app.reading.page, tuple(output.lines)
            previous = app.reading.anchor
            output.scroll_to(y=output.scroll_y + 5, animate=False, immediate=True)
            anchor = output.anchor()
            self.assertNotEqual(anchor, previous)
            # Deliver a height resize before the scroll's deferred anchor save,
            # reproducing the f + Page Up callback order without timing a PTY.
            app.reading.notice = 'Unfinished: 28B (raw preview)'
            app.update_status()
            callbacks = []
            with patch.object(output, 'call_after_refresh',
                              side_effect=lambda callback, *args: callbacks.append((callback, args))):
                output.on_resize()
                if callbacks:
                    callback, args = callbacks.pop(0)
                    callback(*args)
                output.save_anchor()
                for callback, args in callbacks:
                    callback(*args)
            self.assertEqual(output.anchor(), anchor)
            self.assertEqual(app.reading.anchor, anchor)
            await pilot.pause()
            self.assertEqual(output.anchor(), anchor)
            self.assertEqual(app.reading.anchor, anchor)
            with patch.object(output, 'reflow', wraps=output.reflow) as reflow:
                height = output.size.height
                app.reading.notice = ''
                app.update_status()
                await pilot.pause()
                self.assertGreater(output.size.height, height)
                self.assertEqual(output.anchor(), anchor)
                self.assertEqual(app.reading.anchor, anchor)
                pill = app.query_one('#log_state', Static)
                # Isolate the pill's layout changes from resuming follow.
                with patch.object(app, 'update_status'):
                    for visible in (False, True, False, True):
                        pill.display = visible
                        await pilot.pause()
                        self.assertEqual(output.anchor(), anchor)
                        self.assertEqual(app.reading.anchor, anchor)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(tuple(output.lines), lines)
                reflow.assert_not_called()
            # A width resize must still rewrap, using the newer position even
            # when the preceding scroll has not saved its anchor yet.
            output.scroll_to(y=output.scroll_y + 5, animate=False, immediate=True)
            moved = output.anchor()
            self.assertNotEqual(moved, app.reading.anchor)
            width = output.render_width
            await pilot.resize_terminal(130, 32)
            await pilot.pause()
            self.assertNotEqual(output.render_width, width)
            self.assertEqual(output.anchor()[0], moved[0])
            self.assertAlmostEqual(output.anchor()[1], moved[1], delta=0.05)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_raw_access_has_byte_retention_and_render_diagnostics(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            note = app.query_one('#log_note', Static)
            for diagnostic in ('bytes', 'evicted', 'skipped', 'shortened', 'Rendered limit'):
                self.assertNotIn(diagnostic, note.render().plain)
            output = app.query_one(LogPane)
            await pilot.press('p')
            self.assertIsInstance(app.screen, RawAccess)
            raw = app.screen.query_one('#raw_details', Static).render().plain
            self.assertIn(str(self.log), raw)
            self.assertIn(f'Displayed bytes {output.visible_refs[0].start}–{output.visible_refs[-1].end}', raw)
            self.assertIn(f'Page bytes {app.reading.page.start}–{app.reading.page.end}', raw)
            self.assertIn(f'evicted {app.reading.log.evicted_entries}', raw)
            self.assertIn(f'skipped {app.reading.log.skipped_bytes}B', raw)
            self.assertIn(f'shortened {app.reading.log.shortened_entries}', raw)
            self.assertIn(f'Rendered limit {MAX_RENDER_LINES}: {output.hidden} entries hidden', raw)
            await pilot.press('escape', 'q')
        app.worker.thread.join(2)

    async def test_height_only_layout_changes_preserve_a_pending_paused_scroll(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            output = app.query_one(LogPane)
            for key in ('f', 'home', 'pagedown'):
                await pilot.press(key)
                await self.settled(app, output)
            note = app.query_one('#log_note', Static)
            pill = app.query_one('#log_state', Static)
            page = app.reading.page
            # Hold status updates while changing the actual notice/pill layout.
            # Move the screen before saving, deterministically representing a
            # Page Up whose save_anchor callback is pending when Resize arrives.
            with patch.object(app, 'update_status'):
                for widget, visible in ((note, True), (note, False), (pill, False), (pill, True)):
                    with self.subTest(widget=widget.id, visible=visible):
                        saved = app.reading.anchor
                        output.scroll_to(y=int(output.scroll_y) + 2, animate=False, immediate=True)
                        anchor = output.anchor()
                        self.assertNotEqual(anchor, saved)
                        width, height = output.size
                        note.update('Unfinished: 12B (raw preview)')
                        widget.display = visible
                        await pilot.pause()
                        await self.settled(app, output)
                        self.assertEqual(output.size.width, width)
                        self.assertNotEqual(output.size.height, height)
                        # Deliver a resize before the pending scroll is saved,
                        # then drain its refresh callbacks in that exact order.
                        callbacks = []
                        with patch.object(output, 'call_after_refresh', side_effect=lambda callback, *args:
                                          callbacks.append((callback, args))):
                            output.on_resize()
                            if callbacks:
                                callback, args = callbacks.pop(0)
                                callback(*args)
                            output.save_anchor()
                            for callback, args in callbacks:
                                callback(*args)
                        self.assertEqual(output.anchor(), anchor)
                        self.assertEqual(app.reading.anchor, anchor)
                        self.assertIs(app.reading.page, page)
                    output.save_anchor()
                for height in (36, 32):
                    anchor = output.anchor()
                    await pilot.resize_terminal(110, height)
                    await pilot.pause()
                    self.assertEqual(output.anchor(), anchor)
                    self.assertEqual(app.reading.anchor, anchor)
            await pilot.press('f')
            with patch.object(app, 'update_status'):
                for visible in (True, False):
                    note.display = visible
                    await pilot.pause()
                    self.assertTrue(app.reading.follow)
                    self.assertEqual(output.scroll_y, output.max_scroll_y)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_help_lists_all_keys_closes_with_question_or_escape_and_preserves_reading(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f', 'home', 'pagedown')
            selected, page, anchor = app.selected, app.reading.page, app.query_one(LogPane).anchor()
            for close in ('?', 'escape'):
                await pilot.press('?')
                self.assertIsInstance(app.screen, KeyHelp)
                help_text = app.screen.query_one('#raw_details', Static).render().plain
                for key in ('Tab', 'arrows', 'Enter', '1 / 2 / 3', 'g on Issue', 'f   ', 'h   ',
                            'u   ', 'p   ', 'Page Up', 'Page Down', 'Home', 'End', '?', 'Escape', 'q   ', 'Ctrl-C'):
                    self.assertIn(key, help_text)
                self.assertIn('q   Stop after run; close a standalone view', help_text)
                self.assertIn('Ctrl-C   Stop now; close a standalone view', help_text)
                self.assertIn('Mouse drag   Copy selected text on release (OSC 52)', help_text)
                self.assertIn('y   Copy the current selection again', help_text)
                self.assertIn('Applications in terminal may access clipboard', help_text)
                self.assertIn("Option-drag selects with the terminal's own selection", help_text)
                await pilot.press('f', 'h', 'u', 'g', 'p', '2', 'pageup', 'pagedown', 'home', 'end')
                self.assertIsInstance(app.screen, KeyHelp)
                await pilot.press(close)
                self.assertNotIsInstance(app.screen, RawAccess)
                self.assertEqual(app.selected, selected)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(app.query_one(LogPane).anchor(), anchor)
                self.assertFalse(app.reading.follow)
                self.assertFalse(app.reading.raw)
            await pilot.press('?', 'q')
        app.worker.thread.join(2)

    async def test_only_applicable_log_notices_remain_visible(self):
        self.path, self.log, self.state = fixture(self.root, runtime='command:synthetic-model:high')
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            note = app.query_one('#log_note', Static)
            self.assertEqual(note.render().plain, 'command: plain/raw fallback')
            self.log.write_bytes(b'unfinished record')
            await self.ready(app, pilot, lambda: 'Unfinished:' in note.render().plain)
            self.assertTrue(note.render().plain.endswith('command: plain/raw fallb…'))
            with self.log.open('ab') as stream:
                stream.write(b'\n')
            await self.ready(app, pilot, lambda: 'Unfinished:' not in note.render().plain)
            self.log.unlink()
            await self.ready(app, pilot, lambda: 'Read error:' in note.render().plain)
            self.assertNotIn('Runtime output is not', note.render().plain)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_shared_header_stays_below_tabs_including_a_later_tab(self):
        self.state['assignment'].update(kind='pr', attempt=2)
        self.state['outcomes'].append({'item': 114, 'run': 'prior', 'handoff': 1235})
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(130, 36)) as pilot:
            await self.ready(app, pilot)
            header = app.query_one('#item_header', Static)
            tabs = app.query_one(TabbedContent)
            await tabs.add_pane(TabPane('Later', Static('Later content'), id='later'))
            for tab in ('log', 'issue', 'runs', 'later'):
                tabs.active = tab
                await pilot.pause()
                value = header.render()
                title, metadata, rule = value.plain.split('\n')
                self.assertEqual(title, '⌥114 Cached title')
                self.assertEqual(metadata, 'implementer · claude synthetic-model high · attempt 2 · ⌥1235')
                self.assertEqual(rule, '┄' * header.content_region.width)
                self.assertTrue(value.get_style_at_offset(0).bold)
                accent = app.theme_variables['view-accent'].lower()
                self.assertEqual(value.get_style_at_offset(0).foreground.hex.lower(), accent)
                self.assertNotEqual(value.get_style_at_offset(1).foreground,
                                    value.get_style_at_offset(0).foreground)
                style = value.get_style_at_offset(len(title) + 1)
                self.assertTrue(style.dim)
                self.assertFalse(style.bold)
                linked = value.plain.index('⌥1235')
                self.assertEqual(value.get_style_at_offset(linked).foreground.hex.lower(), accent)
                self.assertTrue(value.get_style_at_offset(linked).dim)
                self.assertEqual(value.get_style_at_offset(linked + 1), style)
                self.assertTrue(header.display)
                self.assertEqual(header.region.height, 3)
                self.assertLess(header.region.bottom, app.query_one('#' + tab).region.bottom)
                self.assertLessEqual(app.query_one('#' + tab).region.bottom,
                                     app.query_one('#status').region.y)
            issue = app.query_one('#issue_text', Static).render().plain
            self.assertNotIn('Cached title', issue)
            self.assertNotIn('#114', issue)
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_header_reference_clicks_follow_selection_on_every_tab_and_layout(self):
        self.state['repository'] = 'synthetic-owner/consumer-project'
        self.state['assignment'] = None
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': number, 'kind': kind, 'agent': 'worker', 'state': 'blocked',
             'reason': 'Needs a decision', 'title': 'Long title ' + '界' * 200}
            for number, kind in ((114, 'issue'), (12, 'pr'))]}
        self.state['coordination_authors'] = {'operator': {'trusted': True}}
        self.state['action_needed'] = {
            str(number): {'text': ACTION_MARKER + 'run -->\n**Action needed**\n\nMaintainer: decide.',
                          'author': 'operator'} for number in (114, 12)}
        self.state['outcomes'].append({'item': 114, 'run': 'handoff', 'handoff': 1235})
        publish_snapshot(self.path, self.state)
        for size, theme in (((130, 36), 'ub-agents'), ((60, 16), 'ub-agents'),
                            ((130, 36), 'textual-light')):
            with self.subTest(size=size, theme=theme):
                transport = RecordingDescriptionTransport()
                with patch.dict(os.environ):
                    os.environ.pop('NO_COLOR', None)
                    app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                app.theme = theme
                with patch.object(app, 'open_url') as opened:
                    async with app.run_test(size=size) as pilot:
                        await self.ready(app, pilot, lambda: app.local_description is not None)
                        link_color = app.theme_variables['view-link'].lower()
                        self.assertEqual(link_color, '#6cb6ff' if theme == 'ub-agents'
                                         else app.theme_variables['text-primary'].lower())
                        if theme == 'textual-light':
                            self.assertNotEqual(link_color, '#6cb6ff')
                        if app.narrow:
                            await pilot.press('enter')
                            self.assertTrue(app.item_view)
                        header = app.query_one('#item_header', Static)
                        for number, marker, path in ((114, '#', 'issues'), (12, '⌥', 'pull')):
                            app.select(f'plan:{number}:worker')
                            await self.ready(app, pilot, lambda: app.local_description is not None)
                            for tab in ('log', 'issue', 'runs', 'unblock'):
                                with self.subTest(number=number, tab=tab):
                                    app.query_one(ItemTabs).active = tab
                                    await pilot.pause()
                                    value = header.render()
                                    title, metadata, rule = value.plain.split('\n')
                                    reference = f'{marker}{number}'
                                    self.assertTrue(title.startswith(reference + ' '))
                                    self.assertTrue(title.endswith('…'))
                                    self.assertEqual(rule, '┄' * header.content_region.width)
                                    self.assertEqual(header.size.height, 3)
                                    self.assertTrue(value.get_style_at_offset(len(title) + 1).dim)
                                    # Textual applies link and hover styles after render().
                                    # Inspect the final strips, with the pointer off the reference.
                                    self.assertTrue(await pilot.hover(header, offset=(0, 2)))
                                    await pilot.pause()
                                    lines = header.render_lines(header.size.region)
                                    styles = [next(iter(lines[0].crop(x, x + 1))).style
                                              for x in range(len(reference))]
                                    title_style = next(iter(lines[0].crop(
                                        len(reference) + 1, len(reference) + 2))).style
                                    self.assertTrue(title_style.bold)
                                    for segment in lines[0].crop(len(reference)):
                                        self.assertFalse(segment.style.underline)
                                        self.assertNotIn('@click', segment.style.meta)
                                    for x, style in enumerate(styles):
                                        self.assertTrue(style.bold)
                                        self.assertTrue(style.underline)
                                        self.assertFalse(style.reverse)
                                        self.assertEqual(style.meta['@click'], 'app.open_reference')
                                        self.assertEqual(style.bgcolor, title_style.bgcolor)
                                        color = (app.theme_variables['view-accent'].lower()
                                                 if marker == '⌥' and x == 0 else title_style.color.name)
                                        self.assertEqual(style.color.name, color)
                                    for x in range(len(reference)):
                                        self.assertTrue(await pilot.hover(header, offset=(x, 0)))
                                        await pilot.pause()
                                        hovered = header.render_lines(header.size.region)[0]
                                        self.assertEqual([next(iter(hovered.crop(i, i + 1))).style
                                                          for i in range(len(reference))],
                                                         [style + theme_style(app, 'view-link') for style in styles])
                                        self.assertEqual(list(hovered.crop(len(reference))),
                                                         list(lines[0].crop(len(reference))))
                                        opened.reset_mock()
                                        self.assertTrue(await pilot.click(header, offset=(x, 0)))
                                        opened.assert_called_once_with(
                                            f'https://github.com/synthetic-owner/consumer-project/{path}/{number}')
                                    opened.reset_mock()
                                    for offset in ((len(reference), 0), (len(reference) + 1, 0),
                                                   (header.size.width - 1, 0), (0, 1),
                                                   (max(0, len(metadata) - 1), 1),
                                                   (header.size.width - 1, 1), (0, 2)):
                                        self.assertTrue(await pilot.click(header, offset=offset))
                                    opened.assert_not_called()
                        self.assertEqual(transport.calls, [])
                        await pilot.press('q')
                app.worker.thread.join(2)
                self.assertFalse(app.worker.thread.is_alive())

    async def test_header_reference_is_inert_without_valid_session_identity(self):
        app = View(self.root, self.path)
        with patch.object(app, 'open_url') as opened:
            async with app.run_test(size=(130, 36)) as pilot:
                await self.ready(app, pilot)
                header = app.query_one('#item_header', Static)
                row = app.rows[app.selected]
                repository = app.session.data['repository']
                # Mutate the already-loaded snapshot to exercise partial identities
                # without a disk refresh replacing them during the clicks.
                app.worker.close()
                app.worker.thread.join(2)
                app.busy = True
                while not app.worker.results.empty():
                    app.worker.results.get_nowait()

                async def assert_inert_header():
                    await pilot.pause()
                    for line in header.render_lines(header.size.region):
                        for segment in line:
                            self.assertFalse(segment.style.underline)
                            self.assertNotIn('@click', segment.style.meta)

                for kind, marker in (('issue', '#'), ('pr', '⌥')):
                    app.rows[app.selected] = replace(row, data={**row.data, 'kind': kind})
                    for invalid in (None, '', 42, 'owner', 'owner/repo/extra', 'owner/..',
                                    'owner/repo?query', 'owner/repo\x1b'):
                        with self.subTest(kind=kind, repository=invalid):
                            app.session.data['repository'] = invalid
                            app.update_status()
                            self.assertTrue(header.render().plain.startswith(marker + '114'))
                            await pilot.click(header, offset=(0, 0))
                            await assert_inert_header()
                            opened.assert_not_called()
                app.session.data['repository'] = repository
                for invalid in (None, '?', '114', 0, -1, True, 1.5):
                    with self.subTest(number=invalid):
                        app.rows[app.selected] = replace(row, item=invalid)
                        app.update_status()
                        self.assertNotIn('@click', header.render().get_style_at_offset(0).meta)
                        await pilot.click(header, offset=(0, 0))
                        await assert_inert_header()
                        opened.assert_not_called()
                app.rows[app.selected] = row
                app.select(None)
                self.assertEqual(header.render().plain.split('\n')[0], 'No item selected.')
                await pilot.click(header, offset=(0, 0))
                await assert_inert_header()
                opened.assert_not_called()
                # Restore the loaded session before yielding to the renderer.
                with patch.object(app, 'session', None):
                    app.action_open_reference()
                    opened.assert_not_called()
                await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_log_status_follows_process_and_report_with_right_aligned_history(self):
        self.state['outcomes'].extend([
            {'item': 114, 'run': 'before', 'agent': 'preparer'},
            {'item': 114, 'run': 'other', 'agent': 'implementer'}])
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            status = app.query_one('#run_status', Static)
            lines = status.render().plain.split('\n')
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], '┄' * status.size.width)
            self.assertIn('implementer running · no outcome report…', lines[1])
            self.assertIn(lines[1][0], '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
            self.assertTrue(lines[1].endswith('2 earlier runs'))
            self.assertEqual(pane_line(lines[1], 1000).cell_len, status.size.width)
            self.assertEqual(status.region.bottom, app.query_one('#log').region.bottom)
            self.assertEqual(status.region.bottom + 1, app.query_one('#status').region.y)
            self.assertFalse(app.query_one('#log_note').display)
            self.state['assignment']['process'] = 'exited'
            self.state['outcomes'].append({'item': 114, 'run': 'owned-run', 'result': 'success',
                                           'acceptance': 'finalized'})
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: 'reported success' in status.render().plain)
            self.assertIn('implementer exited · reported success', status.render().plain)
            self.assertIn('…', status.render().plain)
            self.assertTrue(status.render().plain.endswith('2 earlier runs'))
            await pilot.resize_terminal(150, 32)
            await self.ready(app, pilot, lambda: 'finalized' in status.render().plain)
            self.assertIn('implementer exited · reported success (finalized)', status.render().plain)
            self.assertTrue(status.render().plain.endswith('2 earlier runs'))
            await pilot.resize_terminal(65, 25)
            await pilot.press('enter')
            await pilot.pause()
            for widget in (status, app.query_one('#item_header', Static)):
                self.assertTrue(all(pane_line(line, 1000).cell_len <= widget.content_region.width
                                    for line in widget.render().plain.split('\n')))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_stopping_screen_and_non_stopping_states_at_minimum_size(self):
        self.state['assignment']['attempt'] = 3
        self.state['latest_pass']['rows'] = [
            {'item': 20, 'agent': 'worker', 'state': 'ready'},
            {'item': 21, 'agent': 'worker', 'state': 'ready'},
            {'item': 22, 'agent': 'worker', 'state': 'recover'},
            {'item': 23, 'agent': 'worker', 'state': 'backoff'},
            {'item': 24, 'agent': 'worker', 'state': 'waiting'},
        ]
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one('#work', Tree)
            status = app.query_one('#run_status', Static)
            own = 'assignment:owned-run'
            def line(key, detail=False):
                tree.get_node_at_line(0)
                return tree.render_line(app.nodes[key]._line + int(detail) - tree.scroll_offset.y).text.rstrip()
            def normal_work():
                self.assertEqual(app.groups['Eligible'].label.plain, 'Eligible · 5')
                self.assertIn(line(own)[0], '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
                self.assertEqual(line(own, True), pane_line('  implementer · this launcher · attempt 3',
                                                          tree.scrollable_content_region.width).plain)
                for item, state in ((20, 'next'), (21, 'ready'), (22, 'recover'),
                                    (23, 'backoff'), (24, 'waiting')):
                    self.assertTrue(line(f'plan:{item}').endswith(state))
                self.assertIn('implementer running · no outcome reported', status.render().plain)
            normal_work()
            self.state['activity'] = {'state': 'stopping'}
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: 'Stopping after this run' in status.render().plain)
            self.assertEqual(app.selected, own)
            self.assertTrue(line(own).startswith('■ #114'))
            self.assertTrue(line(own).endswith('stopping'))
            expected_detail = pane_line('  implementer · this launcher · finishing run',
                                        tree.scrollable_content_region.width).plain
            self.assertEqual(line(own, True), expected_detail)
            self.assertNotIn('attempt', line(own, True))
            label = 'Eligible · 5 · not claimed while stopping'
            self.assertEqual(app.groups['Eligible'].label.plain, label)
            tree.get_node_at_line(0)
            heading = tree.render_line(app.groups['Eligible']._line - tree.scroll_offset.y).text
            self.assertEqual(heading, pane_line(label, tree.scrollable_content_region.width).plain)
            for item in (20, 21, 22):
                self.assertTrue(line(f'plan:{item}').endswith('held'))
            for item, state in ((23, 'backoff'), (24, 'waiting')):
                self.assertTrue(line(f'plan:{item}').endswith(state))
            self.assertEqual(status.render().plain.splitlines()[1],
                             '■ Stopping after this run (SIGTERM) · no new claims')
            self.assertIn('· stopping', app.query_one('#status', Static).render().plain)
            for key, expected in (('plan:20', 'worker ready · no outcome reported'),
                                  ('outcome:previous-run', 'preparer exited · reported prepared (finalized)')):
                app.select(key)
                await self.ready(app, pilot, lambda: expected in status.render().plain)
                self.assertNotIn('Stopping after this run', status.render().plain)
            app.select(own)
            await self.ready(app, pilot, lambda: 'Stopping after this run' in status.render().plain)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            tree.get_node_at_line(0)
            heading = tree.render_line(app.groups['Eligible']._line - tree.scroll_offset.y).text
            self.assertTrue(heading.startswith(label))
            self.assertEqual(pane_line(heading, 1000).cell_len, tree.scrollable_content_region.width)
            await pilot.resize_terminal(110, 32)
            for state in ('polling', 'waiting', 'running assignment', 'ended', 'stale'):
                with self.subTest(state=state):
                    self.state['ended'] = state == 'ended'
                    self.state['published_at'] = (datetime.now(timezone.utc) -
                                                   timedelta(seconds=60 if state == 'stale' else 0)).isoformat()
                    self.state['activity'] = {'state': state if state not in {'ended', 'stale'}
                                              else 'running assignment'}
                    if state == 'waiting':
                        self.state['activity']['until'] = (datetime.now(timezone.utc) +
                                                          timedelta(seconds=30)).isoformat()
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: app.session.data == self.state)
                    normal_work()
                    footer = app.query_one('#status', Static).render().plain
                    self.assertNotIn('stopping', footer)
                    self.assertIn('next poll' if state == 'waiting' else state, footer)
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_notice_is_one_highlighted_line_only_for_exceptional_log_states(self):
        self.path, self.log, self.state = fixture(self.root, runtime='command:model:high')
        self.log.write_bytes(b'unfinished raw fragment')
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            note = app.query_one('#log_note', Static)
            self.assertEqual(note.size.height, 1)
            self.assertIn('Unfinished:', note.render().plain)
            self.assertTrue(note.render().plain.endswith('command: plain/raw fallb…'))
            self.assertTrue(any(span.style.bold and span.style.foreground.hex.lower() == app.theme_variables['view-warning']
                                for span in note.render().spans))
            self.assertNotIn('bytes ', note.render().plain)
            self.log.write_bytes(b'finished\n')
            await self.ready(app, pilot, lambda: app.reading.page.generation > 0)
            self.assertIn('File replaced or changed generation', note.render().plain)
            self.log.unlink()
            await self.ready(app, pilot, lambda: app.reading.log.error)
            self.assertIn('Read error:', note.render().plain)
            self.assertNotIn('\n', note.render().plain)
            self.assertLessEqual(note.render().cell_length, note.size.width)
            self.assertTrue(note.render().plain.endswith('…'))
            await pilot.press('q')
        app.worker.thread.join(2)

    def test_one_line_clipping_uses_terminal_cells_and_escapes_controls(self):
        self.assertEqual(pane_line('界' * 10, 7).plain, '界界界…')
        self.assertEqual(pane_line('one\ntwo\x1b', 40).plain, r'one\ntwo\x1b')

    async def test_markdown_body_from_every_source_is_formatted_and_inert(self):
        title = '[bold]Title[/bold]\r\nnext\ttitle\rlast\x1b[31m'
        body = ('# Overview\r\n\r\nfirst\tline\rsecond\nthird\n\n'
                '- **strong** and *emphasis* with `code`\n'
                '- [bold]literal[/bold] \x1b[31mred\n\n'
                '1. Ordered\n\n```text\ncode\tline\nnext line\n```\n\n'
                '[web](https://example.invalid) <https://example.invalid>\n'
                '[local](file:///missing) [anchor](#overview)\n'
                '![picture](https://example.invalid/pic.png)\n'
                '<b>HTML</b> <img src="https://example.invalid/pic.png">\n'
                '&#27; &#x9b; [ref][target]\n[target]: https://example.invalid')
        expected = body.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', r'\x1b')
        for source in ('snapshot', 'run context.json', 'GitHub'):
            with self.subTest(source=source):
                self.path, self.log, self.state = fixture(self.root)
                transport = self.description_source(source, title, body)
                app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                with patch.object(app, 'open_url', side_effect=AssertionError('opened link')), \
                     patch.object(Markdown, 'load', side_effect=AssertionError('loaded link')), \
                     patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
                    async with app.run_test(size=(110, 32)) as pilot:
                        await self.ready(app, pilot)
                        await pilot.press('2', 'g')
                        await self.ready(app, pilot, lambda: f'Source: {source}' in (app.last_context or ''))
                        markdown = app.query_one('#issue_body', Markdown)
                        await self.ready(app, pilot, lambda: len(markdown.query('MarkdownBullet')) == 3)
                        self.assertEqual(markdown.source, expected)
                        self.assertEqual(len(markdown.query('MarkdownH1')), 1)
                        self.assertEqual(len(markdown.query_one('MarkdownBulletList').query('MarkdownBullet')), 2)
                        self.assertEqual(len(markdown.query_one('MarkdownOrderedList').query('MarkdownBullet')), 1)
                        content = [block.render() for block in markdown.query('MarkdownParagraph')]
                        rendered = '\n'.join(part.plain for part in content)
                        self.assertIn('\nsecond\nthird', rendered)
                        self.assertIn('[bold]literal[/bold]', rendered)
                        self.assertIn(r'\x1b[31mred', rendered)
                        self.assertNotIn('\x1b', rendered)
                        for literal in ('[web](https://example.invalid)', '<https://example.invalid>',
                                        '[local](file:///missing)', '[anchor](#overview)',
                                        '![picture](https://example.invalid/pic.png)', '<b>HTML</b>',
                                        '<img src="https://example.invalid/pic.png">',
                                        '&#27; &#x9b;', '[ref][target]', '[target]: https://example.invalid'):
                            self.assertIn(literal, rendered)
                        spans = [span for part in content for span in part.spans]
                        for style in ('.strong', '.em', '.code_inline'):
                            self.assertTrue(any(span.style == style for span in spans))
                        self.assertTrue(all(isinstance(span.style, str) or not span.style.meta for span in spans))
                        header = app.query_one('#issue_text', Static).render()
                        self.assertNotIn('Title', header.plain)
                        self.assertFalse(header.spans)
                        shared = app.query_one('#item_header', Static).render()
                        self.assertIn(r'[bold]Title[/bold]\nnext\ttitle\nlast\x1b[31m', shared.plain)
                        self.assertIn(f'Source: {source}', app.query_one('#issue_note', Static).render().plain)
                        self.assertNotIn('Source:', markdown.source)
                        # Neither mouse nor keyboard activation has a link target.
                        await pilot.click(markdown.query_one('MarkdownParagraph'), offset=(2, 0))
                        await pilot.press('tab', 'enter', 'space')
                        markdown.post_message(Markdown.LinkClicked(markdown, 'https://example.invalid'))
                        await pilot.pause()
                        self.assertEqual(len(transport.calls), 1 if source == 'GitHub' else 0)
                        await pilot.press('q')
                app.worker.thread.join(2)

    async def test_shortened_code_fence_keeps_plain_notice_for_every_source(self):
        body = '# Start\n\n```text\n' + 'x' * 3000 + '\n```\nEnd'
        for source in ('snapshot', 'run context.json', 'GitHub'):
            with self.subTest(source=source):
                self.path, self.log, self.state = fixture(self.root)
                transport = self.description_source(source, 'Title', body)
                app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                async with app.run_test(size=(110, 32)) as pilot:
                    await self.ready(app, pilot)
                    await pilot.press('2', 'g')
                    await self.ready(app, pilot, lambda: f'Source: {source}' in (app.last_context or ''))
                    markdown = app.query_one('#issue_body', Markdown)
                    await self.ready(app, pilot, lambda: len(markdown.query('MarkdownFence')) == 1)
                    self.assertEqual(markdown.source, body[:2048])
                    self.assertNotIn('shortened', markdown.query_one('MarkdownFence').code)
                    note = app.query_one('#issue_note', Static)
                    self.assertIn('Description shortened to 2,048 characters.', note.render().plain)
                    self.assertFalse(note.render().spans)
                    self.assertGreater(note.region.y, markdown.region.y)
                    self.assertLess(note.region.bottom, 32)
                    await pilot.press('q')
                app.worker.thread.join(2)

    async def test_eligible_item_merges_agents_count_lines_and_retains_selection(self):
        description = {'available': True, 'text': 'Shared item description'}
        self.state['latest_pass']['rows'][1].update(failures=1, max_attempts=3, description=description)
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready'},
            {'item': 12, 'agent': 'integrator', 'state': 'ready', 'description': description},
        ])
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(200, 40)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one(Tree)
            key = 'plan:12'
            self.assertEqual(app.groups['Eligible'].label.plain, 'Eligible · 2')
            self.assertEqual([node.data for node in app.groups['Eligible'].children], ['plan:12', 'plan:20'])
            tree.get_node_at_line(0)
            node = app.nodes[key]
            first = tree.render_line(node._line - int(tree.scroll_y)).text.rstrip()
            detail = tree.render_line(node._line + 1 - int(tree.scroll_y)).text.rstrip()
            self.assertTrue(first.startswith('● ⌥12 Foreign candidate'))
            self.assertTrue(first.endswith('next'))
            self.assertEqual(detail, '  reviewer 1/3 failures, integrator')
            app.select(key)
            tree.move_cursor(node)
            await pilot.press('2')
            markdown = app.query_one('#issue_body', Markdown)
            await self.ready(app, pilot, lambda: markdown.source == description['text'])
            await pilot.press('3')
            stream = io.StringIO()
            Console(file=stream, width=100, color_system=None).print(app.query_one('#runs_text', Static).content)
            self.assertIn('reviewer', stream.getvalue())
            self.assertIn('other-host', stream.getvalue())
            self.state['latest_pass']['rows'][1]['state'] = 'owned'
            publish_snapshot(self.path, self.state)
            # Row data can change before the Tree rebuilds its line positions.
            await self.ready(app, pilot, lambda: app.rows[key].agent == 'integrator' and
                             tree.render_line(node._line + 1 - tree.scroll_offset.y).text.rstrip() == '  integrator')
            self.assertEqual(app.selected, key)
            self.assertIs(app.nodes[key], node)
            self.assertIs(tree.cursor_node, node)
            self.assertEqual(app.groups['Eligible'].label.plain, 'Eligible · 2')
            self.assertEqual(app.query_one(TabbedContent).active, 'runs')
            self.assertEqual(markdown.source, description['text'])
            tree.get_node_at_line(0)  # Resolve the reordered rows before reading node._line.
            self.assertEqual(tree.render_line(node._line + 1 - int(tree.scroll_y)).text.rstrip(), '  integrator')
            self.state['latest_pass']['rows'][1]['state'] = 'ready'
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: len(app.rows[key].eligible_plans) == 2 and
                             tree.render_line(app.nodes[key]._line + 1 - tree.scroll_offset.y).text.rstrip() ==
                             '  reviewer 1/3 failures, integrator')
            self.assertEqual(app.selected, key)
            self.assertIs(tree.cursor_node, app.nodes[key])
            self.assertEqual(sum(row.item == 12 for row in app.rows.values()), 1)
            # Returning from a per-agent attention row finds that agent even
            # when it is no longer first in the merged Eligible row.
            plans = self.state['latest_pass']['rows']
            plans[1]['state'] = 'blocked'
            plans[1], plans[-1] = plans[-1], plans[1]
            publish_snapshot(self.path, self.state)
            attention = 'plan:12:reviewer'
            await self.ready(app, pilot, lambda: attention in app.nodes)
            app.select(attention)
            tree.move_cursor(app.nodes[attention])
            await self.ready(app, pilot, lambda: app.pane.selected == attention)
            plans[-1]['state'] = 'ready'
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.selected == key and attention not in app.rows and
                             tree.render_line(app.nodes[key]._line + 1 - tree.scroll_offset.y).text.rstrip() ==
                             '  integrator, reviewer 1/3 failures')
            self.assertIs(tree.cursor_node, app.nodes[key])
            self.assertEqual([plan['agent'] for plan in app.rows[key].eligible_plans], ['integrator', 'reviewer'])
            self.assertNotIn('Needs attention', app.groups)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_idle_placeholder_is_one_dim_inert_line_and_running_stays_first(self):
        assignment = self.state['assignment']
        self.state['assignment'] = None
        self.state['latest_pass']['rows'] = [self.state['latest_pass']['rows'][2]]
        self.state['outcomes'] = []
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot, lambda: app.idle_node is not None)
            tree = app.query_one('#work', Tree)
            tree.get_node_at_line(0)
            idle = app.idle_node
            self.assertEqual([node.label.plain for node in tree.root.children], ['Running · 0'])
            self.assertFalse(tree.show_root)
            self.assertEqual(tree.virtual_size.height, 2)
            self.assertIsNone(tree._get_label_region(idle._line))
            line = tree.render_line(idle._line - tree.scroll_offset.y)
            self.assertEqual(idle.label.plain, '    Idle · nothing eligible for this launcher')
            self.assertTrue(line.text.strip().startswith('Idle · nothing eligible'))
            self.assertNotIn('┄', line.text)
            self.assertEqual(line.cell_length, tree.scrollable_content_region.width)
            self.assertTrue(any(segment.style.dim for segment in line))
            self.assertIsNone(idle.data)
            tree.focus()
            tree.move_cursor(idle)
            await pilot.press('enter')
            await pilot.click('#work', offset=(6, idle._line - tree.scroll_offset.y))
            self.assertIsNone(tree.cursor_node)
            self.assertIsNone(app.selected)
            self.assertEqual(app.rows, {})
            self.assertEqual(app.nodes, {})
            self.assertIsNone(app.reading.page)
            self.assertIsNone(app.pane.selected)
            self.assertIn('○ Idle · waiting for the next poll', app.query_one('#run_status', Static).render().plain)
            await pilot.press('2', '3', '1')
            self.assertEqual(transport.calls, [])
            self.state['latest_pass']['rows'].append(
                {'item': 21, 'agent': 'worker', 'state': 'blocked', 'reason': 'Needs a decision'})
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: 'Needs attention' in app.groups)
            self.assertIs(app.idle_node, idle)
            self.assertEqual([node.label.plain for node in tree.root.children],
                             ['Running · 0', 'Needs attention · 1'])
            self.state['assignment'] = assignment
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.selected == 'assignment:owned-run')
            self.assertIsNone(app.idle_node)
            self.assertEqual([node.label.plain for node in tree.root.children],
                             ['Running · 1', 'Needs attention · 1'])
            self.assertEqual([node.data for node in app.groups['Running'].children], ['assignment:owned-run'])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_picked_previous_assignment_keeps_details_without_a_running_row(self):
        assignment = self.state['assignment']
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            key = app.selected
            app.select(key)  # Keep this run selected when another starts.
            await pilot.press('f')
            page = app.reading.page
            self.state['assignment'] = None
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[key].state == 'earlier observation')
            self.assertEqual(app.selected, key)
            self.assertNotIn(key, app.nodes)
            self.assertEqual(app.groups['Running'].label.plain, 'Running · 0')
            self.assertIsNotNone(app.idle_node)
            self.assertEqual(app.reading.page, page)
            self.state['assignment'] = dict(assignment, run='next-run')
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: 'assignment:next-run' in app.nodes)
            self.assertEqual(app.selected, key)
            self.assertEqual(app.groups['Running'].label.plain, 'Running · 1')
            self.assertEqual([node.data for node in app.groups['Running'].children], ['assignment:next-run'])
            self.assertEqual(app.reading.page, page)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_claiming_selection_picks_up_the_named_run_and_late_log_without_keys(self):
        for chosen in (False, True):
            for log_first in (False, True):
                with self.subTest(chosen=chosen, log_first=log_first):
                    self.log.unlink(missing_ok=True)
                    self.state['assignment'].pop('run', None)
                    publish_snapshot(self.path, self.state)
                    transport = RecordingDescriptionTransport()
                    app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                    async with app.run_test(size=(110, 32)) as pilot:
                        await self.ready(app, pilot, lambda: app.selected == 'assignment:claiming')
                        if chosen:
                            app.select(app.selected)
                        output = app.query_one(LogPane)
                        output.focus()
                        reading = app.reading
                        self.assertEqual(reading.empty_message, 'No local log cached for this row.')
                        if log_first:
                            self.log.write_bytes(event(1, 20))
                        self.state['assignment']['run'] = 'owned-run'
                        publish_snapshot(self.path, self.state)
                        await self.ready(app, pilot, lambda: app.selected == 'assignment:owned-run'
                                         and app.reading.page is not None)
                        if not log_first:
                            self.assertEqual(app.reading.page.refs, ())
                            self.assertEqual(app.reading.empty_message, 'No log output yet.')
                            self.log.write_bytes(event(1, 20))
                        await self.ready(app, pilot, lambda: any('event 00001' in line.text for line in output.lines))
                        self.assertIs(app.reading, reading)
                        self.assertEqual(app.chosen, chosen)
                        self.assertNotIn('assignment:claiming', app.rows)
                        self.assertEqual(app.rows[app.selected].state, 'running')
                        self.assertEqual(app.groups['Running'].label.plain, 'Running · 1')
                        self.assertIs(app.focused, output)
                        self.assertTrue(app.reading.follow)
                        with self.log.open('ab') as stream:
                            stream.write(event(2, 20))
                        await self.ready(app, pilot, lambda: any('event 00002' in line.text for line in output.lines))
                        self.assertEqual(transport.calls, [])
                        await pilot.press('q')
                    app.worker.thread.join(2)
                    self.assertFalse(app.worker.thread.is_alive())

    async def test_log_g_retries_missing_file_then_reattaches_and_follows_without_github(self):
        self.log.unlink()
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                output = app.query_one(LogPane)
                note = app.query_one('#log_note', Static)
                self.assertIsNone(app.reading.log.error)
                self.assertEqual(app.reading.empty_message, 'No log output yet.')
                self.assertNotIn('Read error:', note.render().plain)
                self.assertNotIn('Read error:', app.raw_details())
                reader = app.worker.readers[str(self.log)]
                await pilot.press('g')
                await self.ready(app, pilot, lambda: app.worker.readers[str(self.log)] is not reader
                                 and app.reading.page is not None)
                self.assertIsNone(app.reading.log.error)
                self.assertNotIn('Read error:', note.render().plain)
                self.assertNotIn('Read error:', app.raw_details())
                self.assertEqual(app.reading.empty_message, 'No log output yet.')
                self.assertEqual(app.reading.page.refs, ())
                await pilot.press('f', 'u')
                reader = app.worker.readers[str(self.log)]
                original = app.worker.read

                def delayed(request):
                    result = original(request)
                    if not entered.is_set():
                        entered.set()
                        release.wait(5)
                    return result

                with patch.object(app.worker, 'read', side_effect=delayed):
                    await self.ready(app, pilot, entered.is_set)
                    self.log.write_bytes(b''.join(event(i, 20) for i in range(1000)))
                    await pilot.press('g')
                    release.set()
                    await self.ready(app, pilot, lambda: any('event 00999' in line.text for line in output.lines))
                    self.assertIsNot(app.worker.readers[str(self.log)], reader)
                    self.assertTrue(app.reading.follow)
                    self.assertTrue(app.reading.raw)
                    self.assertGreater(app.reading.page.start, 0)
                    self.assertEqual(app.reading.page.end, self.log.stat().st_size)
                    await self.settled(app, output)
                    self.assertEqual(output.scroll_y, output.max_scroll_y)
                    with self.log.open('ab') as stream:
                        stream.write(event(1000, 20))
                    await self.ready(app, pilot, lambda: any('event 01000' in line.text for line in output.lines))
                self.assertIn('g reload', app.query_one('#status', Static).render().plain)
                await pilot.press('?')
                self.assertIn('g on Log', app.screen.query_one('#raw_details', Static).render().plain)
                await pilot.press('g', 'escape')
                self.assertEqual(transport.calls, [])
                await pilot.press('q')
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_selected_plan_claimed_elsewhere_leaves_work_but_keeps_item_history(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            key = 'plan:12'
            app.select(key)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            self.state['latest_pass']['rows'][1]['state'] = 'owned'
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[key].state == 'earlier observation' and
                             key not in app.nodes and list(app.groups) == ['Running'])
            self.assertEqual(app.selected, key)
            self.assertNotIn(key, app.nodes)
            self.assertEqual(list(app.groups), ['Running'])
            self.assertEqual(app.groups['Running'].label.plain, 'Running · 1')
            await pilot.press('3')
            stream = io.StringIO()
            Console(file=stream, width=72, color_system=None).print(app.query_one('#runs_text', Static).content)
            self.assertIn('other-host', stream.getvalue())
            self.state['latest_pass']['state'] = 'complete'
            self.state['latest_pass']['rows'] = []
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.session.data['latest_pass']['state'] == 'complete' and
                             key not in app.nodes)
            self.assertNotIn(key, app.nodes)
            self.assertEqual(app.selected, key)
            self.state['latest_pass']['rows'] = [
                {'item': 12, 'agent': 'reviewer', 'state': 'ready', 'reason': 'Trigger matched'}]
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: key in app.nodes and app.rows[key].state == 'ready')
            self.assertEqual(app.selected, key)
            self.assertEqual(app.rows[key].state, 'ready')
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_runs_follows_item_selection_refresh_and_retained_observation_without_github(self):
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        def displayed():
            stream = io.StringIO()
            Console(file=stream, width=72, color_system=None).print(app.query_one('#runs_text', Static).content)
            return stream.getvalue()
        def identity():
            return app.query_one('#item_header', Static).render().plain
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('3')
            self.assertIn('#114 Cached title', identity())
            self.assertNotIn('Cached title', displayed())
            self.assertIn('filed by bk-one', displayed())
            self.assertIn('BLOCKED:', displayed())
            self.assertIn('build-01', displayed())
            app.select('plan:12')
            await self.ready(app, pilot, lambda: '⌥12 Foreign candidate' in identity())
            self.assertNotIn('Foreign candidate', displayed())
            self.assertIn('closes #11 · 4 runs', displayed())
            self.assertIn('3 earlier runs omitted.', displayed())
            self.assertNotIn('filed by', displayed())
            self.assertNotIn('#114', displayed())
            self.state['histories']['12']['title'] = 'Updated candidate'
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: '⌥12 Updated candidate' in identity())
            self.state['latest_pass']['rows'] = []
            self.state['histories'].pop('12')
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[app.selected].state == 'earlier observation')
            self.assertIn('⌥12 Updated candidate', identity())
            app.select('outcome:previous-run')
            await self.ready(app, pilot, lambda: '#10 Earlier item' in identity())
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_quiet_ticks_do_not_update_static_content_even_across_seconds(self):
        self.state['assignment'] = None
        self.state['activity'] = {'state': 'idle'}
        self.state['latest_pass']['rows'] = [self.state['latest_pass']['rows'][1]]
        self.state['histories']['12']['runs'][0]['result'] = 'success'
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot, lambda: app.selected == 'plan:12')
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            # The help footer is also fixed-height; raw_details retains its own behavior.
            await pilot.press('?')
            widgets = [app.query_one(selector, Static) for selector in (
                '#log_mode', '#tab_rule', '#status', '#log_state', '#item_header',
                '#log_note', '#run_status', '#unblock_note', '#runs_text')]
            widgets.append(app.screen.query_one('#raw_status', Static))
            now = datetime.now(timezone.utc).replace(microsecond=0)
            with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                app._static_values.clear()
                app.last_runs = None
                with patch.object(Static, 'update', autospec=True) as update:
                    app.tick()
                    self.assertEqual([call.args[0] for call in update.call_args_list],
                                     [widgets[7], widgets[8], *widgets[:3], widgets[9], *widgets[3:7]])
                    for call in update.call_args_list:
                        self.assertEqual(call.kwargs['layout'], call.args[0] in widgets[7:9])
                    update.reset_mock()
                    for tick in range(1, 31):
                        clock.now.return_value = now + timedelta(milliseconds=100 * tick)
                        app.tick()
                    update.assert_not_called()
            await pilot.press('?', '3')
            with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                app.tick()
                with patch.object(Static, 'update', autospec=True) as update:
                    for tick in range(1, 31):
                        clock.now.return_value = now + timedelta(milliseconds=100 * tick)
                        app.tick()
                    update.assert_not_called()
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_countdown_and_loaded_comment_update_only_when_seconds_change(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state['assignment'] = None
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=60)).isoformat()}
        self.state['latest_pass']['rows'] = []
        self.state['outcomes'] = []
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot, lambda: app.session is not None)
            comment = ActionComment(available=True, source='GitHub', observed_at=now.timestamp())
            status, note = app.query_one('#status', Static), app.query_one('#unblock_note', Static)
            with (patch('ub_agents.view_ui.datetime', wraps=datetime) as clock,
                  patch.object(app.descriptions, 'clock') as loaded_clock,
                  patch.object(app, 'current_action', return_value=comment),
                  patch.object(status, 'update', wraps=status.update) as status_update,
                  patch.object(note, 'update', wraps=note.update) as note_update):
                clock.now.return_value = now
                loaded_clock.return_value = now.timestamp()
                app.update_status()
                app.update_unblock()
                status_update.reset_mock()
                note_update.reset_mock()
                for tick in range(1, 20):
                    clock.now.return_value = now + timedelta(milliseconds=100 * tick)
                    loaded_clock.return_value = clock.now.return_value.timestamp()
                    app.update_status()
                    app.update_unblock()
                self.assertEqual(status_update.call_count, 1)
                self.assertEqual(note_update.call_count, 2)
                self.assertFalse(status_update.call_args.kwargs['layout'])
                self.assertTrue(note_update.call_args.kwargs['layout'])
                self.assertIn('next poll 59s', status.render().plain)
                self.assertIn('loaded 2s ago', note.render().plain)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_runs_clock_is_tenths_only_on_active_tab_and_activation_renders_immediately(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            from ub_agents.view_runs import runs_view
            with (patch('ub_agents.view_ui.datetime', wraps=datetime) as clock,
                  patch('ub_agents.view_ui.runs_view', wraps=runs_view) as render):
                app.last_runs = None
                for tick in range(20):
                    clock.now.return_value = now + timedelta(milliseconds=100 * tick)
                    app.update_runs()
                self.assertEqual(render.call_count, 2)
                # Activation must render even if no clock bucket has changed.
                await pilot.press('3')
                self.assertGreater(render.call_count, 2)
                render.reset_mock()
                app.last_runs = None
                for tick in range(10):
                    clock.now.return_value = now + timedelta(milliseconds=100 * tick)
                    app.update_runs()
                self.assertEqual(render.call_count, 10)
                # A cached history change still renders immediately on a hidden tab.
                await pilot.press('1')
                app.update_runs()
                render.reset_mock()
                app.session.data['histories']['114']['runs'].append({'agent': 'reviewer', 'result': 'success'})
                runs = app.query_one('#runs_text', Static)
                with patch.object(runs, 'update', wraps=runs.update) as update:
                    app.update_runs()
                    render.assert_called_once()
                    update.assert_called_once()
                    self.assertTrue(update.call_args.kwargs['layout'])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_runs_activation_replaces_same_shape_history_before_first_layout(self):
        self.state['histories']['12']['runs'][0]['result'] = 'success'
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(160, 45)) as pilot:
            await self.ready(app, pilot)
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                app.select('plan:12')
                runs = app.query_one('#runs_text', Static)
                self.assertEqual(runs.content_size.width, 0)
                app.session.data['histories']['12']['runs'][0]['summary'] = 'ZZZ'
                app.update_runs()
                # Activation happens before layout. A frozen clock prevents the
                # next one-second bucket from masking a stale content write.
                await pilot.press('3')
                table = list(runs.content.renderables)[1]
                self.assertEqual(table.columns[2]._cells[0].plain, 'reviewer · ZZZ')
                await pilot.pause(0.5)
                self.assertIn('reviewer · ZZZ', '\n'.join(
                    runs.render_line(y).text for y in range(runs.size.height)))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_runs_and_log_spinners_advance_each_tenth_with_static_status_text(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('3')
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
            status_text = None
            for tick, glyph in enumerate('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠋'):
                with self.subTest(tick=tick):
                    later = now + timedelta(milliseconds=100 * tick)
                    with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
                        clock.now.return_value = later
                        app.update_runs()
                    table = list(app.query_one('#runs_text', Static).content.renderables)[1]
                    results = table.columns[1]._cells
                    self.assertEqual([cell.plain for cell in results], ['✓', '✓', glyph])
                    # Patch only this synchronous render, keeping Textual's timers live.
                    with patch('ub_agents.view_ui.time.monotonic', return_value=later.timestamp()):
                        app.update_status()
                    line = app.query_one('#run_status', Static).render().plain.split('\n')[1]
                    self.assertEqual(line[0], glyph)
                    if status_text is None:
                        status_text = line[1:]
                    self.assertEqual(line[1:], status_text)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def poll_keeps_cursor(self, before, after):
        for size in ((110, 32), (80, 24)):
            with self.subTest(size=size):
                self.state['latest_pass'] = {'state': 'complete', 'rows': [
                    {'item': item, 'agent': 'worker', 'state': 'ready'} for item in before]}
                publish_snapshot(self.path, self.state)
                with patch.object(View, 'tick', autospec=True, side_effect=View.tick) as tick:
                    app = View(self.root, self.path)
                    async with app.run_test(size=size) as pilot:
                        await self.ready(app, pilot)
                        key = 'plan:21'
                        app.select(key)
                        tree = app.query_one('#work', Tree)
                        node = app.nodes[key]
                        tree.move_cursor(node)
                        await self.ready(app, pilot, lambda: app.pane.selected == key)
                        app.worker.close()
                        app.worker.thread.join(2)
                        tick.side_effect = None
                        await self.settled(app, tree)
                        line = node._line
                        reading = app.reading
                        reading.follow, reading.raw = False, True
                        tabs = app.query_one(TabbedContent)
                        focus, tab = app.focused, tabs.active
                        output = app.query_one(LogPane)
                        anchor = output.anchor()
                        self.state['latest_pass']['rows'] = [
                            {'item': item, 'agent': 'worker', 'state': 'ready'} for item in after]
                        app.session = Session(self.path, self.state)
                        pane = work_pane(app.session, self.root, app.pane, app.selected, app.chosen)
                        app.populate(pane)
                        # Check before yielding to Textual, then after its refresh.
                        for refreshed in (False, True):
                            if refreshed:
                                await self.settled(app, tree)
                            self.assertIs(tree.cursor_node, app.nodes[key])
                            self.assertIs(tree.get_node_at_line(tree.cursor_line), app.nodes[key])
                            self.assertEqual(app.selected, key)
                            self.assertIs(app.reading, reading)
                            self.assertFalse(reading.follow)
                            self.assertTrue(reading.raw)
                            self.assertEqual(output.anchor(), anchor)
                            self.assertIs(app.focused, focus)
                            self.assertEqual(tabs.active, tab)
                        self.assertIs(app.nodes[key], node)
                        self.assertNotEqual(node._line, line)
                        await pilot.press('q')
                    app.worker.thread.join(2)
                    self.assertFalse(app.worker.thread.is_alive())

    async def test_poll_adds_row_above_highlight_without_changing_right_pane(self):
        await self.poll_keeps_cursor([20, 21, 22], [19, 20, 21, 22])

    async def test_poll_removes_row_above_highlight_without_changing_right_pane(self):
        await self.poll_keeps_cursor([20, 21, 22], [21, 22])

    async def test_poll_keeps_independent_highlight_and_falls_back_when_it_disappears(self):
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': item, 'agent': 'worker', 'state': 'ready'} for item in (20, 21, 22)]}
        publish_snapshot(self.path, self.state)
        with patch.object(View, 'tick', autospec=True, side_effect=View.tick) as tick:
            app = View(self.root, self.path)
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                selected = app.selected
                app.select(selected)
                await pilot.press('f', 'home', 'pagedown', '3')
                app.worker.close()
                app.worker.thread.join(2)
                tick.side_effect = None
                tree = app.query_one('#work', Tree)
                output = app.query_one(LogPane)
                await self.settled(app, output)
                reading, page, anchor = app.reading, app.reading.page, output.anchor()
                focus, tab = app.focused, app.query_one(TabbedContent).active
                tree.move_cursor(app.nodes['plan:21'])
                cases = [
                    ([(22, 'ready'), (21, 'ready'), (20, 'ready')], 'plan:21'),
                    ([(21, 'blocked'), (20, 'ready'), (22, 'ready')], 'plan:21:worker'),
                    ([(20, 'ready'), (22, 'ready')], selected),
                ]
                for rows, highlighted in cases:
                    with self.subTest(highlighted=highlighted):
                        self.state['latest_pass']['rows'] = [
                            {'item': item, 'agent': 'worker', 'state': state} for item, state in rows]
                        app.session = Session(self.path, self.state)
                        pane = work_pane(app.session, self.root, app.pane, app.selected, app.chosen)
                        app.populate(pane)
                        for refreshed in (False, True):
                            if refreshed:
                                await self.settled(app, tree)
                            self.assertIs(tree.cursor_node, app.nodes[highlighted])
                            self.assertIs(tree.get_node_at_line(tree.cursor_line), app.nodes[highlighted])
                            self.assertEqual(app.selected, selected)
                            self.assertIs(app.reading, reading)
                            self.assertEqual(reading.page, page)
                            self.assertFalse(reading.follow)
                            self.assertEqual(output.anchor(), anchor)
                            self.assertIs(app.focused, focus)
                            self.assertEqual(app.query_one(TabbedContent).active, tab)
                await pilot.press('q')
            app.worker.thread.join(2)
            self.assertFalse(app.worker.thread.is_alive())

    async def test_cursor_follows_row_when_tree_changes_before_a_render(self):
        self.state['latest_pass']['rows'].append(
            {'item': 21, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'})
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            key = 'plan:21'
            app.select(key)
            tree = app.query_one('#work', Tree)
            tree.move_cursor(app.nodes[key])
            old_node = tree.cursor_node
            reading = app.reading
            reading.follow, reading.raw = False, True
            changed = dict(self.state, latest_pass={'state': 'complete', 'rows': [
                {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required'}]})
            pane = work_pane(Session(self.path, changed), self.root, app.pane, app.selected, app.chosen)
            app.populate(pane)
            # Assert before yielding to Textual's next layout or idle callback.
            key = 'plan:21:worker'
            self.assertIsNot(old_node, app.nodes[key])
            self.assertIs(tree.cursor_node, app.nodes[key])
            self.assertIs(app.reading, reading)
            self.assertFalse(app.reading.follow)
            self.assertTrue(app.reading.raw)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_selected_plan_survives_section_order_change_and_disappearance(self):
        description = {'available': True, 'text': '# Planned work\n\n**Cached body**'}
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
            {'item': 21, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched',
             'description': description},
        ])
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f', 'home', 'pagedown')
            output = app.query_one(LogPane)
            own, page, anchor = app.selected, app.reading.page, output.anchor()
            key = next(k for k, row in app.rows.items() if row.item == 21)
            app.select(key)
            markdown = app.query_one('#issue_body', Markdown)
            await self.ready(app, pilot, lambda: len(markdown.query('MarkdownH1')) == 1)
            self.assertEqual(markdown.source, description['text'])
            tree = app.query_one('#work', Tree)
            app.move_cursor(app.nodes[key])
            output.focus()
            self.assertIs(tree.cursor_node, app.nodes[key])
            self.state['latest_pass']['rows'] = [
                {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required',
                 'description': description}]
            publish_snapshot(self.path, self.state)
            # The tree moves its cursor back to the kept node after the next refresh.
            key = 'plan:21:worker'
            await self.ready(app, pilot, lambda: key in app.nodes and
                             app.nodes[key].parent is app.groups.get('Needs attention')
                             and tree.cursor_node is app.nodes[key])
            self.assertEqual(app.selected, key)
            self.assertIs(app.focused, output)
            self.assertEqual(markdown.source, description['text'])
            self.assertNotIn('Eligible', app.groups)
            self.assertEqual([node.label.plain for node in tree.root.children[:2]],
                             ['Running · 1', 'Needs attention · 1'])
            for reason in ('Waiting for blockers #31', 'Waiting for active milestone #10'):
                self.state['latest_pass']['rows'][0]['reason'] = reason
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: app.rows[key].hidden and key not in app.nodes
                                 and app.session.data['latest_pass']['rows'][0]['reason'] == reason)
                self.assertEqual(app.selected, key)
                self.assertIs(app.focused, output)
                self.assertEqual(markdown.source, description['text'])
                self.assertEqual([node.label.plain for node in tree.root.children], ['Running · 1'])
            self.state['latest_pass']['rows'] = []
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[key].state == 'earlier observation')
            self.assertEqual(app.selected, key)
            self.assertIs(app.focused, output)
            self.assertEqual(markdown.source, description['text'])
            app.select(own)
            await pilot.pause(0.3)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_published_passes_keep_selection_details_focus_and_paused_log(self):
        def write_snapshot(snapshot):
            # Match the publisher: readers see a complete old or new snapshot.
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(snapshot))
            os.replace(temporary, self.path)

        memory = MemoryPublisher()
        observer = Observations(config(self.root), 'operator', None, memory)
        observer.state['assignment'] = self.state['assignment']
        observer.state['outcomes'] = [row | {'target': row['item']} for row in self.state['outcomes']]
        plans = [Plan(replace(issue(n), body=f'Cached body {n}'), agent(self.root), None,
                      state, 'Ready', 1, history=({'kind': 'lease', 'assignment': n,
                      'agent': 'worker', 'run': f'run-{n}', 'created': '2026-10-03T00:00:00Z',
                      'expires': '2026-10-03T00:30:00Z', 'state': 'released',
                      'summary': f'Cached history {n}'},))
                 for n, state in ((20, 'ready'), (21, 'ready'), (22, 'ready'), (23, 'waiting'))]
        observer.begin_pass()
        for plan in plans:
            observer.plan(plan)
        observer.complete_pass()
        write_snapshot(memory.snapshots[-1])
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f', 'home', 'pagedown')
            output = app.query_one(LogPane)
            await self.settled(app, output)
            own, page, anchor = app.selected, app.reading.page, output.anchor()
            key = 'plan:21'
            app.select(key)
            markdown = app.query_one('#issue_body', Markdown)
            await self.ready(app, pilot, lambda: markdown.source == 'Cached body 21')
            tree = app.query_one('#work', Tree)
            app.move_cursor(app.nodes[key])
            output.focus()
            async def publish():
                snapshot = memory.snapshots[-1]
                write_snapshot(snapshot)
                await self.ready(app, pilot, lambda: app.session.data['latest_pass'] == snapshot['latest_pass'])
                self.assertEqual(app.selected, key)
                self.assertIs(app.focused, output)
                self.assertIs(tree.cursor_node, app.nodes[key])
                self.assertEqual(markdown.source, 'Cached body 21')
                self.assertEqual(sum(row.group != 'Recent activity' for row in app.rows.values()), len(app.nodes))
                self.assertEqual(sum(row.item == 114 for row in app.rows.values()), 1)
                self.assertEqual(app.rows[key].data['history']['runs'][0]['summary'], 'Cached history 21')
                stream = io.StringIO()
                Console(file=stream, width=110, color_system=None).print(app.query_one('#runs_text', Static).content)
                self.assertIn('Cached history 21', stream.getvalue())
            observer.begin_pass()
            await publish()
            for plan in (replace(plans[3], state='ready'),
                         Plan(issue(24), agent(self.root), None, 'ready', 'New', 1), plans[0]):
                observer.plan(plan)
                await publish()
                self.assertTrue(all(f'plan:{n}' in app.rows for n in (20, 21, 22, 23)))
                self.assertEqual(app.rows[key].state, 'ready')
                self.assertIn('partial', app.query_one('#work_pane').border_title)
            self.assertEqual([node.data for node in app.groups['Eligible'].children],
                             ['plan:20', key, 'plan:22', 'plan:23', 'plan:24'])
            observer.complete_pass()
            await publish()
            await self.ready(app, pilot, lambda: app.rows[key].state == 'earlier observation')
            self.assertNotIn('plan:22', app.rows)
            self.assertNotIn('partial', app.query_one('#work_pane').border_title)
            self.assertEqual([node.data for node in app.groups['Eligible'].children if node.data != key],
                             ['plan:23', 'plan:24', 'plan:20'])
            app.select(own)
            await pilot.pause(0.3)
            await self.settled(app, output)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_recent_split_fixed_with_empty_and_overflowing_live_work(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree, recent = app.query_one('#work', Tree), app.query_one(RecentActivity)
            for size in ((110, 32), (140, 44), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                boundary = recent.region.y
                self.assertLessEqual(abs(tree.size.height - recent.size.height), 1)
                self.assertEqual(tree.region.bottom, boundary)
                self.state['latest_pass']['rows'] = [
                    {'item': 50, 'agent': 'worker', 'state': 'blocked'}] + [
                    {'item': n, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'}
                    for n in range(1, 50)]
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: 'Eligible' in app.groups and
                                 len(app.groups['Eligible'].children) == 10 and
                                 tree.virtual_size.height > tree.size.height)
                tree.get_node_at_line(0)
                separators = {app.groups[name]._line - 1 for name in ('Needs attention', 'Eligible')}
                self.assertTrue(separators <= tree._spacer_lines)
                tree.scroll_end(animate=False, immediate=True)
                await pilot.pause()
                self.assertGreater(tree.scroll_y, 0)
                self.assertEqual(recent.region.y, boundary)
                self.assertEqual(recent.scroll_y, 0)
                self.assertEqual(recent.max_scroll_y, 0)
                self.state['assignment'] = None
                self.state['latest_pass'] = {'state': 'complete', 'rows': []}
                self.state['outcomes'] = []
                app.select('outcome:previous-run')  # Retain only a right-pane outcome, not live work.
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: list(app.groups) == ['Running'] and not recent.rows)
                self.assertEqual(app.groups['Running'].label.plain, 'Running · 0')
                self.assertEqual(recent.region.y, boundary)
                self.assertTrue(recent.render().plain.startswith('Recent activity · 0 today'))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_recent_issue_pr_handoff_markers_preserve_dim_and_selection_styles(self):
        stamp = self.state['published_at']
        when = datetime.fromisoformat(stamp).astimezone().strftime('%H:%M')
        self.state['outcomes'] = [
            {'item': 10, 'run': 'issue-handoff', 'kind': 'issue', 'title': 'Issue ⌥88',
             'agent': 'implementer', 'time': stamp, 'result': 'handed-off', 'completed': True,
             'handoff': 167, 'summary': 'Summary ⌥99 #98'},
            {'item': 12, 'run': 'pr-merged', 'agent': 'integrator', 'time': stamp,
             'result': 'merged', 'completed': True, 'target': 12, 'summary': 'squash-merged'},
        ]
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(160, 40)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(app, pilot, lambda: len(recent.rows) == 2)
            # Expose the complete summary; normal-width clipping is covered separately.
            app.query_one('#work_pane').styles.width = 80
            await pilot.pause()
            for theme in ('ub-agents', 'textual-light'):
                app.theme = theme
                await pilot.pause()
                rendered = recent.render()
                lines = rendered.plain.splitlines()
                self.assertTrue(lines[1].startswith('✓ ⌥12 Foreign candidate'))
                self.assertEqual(lines[2], f'  integrator · {when} · squash-merged')
                self.assertEqual(lines[3], '')
                self.assertTrue(lines[4].startswith('✓ #10 Issue ⌥88'))
                self.assertEqual(lines[5], f'  implementer · {when} · opened ⌥167 · Summary ⌥99 #98')
                accent = theme_style(app, 'view-accent').color
                for reference in ('⌥12', '⌥167'):
                    offset = rendered.plain.index(reference)
                    marker = rendered.get_style_at_offset(app.console, offset)
                    number = rendered.get_style_at_offset(app.console, offset + 1)
                    self.assertEqual(marker.color, accent)
                    self.assertNotEqual(number.color, accent)
                    self.assertTrue(marker.dim)
                    self.assertEqual(marker.dim, number.dim)
                    self.assertEqual(marker.bgcolor, number.bgcolor)
                for verbatim in ('⌥88', '⌥99'):
                    style = rendered.get_style_at_offset(app.console, rendered.plain.index(verbatim))
                    self.assertNotEqual(style.color, accent)

            recent.focus()
            recent.cursor = 'outcome:issue-handoff'
            app.select(recent.cursor)
            await pilot.pause()
            rendered = recent.render()
            offset = rendered.plain.index('⌥167')
            marker = rendered.get_style_at_offset(app.console, offset)
            number = rendered.get_style_at_offset(app.console, offset + 1)
            self.assertEqual(marker.color, theme_style(app, 'view-accent').color)
            self.assertFalse(marker.dim)
            self.assertFalse(number.dim)
            self.assertEqual(marker.bgcolor, theme_style(
                app, 'view-accent', bgcolor=app.theme_variables['view-selection']).bgcolor)
            self.assertEqual(marker.bgcolor, number.bgcolor)

            # Missing summaries add no suffix, and legacy target handoffs work.
            self.state['outcomes'][0].pop('summary')
            self.state['outcomes'][0]['target'] = self.state['outcomes'][0].pop('handoff')
            self.state['outcomes'][1].update(kind='issue', title='Snapshot issue')
            self.state['outcomes'][1].pop('summary')
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: 'Summary' not in recent.render().plain)
            lines = recent.render().plain.splitlines()
            self.assertTrue(lines[1].startswith('✓ #12 Snapshot issue'))
            self.assertEqual(lines[2], f'  integrator · {when}')
            self.assertEqual(lines[5], f'  implementer · {when} · opened ⌥167')
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_recent_outcome_cues_preserve_all_row_states_and_follow_the_theme(self):
        cases = [('success', True, '✓', 'view-success'),
                 ('prepared', True, '✓', 'view-success'),
                 ('handed-off', True, '✓', 'view-success'),
                 ('merged', True, '✓', 'view-success'),
                 ('retry', False, '✗', 'view-error'),
                 ('blocked', True, '✗', 'view-error'),
                 ('failed', False, '✗', 'view-error'),
                 ('abandoned', False, '✗', 'view-error'),
                 ('interrupted', False, '○', None)]
        self.state['outcomes'] = [
            {'item': 30 + index, 'kind': 'pr', 'run': result, 'title': 'Title',
             'agent': 'worker', 'time': self.state['published_at'],
             'result': result, 'completed': completed, 'summary': 'Detail'}
            for index, (result, completed, _, _) in enumerate(cases)]
        self.path.write_text(json.dumps(self.state))
        with patch.dict(os.environ):
            os.environ.pop('NO_COLOR', None)
            app = View(self.root, self.path)
        async with app.run_test(size=(160, 80)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(app, pilot, lambda: len(recent.visible_rows) == len(cases))
            expected = {result: (glyph, variable) for result, _, glyph, variable in cases}
            for theme in ('ub-agents', 'textual-light'):
                app.theme = theme
                await pilot.pause()
                for focused in (False, True):
                    (recent if focused else app.query_one(Tree)).focus()
                    await pilot.pause()
                    self.assertEqual(recent.has_focus, focused)
                    for selected in (False, True):
                        for cursor in (False, True):
                            for index, row in enumerate(recent.visible_rows):
                                with self.subTest(theme=theme, result=row.data['result'],
                                                  focused=focused, selected=selected, cursor=cursor):
                                    app.selected = row.key if selected else 'assignment:owned-run'
                                    recent.cursor = row.key if cursor else 'another-row'
                                    first, detail = recent.render().split('\n')[
                                        1 + index * recent.row_stride:3 + index * recent.row_stride]
                                    glyph, variable = expected[row.data['result']]
                                    row_style = theme_style(app, 'foreground' if selected else 'view-muted',
                                                            dim=not selected)
                                    if focused and cursor:
                                        row_style += theme_style(app, 'view-accent',
                                                                 bgcolor=app.theme_variables['view-selection'])
                                    cue_color = theme_style(app, variable).color if variable else row_style.color
                                    self.assertTrue(first.plain.startswith(f'{glyph} ⌥{row.item} Title'))
                                    self.assertTrue(first.plain.endswith(row.data['result']))
                                    offsets = [0, *range(len(first) - len(row.data['result']), len(first))]
                                    for offset in offsets:
                                        cue = first.get_style_at_offset(app.console, offset)
                                        self.assertEqual(cue.color, cue_color)
                                        self.assertEqual(cue.dim, row_style.dim)
                                        self.assertEqual(cue.bgcolor, row_style.bgcolor)
                                    for offset in (1, 3, first.plain.index('Title')):
                                        self.assertEqual(first.get_style_at_offset(app.console, offset), row_style)
                                    marker = first.get_style_at_offset(app.console, 2)
                                    self.assertEqual(marker.color, theme_style(app, 'view-accent').color)
                                    self.assertEqual(marker.dim, row_style.dim)
                                    self.assertEqual(marker.bgcolor, row_style.bgcolor)
                                    self.assertEqual(detail.get_style_at_offset(app.console, 2), row_style)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_recent_clipped_outcome_labels_keep_color_and_alignment_in_both_layouts(self):
        self.state['outcomes'] = [
            {'item': 30 + index, 'run': str(index), 'title': '界' * 80, 'agent': 'worker',
             'time': self.state['published_at'], 'result': result, 'completed': completed}
            for index, (result, completed) in enumerate(
                [('completed-' * 10, True), ('abandoned', False), ('interrupted-' * 10, False)])]
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(app, pilot, lambda: len(recent.visible_rows) == 3)
            for size in ((110, 32), (60, 16)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertEqual(app.narrow, size[0] < 110)
                for width in ('100%', 9):
                    recent.styles.width = width
                    await pilot.pause()
                    rendered = recent.render()
                    lines = rendered.split('\n')
                    self.assertTrue(rendered.no_wrap)
                    self.assertEqual(len(lines), 3 * len(recent.visible_rows) if not app.narrow else 4)
                    for index, row in enumerate(recent.visible_rows):
                        with self.subTest(size=size, width=width, result=row.data['result']):
                            line = lines[1 + index * recent.row_stride]
                            self.assertEqual(line.cell_len, recent.content_size.width)
                            # The title is clipped independently of the right-aligned status.
                            self.assertIn('…', line.plain[:-1])
                            label = line.plain.rsplit(' ', 1)[-1]
                            if width == 9 or row.data['result'] != 'abandoned':
                                self.assertTrue(label.endswith('…'))
                            else:
                                self.assertEqual(label, 'abandoned')
                            variable = ('view-success' if row.data['completed'] else
                                        'view-error' if row.data['result'] == 'abandoned' else 'view-muted')
                            color = theme_style(app, variable).color
                            for offset in [0, *range(len(line) - len(label), len(line))]:
                                self.assertEqual(line.get_style_at_offset(app.console, offset).color, color)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_recent_outcome_cues_are_monochrome_with_no_color(self):
        self.state['outcomes'] = [
            {'item': 30 + index, 'run': str(index), 'title': 'Synthetic outcome',
             'time': self.state['published_at'], 'result': result, 'completed': completed}
            for index, (result, completed) in enumerate(
                [('prepared', True), ('failed', False), ('interrupted', False)])]
        self.path.write_text(json.dumps(self.state))
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(app, pilot, lambda: len(recent.visible_rows) == 3)
            strips = app.screen._compositor.render_strips()[recent.region.y:recent.region.bottom]
            visible = '\n'.join(strip.text for strip in strips)
            for cue in ('✓', '✗', '○', 'prepared', 'failed', 'interrupted'):
                self.assertIn(cue, visible)
            for strip in strips:
                for segment in strip:
                    if segment.style and segment.style.color:
                        color = segment.style.color.get_truecolor()
                        self.assertEqual(color.red, color.green)
                        self.assertEqual(color.green, color.blue)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_recent_blank_rows_are_inert_and_only_whole_items_fit(self):
        self.state['outcomes'] = [dict(self.state['outcomes'][0], run=f'past-{n}') for n in range(8)]
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(140, 44)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(app, pilot, lambda: len(recent.rows) == 8)
            for height, count in ((1, 0), (2, 0), (3, 1), (5, 1), (6, 2), (8, 2), (9, 3)):
                recent.styles.height = height
                await pilot.pause()
                self.assertEqual(len(recent.visible_rows), count)
                lines = recent.render().plain.splitlines()
                self.assertEqual(len(lines), max(1, 3 * count))
                self.assertLessEqual(len(lines), height)
                self.assertEqual([row.key for row in recent.visible_rows],
                                 [f'outcome:past-{n}' for n in range(7, 7 - count, -1)])
            recent.focus()
            recent.cursor = recent.visible_rows[0].key
            await pilot.press('enter', 'down', 'enter')
            self.assertEqual(app.selected, recent.visible_rows[1].key)
            await pilot.press('up', 'enter')
            self.assertEqual(app.selected, recent.visible_rows[0].key)
            for y in (4, 5):
                await pilot.click('#recent', offset=(2, y))
                self.assertEqual(app.selected, recent.visible_rows[1].key)
                self.assertEqual(recent.cursor, app.selected)
            rendered = recent.render()
            blank = rendered.plain.index('\n\n') + 1
            self.assertIsNone(rendered.get_style_at_offset(app.console, blank).bgcolor)
            self.assertIsNotNone(rendered.get_style_at_offset(app.console, blank + 1).bgcolor)
            await pilot.click('#recent', offset=(2, 3))
            self.assertEqual(app.selected, recent.visible_rows[1].key)
            self.assertEqual(recent.cursor, app.selected)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertEqual(len(recent.visible_rows), 8)
            self.assertEqual(len(recent.render().plain.splitlines()), 9)
            for y in (1, 2):
                await pilot.click('#recent', offset=(2, y))
                self.assertEqual(app.selected, recent.visible_rows[y - 1].key)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_recent_whole_rows_dim_selection_and_clipped_selected_outcome(self):
        self.state['assignment'] = None
        self.state['latest_pass'] = {'state': 'complete', 'rows': []}
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.state['outcomes'] = [dict(self.state['outcomes'][0], run=f'past-{n}', title=f'Outcome {n}',
                                      time=old, summary=f'Summary {n}') for n in range(20)]
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            recent = app.query_one(RecentActivity)
            # Selection and focus follow the rows in a later refresh.
            await self.ready(app, pilot, lambda: len(recent.rows) == 20 and app.focused is recent
                             and app.selected == 'outcome:past-19')
            self.assertEqual(app.selected, 'outcome:past-19')
            self.assertIs(app.focused, recent)
            visible = recent.visible_rows
            self.assertEqual([row.key for row in visible],
                             [f'outcome:past-{n}' for n in range(19, 19 - len(visible), -1)])
            rendered = recent.render()
            lines = rendered.plain.splitlines()
            self.assertEqual(len(lines), 3 * len(visible))
            self.assertLessEqual(len(lines), recent.size.height)
            self.assertTrue(lines[0].startswith('Recent activity · 0 today'))
            selected_offset = rendered.plain.index('Outcome 19')
            dim_offset = rendered.plain.index('Outcome 18')
            self.assertFalse(rendered.get_style_at_offset(Console(), selected_offset).dim)
            self.assertTrue(rendered.get_style_at_offset(Console(), dim_offset).dim)
            recent.cursor = visible[-1].key
            await pilot.press('enter', '2')
            selected, focus = app.selected, app.focused
            self.state['outcomes'] = self.state['outcomes'][1:] + [dict(self.state['outcomes'][-1], run='newest')]
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: recent.rows[0].key == 'outcome:newest')
            self.assertNotIn(selected, [row.key for row in recent.visible_rows])
            self.assertEqual(app.selected, selected)
            self.assertIs(app.focused, focus)
            await self.ready(app, pilot, lambda: f'Outcome {visible[-1].run.split("-")[-1]}' in
                             app.query_one('#item_header', Static).render().plain)
            # Once it also leaves the cache, it remains only in the right pane.
            self.state['outcomes'] = [dict(self.state['outcomes'][-1], run=f'new-{n}') for n in range(20)]
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[selected].state == 'earlier observation')
            self.assertEqual(len(recent.rows), 20)
            self.assertNotIn(selected, [row.key for row in recent.rows])
            self.assertEqual(app.selected, selected)
            self.assertIs(app.focused, focus)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_arrows_cross_the_split_without_selecting_the_recent_header(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree, recent = app.query_one('#work', Tree), app.query_one(RecentActivity)
            tree.focus()
            tree.get_node_at_line(0)
            bottom = tree.get_node_at_line(tree.last_line)
            tree.move_cursor(bottom)
            await pilot.press('down', 'enter')
            self.assertIs(app.focused, recent)
            self.assertEqual(app.selected, 'outcome:previous-run')
            await pilot.press('up', 'enter')
            self.assertIs(app.focused, tree)
            self.assertIs(tree.cursor_node, bottom)
            self.assertEqual(app.selected, bottom.data)
            # Clicking the inert header cannot select or collapse it.
            await pilot.click('#recent', offset=(2, 0))
            self.assertEqual(app.selected, bottom.data)
            self.assertTrue(recent.display)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_work_headers_and_idle_clicks_and_expansion_keys_are_inert(self):
        assignment = self.state['assignment']
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': 20, 'agent': 'worker', 'state': 'blocked'},
            {'item': 12, 'agent': 'reviewer', 'state': 'ready'},
            {'item': 22, 'agent': 'worker', 'state': 'ready'},
        ]}
        for size in ((140, 44), (80, 24)):
            with self.subTest(size=size):
                self.state['assignment'] = assignment
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path)
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    tree = app.query_one(Tree)
                    tree.focus()

                    def assert_expanded():
                        tree.get_node_at_line(0)
                        self.assertTrue(tree.root.is_expanded)
                        for group in app.groups.values():
                            self.assertTrue(group.is_expanded)
                            for child in group.children:
                                self.assertIs(tree.get_node_at_line(child._line), child)
                                line = tree.render_line(child._line - tree.scroll_offset.y)
                                reference = str(app.rows[child.data].item) if child.data else 'Idle'
                                self.assertIn(reference, line.text)

                    async def click_label(node):
                        selected, cursor = app.selected, tree.cursor_node
                        tree.scroll_to(y=node._line, animate=False, immediate=True)
                        await pilot.pause()
                        line = tree.render_line(node._line - tree.scroll_offset.y)
                        self.assertTrue(all(not segment.style.meta for segment in line))
                        self.assertIsNone(tree._get_label_region(node._line))
                        await pilot.click('#work', offset=(2, node._line - tree.scroll_offset.y))
                        self.assertEqual(app.selected, selected)
                        self.assertIs(tree.cursor_node, cursor)
                        assert_expanded()
                        for key in ('space', 'shift+space'):
                            await pilot.press(key)
                            self.assertEqual(app.selected, selected)
                            self.assertIs(tree.cursor_node, cursor)
                            assert_expanded()

                    assert_expanded()
                    for group in app.groups.values():
                        await click_label(group)
                    # A reused group is expanded again on the next snapshot too.
                    app.groups['Needs attention'].collapse()
                    self.state['assignment'] = None
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: app.idle_node is not None and
                                     app.groups['Needs attention'].is_expanded)
                    tree.move_cursor(app.nodes['plan:22'])
                    app.select('plan:22')
                    for node in (*app.groups.values(), app.idle_node):
                        await click_label(node)
                    app.query_one(LogPane).focus()
                    tree.focus()
                    await pilot.pause()
                    self.assertIs(tree.cursor_node, app.nodes['plan:22'])
                    assert_expanded()
                    await pilot.press('q')
                app.worker.thread.join(2)

    async def test_work_keyboard_cursor_only_visits_items_in_both_layouts(self):
        plans = [
            {'item': 20, 'agent': 'worker', 'state': 'blocked'},
            {'item': 21, 'agent': 'worker', 'state': 'blocked'},
            {'item': 12, 'agent': 'reviewer', 'state': 'ready'},
            {'item': 22, 'agent': 'worker', 'state': 'ready'},
        ]
        assignment = self.state['assignment']
        for size in ((140, 44), (80, 24)):
            with self.subTest(size=size):
                self.state['assignment'] = assignment
                self.state['latest_pass'] = {'state': 'complete', 'rows': plans}
                publish_snapshot(self.path, self.state)
                app = View(self.root, self.path)
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    tree, recent = app.query_one(Tree), app.query_one(RecentActivity)
                    tree.focus()
                    tree.get_node_at_line(0)
                    items = sorted(app.nodes.values(), key=lambda node: node._line)
                    self.assertIs(tree.cursor_node, items[0])
                    await pilot.press('up')
                    self.assertIs(tree.cursor_node, items[0])
                    for node in items[1:]:
                        await pilot.press('down')
                        self.assertIs(tree.cursor_node, node)
                    await pilot.press('down')
                    self.assertIs(app.focused, recent)
                    await pilot.press('up')
                    self.assertIs(app.focused, tree)
                    self.assertIs(tree.cursor_node, items[-1])
                    for node in reversed(items[:-1]):
                        await pilot.press('up')
                        self.assertIs(tree.cursor_node, node)
                    for node in items:
                        tree.move_cursor(node)
                        for key in ('shift+left', 'shift+right', 'shift+up', 'shift+down',
                                    'space', 'shift+space', 'pageup', 'pagedown', 'home', 'end'):
                            await pilot.press(key)
                            self.assertIn(tree.cursor_node, items, key)
                    # Exercise the inherited direct cursor-line assignments even
                    # though the app's page/home/end keys normally scroll the tabs.
                    for action in ('scroll_home', 'scroll_end', 'page_up', 'page_down'):
                        await tree.run_action(action)
                        self.assertIn(tree.cursor_node, items, action)
                    self.state['assignment'] = None
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: app.idle_node is not None)
                    tree.get_node_at_line(0)
                    first = app.groups['Needs attention'].children[0]
                    tree.move_cursor(first)
                    await pilot.press('up')
                    self.assertIs(tree.cursor_node, first)
                    # With only labels left, Up from Recent stays on an outcome.
                    self.state['latest_pass']['rows'] = []
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: not app.nodes)
                    recent.cursor = recent.visible_rows[0].key
                    recent.focus()
                    await pilot.press('up')
                    self.assertIs(app.focused, recent)
                    self.assertIsNone(tree.cursor_node)
                    tree.focus()
                    await pilot.press('up', 'space', 'shift+space', 'home', 'end')
                    self.assertIsNone(tree.cursor_node)
                    self.assertTrue(app.groups['Running'].is_expanded)
                    await pilot.press('down')
                    self.assertIs(app.focused, recent)
                    await pilot.press('q')
                app.worker.thread.join(2)

    async def test_combined_work_blank_rows_preserve_headings_selection_and_narrow_rows(self):
        self.state['latest_pass'] = {'state': 'complete', 'rows': [
            {'item': 20, 'agent': 'worker', 'state': 'blocked'},
            {'item': 21, 'agent': 'worker', 'state': 'blocked'},
            {'item': 12, 'agent': 'reviewer', 'state': 'ready'},
            {'item': 22, 'agent': 'worker', 'state': 'ready'},
        ]}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot)
            tree, recent = app.query_one(Tree), app.query_one(RecentActivity)
            tree.get_node_at_line(0)
            self.assertEqual(tree.virtual_size.height, 17)
            groups = list(app.groups.values())
            for index, group in enumerate(groups):
                self.assertEqual(group.children[0]._line, group._line + 1)
                if index + 1 < len(groups):
                    self.assertEqual(groups[index + 1]._line, group.children[-1]._line + 3)
            async def check_heading_separators():
                tree.get_node_at_line(0)
                self.assertEqual(app.groups['Running']._line, 0)
                for index, name in enumerate(('Needs attention', 'Eligible'), 1):
                    heading = app.groups[name]
                    preceding = groups[index - 1].children[-1]
                    gap = heading._line - 1
                    self.assertEqual(gap, preceding._line + tree.row_height)
                    self.assertIsNone(tree.get_node_at_line(gap))
                    self.assertIsNone(tree._get_label_region(gap))
                    tree.move_cursor(preceding)
                    await pilot.press('down')
                    self.assertIs(tree.cursor_node, heading.children[0])
                    await pilot.press('up')
                    self.assertIs(tree.cursor_node, preceding)
                    tree.scroll_to(y=max(0, gap - tree.size.height + 2), animate=False, immediate=True)
                    await pilot.pause()
                    await pilot.hover('#work', offset=(2, gap - tree.scroll_offset.y))
                    blank = tree.render_line(gap - tree.scroll_offset.y)
                    self.assertEqual(blank.text.strip(), '')
                    selection = tree.get_component_rich_style('tree--cursor', partial=False).bgcolor
                    self.assertTrue(all(not segment.style.meta and segment.style.bgcolor != selection
                                        for segment in blank))
                    selected, cursor = app.selected, tree.cursor_node
                    await pilot.click('#work', offset=(2, gap - tree.scroll_offset.y))
                    self.assertEqual(app.selected, selected)
                    self.assertIs(tree.cursor_node, cursor)
                    await pilot.press('down')
                    self.assertIs(tree.cursor_node, heading.children[0])
                    await pilot.press('up')
                    self.assertIs(tree.cursor_node, preceding)
            tree.focus()
            await check_heading_separators()
            for name in ('Needs attention', 'Eligible'):
                first, second = app.groups[name].children
                self.assertEqual(second._line, first._line + 3)
                gap = first._line + 2
                self.assertIsNone(tree.get_node_at_line(gap))
                self.assertIsNone(tree._get_label_region(gap))
                tree.move_cursor(first)
                await pilot.pause()
                for line in (first._line, first._line + 1):
                    self.assertIs(tree.get_node_at_line(line), first)
                    self.assertEqual(tree._get_label_region(line).height, 2)
                    strip = tree.render_line(line - tree.scroll_offset.y)
                    self.assertTrue(all(segment.style.meta.get('node') == first.id for segment in strip))
                blank = tree.render_line(gap - tree.scroll_offset.y)
                self.assertEqual(blank.text.strip(), '')
                selection = tree.get_component_rich_style('tree--cursor', partial=False).bgcolor
                self.assertTrue(all(not segment.style.meta and segment.style.bgcolor != selection
                                    for segment in blank))
                await pilot.press('down', 'enter')
                self.assertIs(tree.cursor_node, second)
                self.assertEqual(app.selected, second.data)
                await pilot.press('up', 'enter')
                self.assertIs(tree.cursor_node, first)
                self.assertEqual(app.selected, first.data)
                for offset in (0, 1):
                    await pilot.click('#work', offset=(2, second._line + offset - tree.scroll_offset.y))
                    self.assertIs(tree.cursor_node, second)
                    self.assertEqual(app.selected, second.data)
                await pilot.click('#work', offset=(2, gap - tree.scroll_offset.y))
                self.assertIs(tree.cursor_node, second)
                self.assertEqual(app.selected, second.data)
            await pilot.press('down', 'enter')
            self.assertIs(app.focused, recent)
            self.assertEqual(app.selected, recent.visible_rows[0].key)
            await pilot.press('up', 'enter')
            self.assertIs(app.focused, tree)
            self.assertEqual(app.selected, groups[-1].children[-1].data)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            tree.get_node_at_line(0)
            self.assertEqual(tree.virtual_size.height, 10)  # Three headings, five items, two separators.
            self.assertEqual(tree._spacer_lines, {app.groups[name]._line - 1
                                               for name in ('Needs attention', 'Eligible')})
            first, second = app.groups['Eligible'].children
            self.assertEqual(second._line, first._line + 1)
            self.assertEqual(tree._get_label_region(first._line).height, 1)
            await check_heading_separators()
            tree.move_cursor(second)
            await pilot.press('down')
            self.assertIs(app.focused, recent)
            await pilot.press('up')
            self.assertIs(app.focused, tree)
            self.assertIs(tree.cursor_node, second)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_heading_separators_follow_visible_sections_and_idle_across_refresh_and_resize(self):
        self.state['assignment'] = None
        eligible = [{'item': 22, 'agent': 'worker', 'state': 'ready'}]
        attention = [{'item': 20, 'agent': 'worker', 'state': 'blocked'}]
        self.state['latest_pass'] = {'state': 'complete', 'rows': eligible}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(140, 44)) as pilot:
            await self.ready(app, pilot, lambda: 'plan:22' in app.nodes)
            tree = app.query_one(Tree)
            app.select('plan:22')
            tree.focus()
            tree.move_cursor(app.nodes['plan:22'])
            for size in ((140, 44), (80, 24), (140, 44)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                for displayed in (False, True, False):
                    self.state['latest_pass']['rows'] = attention + eligible if displayed else eligible
                    publish_snapshot(self.path, self.state)
                    await self.ready(app, pilot, lambda: ('Needs attention' in app.groups) == displayed)
                    tree.get_node_at_line(0)
                    names = ['Running', 'Needs attention', 'Eligible'] if displayed else ['Running', 'Eligible']
                    self.assertEqual([node.label.plain.split(' · ')[0] for node in tree.root.children], names)
                    self.assertEqual(app.selected, 'plan:22')
                    self.assertIs(tree.cursor_node, app.nodes['plan:22'])
                    self.assertIs(app.focused, tree)
                    self.assertEqual(app.groups['Running']._line, 0)
                    self.assertEqual(app.idle_node._line, 1)
                    self.assertEqual(app.groups[names[1]]._line, 3)
                    self.assertIsNone(tree.get_node_at_line(2))
                    self.assertEqual(tree._spacer_lines, {app.groups[name]._line - 1 for name in names[1:]})
                    height = 4 + tree.row_height + (2 + tree.row_height if displayed else 0)
                    self.assertEqual(tree.virtual_size.height, height)
                    # Repeated snapshot refreshes rebuild from logical nodes, not prior spacers.
                    for _ in range(2):
                        app.populate(app.pane)
                        await pilot.pause()
                        tree.get_node_at_line(0)
                        self.assertEqual(tree.virtual_size.height, height)
                        self.assertEqual(tree._spacer_lines, {app.groups[name]._line - 1 for name in names[1:]})
                        self.assertEqual(app.selected, 'plan:22')
                        self.assertIs(tree.cursor_node, app.nodes['plan:22'])
                # With both optional sections hidden, only Running and its idle line remain.
                app.select('outcome:previous-run')
                self.state['latest_pass']['rows'] = []
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: list(app.groups) == ['Running'])
                tree.get_node_at_line(0)
                self.assertEqual(tree.virtual_size.height, 2)
                self.assertEqual(tree._spacer_lines, set())
                self.state['latest_pass']['rows'] = eligible
                publish_snapshot(self.path, self.state)
                await self.ready(app, pilot, lambda: 'Eligible' in app.groups)
                app.select('plan:22')
                tree.move_cursor(app.nodes['plan:22'])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_live_rows_are_two_clipped_lines_at_110_by_32_and_mouse_selects_either(self):
        self.state['assignment'].update(kind='issue', title='A long assignment title ' * 5, attempt=1)
        self.state['latest_pass']['rows'][1].update(kind='pr', title='Foreign work')
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'kind': 'issue', 'title': 'Blocked work',
             'state': 'blocked', 'reason': 'Full blocker detail stays on Issue'},
            {'item': 21, 'agent': 'worker', 'kind': 'issue', 'title': 'Exhausted work',
             'state': 'blocked', 'reason': 'Attempt limit exhausted; inspect failures',
             'failures': 3, 'max_attempts': 3},
            {'item': 22, 'agent': 'worker', 'kind': 'pr', 'title': 'Eligible work', 'state': 'ready'},
        ])
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one('#work', Tree)
            tree.get_node_at_line(0)
            self.assertEqual(tree.virtual_size.height, 17)  # Three headings, five items, four gaps.
            # Building the rows queues scrollbar layout; wait for that refresh
            # before checking geometry, including under the parallel suite.
            await self.settled(app, tree)
            self.assertTrue(tree.show_vertical_scrollbar)
            self.assertEqual(tree.styles.overflow_x, 'hidden')
            # A busy runner can need more than one layout pass to drop the scrollbar.
            await self.ready(app, pilot, lambda: not tree.show_horizontal_scrollbar)
            self.assertFalse(tree.show_horizontal_scrollbar)
            width = tree.scrollable_content_region.width
            expected = [('assignment:owned-run', ' #114', '00:00', '  implementer · this launcher'),
                        ('plan:12', '● ⌥12', 'next', '  reviewer'),
                        ('plan:20:worker', '! #20', '', '  worker · blocked'),
                        ('plan:21:worker', '✗ #21', '', '  worker · failed 3/3'),
                        ('plan:22', '● ⌥22', 'ready', '  worker')]
            for key, prefix, status, detail in expected:
                node = app.nodes[key]
                self.assertFalse(node.children)
                self.assertIs(tree.get_node_at_line(node._line + 1), node)
                first = tree.render_line(node._line - tree.scroll_offset.y)
                second = tree.render_line(node._line + 1 - tree.scroll_offset.y)
                self.assertEqual(first.cell_length, width)
                self.assertEqual(second.cell_length, width)
                if key.startswith('assignment:'):
                    self.assertIn(first.text[0], '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
                    self.assertTrue(first.text[1:].startswith(prefix), first.text)
                    self.assertRegex(first.text, r'\d\d:\d\d$')
                    self.assertIn('…', first.text)
                    self.assertEqual(second.text.rstrip(),
                                     pane_line('  implementer · this launcher · attempt 1', width).plain)
                else:
                    self.assertTrue(first.text.startswith(prefix), first.text)
                    self.assertTrue(first.text.endswith(status), first.text)
                self.assertTrue(second.text.startswith(detail), second.text)
                if '⌥' in prefix:
                    marker = first.crop(2, 3)
                    number = first.crop(3, 4)
                    self.assertEqual(next(iter(marker)).style.color, theme_style(app, 'view-accent').color)
                    self.assertNotEqual(next(iter(number)).style.color, theme_style(app, 'view-accent').color)
            own, blocked = app.nodes['assignment:owned-run'], app.nodes['plan:20:worker']
            tree.focus()
            tree.move_cursor(own)
            await pilot.press('down')
            self.assertIs(tree.cursor_node, blocked)
            await pilot.press('up')
            self.assertIs(tree.cursor_node, own)
            for offset in (0, 1):
                await pilot.click('#work', offset=(3, blocked._line + offset - tree.scroll_offset.y))
                self.assertEqual(app.selected, blocked.data)
                self.assertIs(tree.cursor_node, blocked)
            # Empty space after a short metadata line still belongs to its row.
            await pilot.click('#work', offset=(width - 2, blocked._line + 1 - tree.scroll_offset.y))
            self.assertEqual(app.selected, blocked.data)
            await self.ready(app, pilot, lambda: 'Full blocker detail stays on Issue' in
                             app.query_one('#issue_text', Static).render().plain)
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_populate_identical_result_leaves_work_and_recent_untouched(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one(Tree)
            await self.settled(app, tree)
            work, recent = app.query_one('#work_pane'), app.query_one(RecentActivity)
            lines = tree._tree_lines_cached
            updates = {key: node._updates for key, node in app.nodes.items()}
            with (patch.object(tree, 'refresh', wraps=tree.refresh) as refresh,
                  patch.object(tree, '_build', wraps=tree._build) as build,
                  patch.object(tree, '_invalidate', wraps=tree._invalidate) as invalidate,
                  patch.object(tree, '_clear_line_cache', wraps=tree._clear_line_cache) as clear,
                  patch.object(tree, 'call_later', wraps=tree.call_later) as later,
                  patch.object(work, 'refresh', wraps=work.refresh) as work_refresh,
                  patch.object(recent, 'refresh', wraps=recent.refresh) as recent_refresh):
                for _ in range(10):
                    # Each worker result has fresh objects, even if its data is identical.
                    app.session = Session(self.path, json.loads(json.dumps(app.session.data)))
                    pane = work_pane(app.session, self.root, app.pane, app.selected, app.chosen)
                    app.populate(pane)
                for call in (refresh, build, invalidate, clear, later, work_refresh, recent_refresh):
                    call.assert_not_called()
                self.assertIs(tree._tree_lines_cached, lines)
                self.assertEqual({key: node._updates for key, node in app.nodes.items()}, updates)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_populate_repaints_changed_row_content_and_next_without_tick(self):
        now = datetime.now(timezone.utc)
        self.state['assignment']['attempt'] = 1
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready', 'title': 'Work', 'priority': 'low'},
            {'item': 21, 'agent': 'worker', 'state': 'blocked', 'attention_reason': 'Old reason',
             'waiting_since': (now - timedelta(minutes=5)).isoformat()},
        ])
        publish_snapshot(self.path, self.state)
        with patch.object(View, 'tick', autospec=True, side_effect=View.tick) as tick:
            app = View(self.root, self.path)
            async with app.run_test(size=(160, 45)) as pilot:
                await self.ready(app, pilot)
                app.worker.close()
                app.worker.thread.join(2)
                tick.side_effect = None  # Only populate may request the following repaints.
                tree = app.query_one(Tree)
                await self.settled(app, tree)
                cases = [
                    ('assignment:owned-run', self.state['assignment'], {'title': 'New title'}, 0, 'New title'),
                    ('assignment:owned-run', self.state['assignment'], {'attempt': 2}, 1, 'attempt 2'),
                    ('plan:20', self.state['latest_pass']['rows'][-2], {'priority': 'urgent'}, 1, 'urgent'),
                    ('plan:20', self.state['latest_pass']['rows'][-2],
                     {'failures': 1, 'max_attempts': 3}, 1, '1/3 failures'),
                    ('plan:21:worker', self.state['latest_pass']['rows'][-1],
                     {'attention_reason': 'New reason'}, 1, 'New reason'),
                    ('plan:21:worker', self.state['latest_pass']['rows'][-1],
                     {'waiting_since': (now - timedelta(minutes=10)).isoformat()}, 0, '10m'),
                ]
                for key, data, changes, offset, expected in cases:
                    with self.subTest(changes=changes):
                        node = app.nodes[key]
                        data.update(changes)
                        app.session = Session(self.path, json.loads(json.dumps(self.state)))
                        pane = work_pane(app.session, self.root, app.pane, app.selected, app.chosen)
                        with patch.object(tree, '_refresh_node', wraps=tree._refresh_node) as refresh:
                            app.populate(pane)
                            await self.settled(app, tree)
                            self.assertIs(app.nodes[key], node)
                            self.assertEqual([call.args[0] for call in refresh.call_args_list], [node])
                        self.assertIn(expected, tree.render_line(node._line + offset - tree.scroll_offset.y).text)
                # A changed diagnostic that is absent from work_lines stays inert.
                self.state['assignment']['process_reason'] = 'Another supervisor observation'
                app.session = Session(self.path, json.loads(json.dumps(self.state)))
                with patch.object(tree, '_refresh_node', wraps=tree._refresh_node) as refresh:
                    app.populate(work_pane(app.session, self.root, app.pane, app.selected, app.chosen))
                    await self.settled(app, tree)
                    refresh.assert_not_called()
                node = app.nodes['plan:20']
                old_row = app.rows['plan:20']
                self.state['latest_pass']['rows'] = [
                    row for row in self.state['latest_pass']['rows'] if row['item'] != 12]
                app.session = Session(self.path, json.loads(json.dumps(self.state)))
                pane = work_pane(app.session, self.root, app.pane, app.selected, app.chosen)
                with patch.object(tree, '_refresh_node', wraps=tree._refresh_node) as refresh:
                    app.populate(pane)
                    await self.settled(app, tree)
                    self.assertIs(app.nodes['plan:20'], node)
                    self.assertEqual(app.rows['plan:20'], old_row)
                    self.assertIn(node, [call.args[0] for call in refresh.call_args_list])
                self.assertTrue(tree.render_line(node._line - tree.scroll_offset.y).text.endswith('next'))
                self.state['activity'] = {'state': 'stopping'}
                app.session = Session(self.path, json.loads(json.dumps(self.state)))
                app.populate(work_pane(app.session, self.root, app.pane, app.selected, app.chosen))
                await self.settled(app, tree)
                self.assertTrue(tree.render_line(node._line - tree.scroll_offset.y).text.endswith('held'))
                own = app.nodes['assignment:owned-run']
                self.assertTrue(tree.render_line(own._line - tree.scroll_offset.y).text.endswith('stopping'))
                self.assertIn('finishing run', tree.render_line(own._line + 1 - tree.scroll_offset.y).text)
                await pilot.press('q')
        app.worker.thread.join(2)

    async def test_populate_recent_display_rows_and_today_count_without_work_repaint(self):
        with patch.object(View, 'tick', autospec=True, side_effect=View.tick) as tick:
            app = View(self.root, self.path)
            async with app.run_test(size=(160, 45)) as pilot:
                await self.ready(app, pilot)
                app.worker.close()
                app.worker.thread.join(2)
                tick.side_effect = None
                tree, recent = app.query_one(Tree), app.query_one(RecentActivity)
                await self.settled(app, tree)
                outcome = self.state['outcomes'][0]
                cases = [({'title': 'New outcome'}, 'New outcome'),
                         ({'summary': 'New summary'}, 'New summary'),
                         ({'result': 'blocked'}, 'blocked'),
                         ({'handoff': 42}, 'opened ⌥42'),
                         ({'time': datetime.now(timezone.utc).isoformat()}, '1 today')]
                with patch.object(tree, 'refresh', wraps=tree.refresh) as work_refresh:
                    for changes, expected in cases:
                        with self.subTest(changes=changes):
                            outcome.update(changes)
                            app.session = Session(self.path, json.loads(json.dumps(self.state)))
                            with patch.object(recent, 'refresh', wraps=recent.refresh) as refresh:
                                app.populate(work_pane(app.session, self.root, app.pane, app.selected, app.chosen))
                                refresh.assert_called_once_with()
                                await self.settled(app, recent)
                            self.assertIn(expected, recent.render().plain)
                    # Count changes independently of the bounded recent rows.
                    pane = app.pane
                    app.session.data['outcomes'].append(dict(outcome, run='another-run'))
                    with patch.object(recent, 'refresh', wraps=recent.refresh) as refresh:
                        app.populate(pane)
                        refresh.assert_called_once_with()
                        await self.settled(app, recent)
                    self.assertIn('2 today', recent.render().plain)
                    work_refresh.assert_not_called()
                await pilot.press('q')
        app.worker.thread.join(2)

    async def test_tick_refreshes_only_spinner_nodes_between_whole_tree_seconds(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            tree = app.query_one('#work', Tree)
            own = app.nodes['assignment:owned-run']
            app._tree_second = None
            with (patch.object(tree, '_refresh_line', wraps=tree._refresh_line) as line_refresh,
                  patch.object(tree, 'refresh', wraps=tree.refresh) as refresh,
                  patch('ub_agents.view_ui.time.monotonic') as clock):
                for tick in range(11):
                    clock.return_value = 1000 + tick / 10
                    app.tick()
                self.assertEqual(line_refresh.call_count, 18)
                self.assertEqual([call.args[0] for call in line_refresh.call_args_list],
                                 [own._line, own._line + 1] * 9)
                full_refreshes = [call for call in refresh.call_args_list if not call.args]
                self.assertEqual(len(full_refreshes), 2)
                regions = [call.args[0] for call in refresh.call_args_list if call.args]
                self.assertEqual(len(regions), 18)  # Both lines of the own-run node.
                self.assertEqual({region.y for region in regions},
                                 {own._line - tree.scroll_offset.y, own._line + 1 - tree.scroll_offset.y})
                self.assertTrue(all(not call.kwargs.get('layout') for call in refresh.call_args_list))
                app.session.data['activity']['state'] = 'stopping'
                clock.return_value = 1001.1
                refresh.reset_mock()
                line_refresh.reset_mock()
                app.tick()
                refresh.assert_not_called()
                line_refresh.assert_not_called()
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_whole_tree_tick_updates_attention_waiting_time_without_a_snapshot(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state['assignment'] = None
        self.state['activity'] = {'state': 'idle'}
        self.state['latest_pass']['rows'] = [
            {'item': 20, 'agent': 'worker', 'state': 'blocked',
             'waiting_since': (now - timedelta(seconds=59)).isoformat()}]
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot, lambda: 'plan:20:worker' in app.nodes)
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            tree = app.query_one('#work', Tree)
            node = app.nodes['plan:20:worker']
            nodes = tuple(app.nodes.items())
            def displayed():
                strips = app.screen._compositor.render_strips()
                y = tree.region.y + node._line - tree.scroll_offset.y
                return strips[y].crop(tree.region.x, tree.region.x + tree.scrollable_content_region.width).text
            # Keep the real Textual clock running while controlling only synchronous ticks.
            with patch.object(app.descriptions, 'clock', return_value=now.timestamp()) as waiting_clock:
                with patch('ub_agents.view_ui.time.monotonic', return_value=1000):
                    app.tick()
                await pilot.pause(0.1)
                self.assertTrue(displayed().endswith('0m'), displayed())
                waiting_clock.return_value = (now + timedelta(seconds=1)).timestamp()
                with patch('ub_agents.view_ui.time.monotonic', return_value=1001):
                    app.tick()
                await pilot.pause(0.1)
                self.assertTrue(displayed().endswith('1m'), displayed())
                self.assertEqual(tuple(app.nodes.items()), nodes)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_assignment_spinner_and_timer_refresh_without_rebuilding_or_moving_rows(self):
        self.state['latest_pass']['rows'].extend(
            {'item': item, 'agent': 'worker', 'state': 'ready'} for item in range(20, 40))
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one('#work', Tree)
            now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
            stamp = (now - timedelta(minutes=4, seconds=12)).isoformat()
            # Hold the current snapshot, as when a filesystem read is slow.
            app.worker.close()
            app.worker.thread.join(2)
            app.busy = True
            while not app.worker.results.empty():
                app.worker.results.get_nowait()
            own = 'assignment:owned-run'
            other = 'plan:12'
            app.rows[own] = replace(app.rows[own], data={
                **app.rows[own].data,
                'history': {'runs': [{'agent': 'implementer', 'time': stamp}]}})
            tree.claim_times.clear()
            tree.remember_claims(app.rows)
            tree.focus()
            tree.move_cursor(app.nodes[other])
            app.select(other)
            tree.scroll_to(y=1, animate=False, immediate=True)
            await pilot.pause()
            self.assertEqual(tree.scroll_y, 1)
            nodes = tuple(app.nodes.items())
            lines = tree._tree_lines_cached
            position = (app.selected, tree.cursor_node, tree.scroll_offset, app.focused, tree.virtual_size)
            self.assertEqual(tree.virtual_size.height, 34)  # Two headings, 11 items, ten gaps.
            def displayed(key=own, detail=False):
                line = app.nodes[key]._line + int(detail) - tree.scroll_offset.y
                strips = app.screen._compositor.render_strips()
                return strips[tree.region.y + line].crop(tree.region.x,
                                                        tree.region.x + tree.scrollable_content_region.width).text
            with patch('ub_agents.view_work.datetime') as clock:
                clock.fromisoformat = datetime.fromisoformat
                clock.now.return_value = now
                tree.refresh()
                await pilot.pause(0.1)
                self.assertTrue(displayed().endswith('04:12'), displayed())
                first, detail = displayed(), displayed(detail=True)
                static = (displayed(other), displayed(other, detail=True))
                self.assertTrue(first.startswith('⠋'))
                clock.now.return_value = now + timedelta(milliseconds=100)
                # Only View.tick drives the next frame; WorkTree has no timer.
                await pilot.pause(0.15)
                self.assertEqual(displayed(), '⠙' + first[1:])
                self.assertEqual(displayed(detail=True), detail)
                self.assertEqual((displayed(other), displayed(other, detail=True)), static)
                self.assertEqual(tuple(app.nodes.items()), nodes)
                self.assertIs(tree._tree_lines_cached, lines)
                self.assertEqual((app.selected, tree.cursor_node, tree.scroll_offset, app.focused,
                                  tree.virtual_size), position)
                clock.now.return_value = now + timedelta(seconds=1)
                await pilot.pause(0.15)
                self.assertTrue(displayed().endswith('04:13'), displayed())
                # A report replaces history.time with its outcome timestamp.
                # It must not reset an already observed claim timer.
                app.rows[own] = replace(app.rows[own], data={
                    **app.rows[own].data,
                    'history': {'runs': [{'agent': 'implementer', 'time': now.isoformat(),
                                          'acceptance': 'unaccepted'}]}})
                tree.remember_claims(app.rows)
                tree.refresh()
                await pilot.pause(0.1)
                self.assertTrue(displayed().endswith('04:13'), displayed())
                app.session.data['activity']['state'] = 'stopping'
                tree.refresh()  # Stand in for the new snapshot's populate.
                await pilot.pause(0.15)
                stopped = displayed()
                self.assertTrue(stopped.startswith('■'))
                self.assertTrue(stopped.endswith('stopping'))
                clock.now.return_value = now + timedelta(seconds=1, milliseconds=100)
                await pilot.pause(0.15)
                self.assertEqual(displayed(), stopped)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_recent_activity_today_count_enter_refresh_and_paused_outcome_log(self):
        now = datetime.now(timezone.utc)
        self.state['outcomes'][0]['time'] = now.isoformat()
        self.state['outcomes'].insert(0, dict(self.state['outcomes'][0], run='older-run',
                                           time=(now - timedelta(days=2)).isoformat()))
        publish_snapshot(self.path, self.state)
        previous = self.root / '.ub-agents' / 'runs' / 'previous-run'
        previous.mkdir()
        log = previous / 'process.log'
        log.write_bytes(b''.join(event(i) for i in range(600)))
        (previous / 'context.json').write_text(json.dumps({
            'title': 'Earlier work', 'body': '# Earlier work\n\n**Outcome context**'}))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            recent = app.query_one(RecentActivity)
            self.assertTrue(recent.render().plain.splitlines()[0].startswith('Recent activity · 1 today'))
            self.assertEqual([row.key for row in recent.visible_rows], ['outcome:previous-run', 'outcome:older-run'])
            recent.focus()
            await pilot.press('enter')
            key = 'outcome:previous-run'
            self.assertEqual(app.selected, key)
            await self.ready(app, pilot)
            markdown = app.query_one('#issue_body', Markdown)
            await self.ready(app, pilot, lambda: len(markdown.query('MarkdownH1')) == 1)
            self.assertIn('Outcome context', markdown.source)
            output = app.query_one(LogPane)
            for name in ('f', 'home', 'pagedown'):
                await pilot.press(name)
                await self.settled(app, output)
            page, anchor = app.reading.page, output.anchor()
            with log.open('ab') as stream:
                stream.write(event(9000))
            await self.ready(app, pilot, lambda: app.reading.latest != page)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            recent.focus()
            await pilot.press('down', 'enter')
            self.assertEqual(app.selected, 'outcome:older-run')
            await pilot.press('up', 'enter')
            await self.ready(app, pilot, lambda: 'Outcome context' in markdown.source)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            # Neither Enter nor the tree's expansion keys hide recent rows.
            await pilot.press('enter', 'left', 'right', 'space')
            self.assertEqual(app.selected, key)
            self.assertEqual(len(recent.visible_rows), 2)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_description_requests_local_sources_cache_and_navigation(self):
        self.state['latest_pass']['rows'].append({
            'item': 15, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched',
            'description': {'available': True, 'text': 'Cached description'}})
        publish_snapshot(self.path, self.state)
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('g', '2', 'g')
            self.assertEqual(transport.calls, [])  # run context is already present
            self.assertIn('Source: run context.json', app.last_context)
            self.assertIn('s old', app.last_context)
            own_key = app.selected
            plan_key = next(k for k, row in app.rows.items() if row.item == 15)
            app.select(plan_key)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            self.assertIn('Cached description', app.last_context)
            self.assertEqual(transport.calls, [])
            foreign = next(k for k, row in app.rows.items() if row.item == 12)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('1', 'g', '3', 'g', '2')
            await pilot.resize_terminal(120, 35)
            await pilot.pause(0.4)  # redraws and local timers
            self.assertEqual(transport.calls, [])
            await pilot.press('g')
            self.assertEqual(transport.calls, [('example/repo', 12)])
            self.assertIn('Loading title/body', app.last_context)
            await pilot.press('g', 'g')
            app.select(own_key)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('2', 'g', '3', '1', 'f', 'u')
            # Logs keep ingesting while the independent GitHub request is hung.
            before = app.reading.log.total_entries
            with self.log.open('ab') as stream:
                stream.write(event(9000))
            await self.ready(app, pilot, lambda: app.reading.log.total_entries > before)
            self.assertEqual(len(transport.calls), 1)
            transport.response = Response('Loaded title', 'Loaded body')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            for _ in range(3):
                app.select(foreign)
                await self.ready(app, pilot, lambda: app.local_description is not None)
                await pilot.press('2', 'g', '3', '2')
                self.assertIn('Loaded body', app.last_context)
                self.assertIn('Source: GitHub', app.last_context)
                app.select(own_key)
                await self.ready(app, pilot, lambda: app.local_description is not None)
            self.assertEqual(len(transport.calls), 1)
            # A shortened snapshot is available, including an empty body.
            self.state['latest_pass']['rows'][0]['description'] = {
                'available': True, 'text': '', 'omitted_characters': 1000}
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.local_description.source == 'snapshot')
            await pilot.press('2', 'g')
            self.assertIn('Description shortened in snapshot', app.last_context)
            self.assertEqual(len(transport.calls), 1)
            await pilot.press('q')
        self.assertTrue(transport.closed)
        app.worker.thread.join(2)

    async def test_description_failure_explicit_retry_and_global_cooldown(self):
        transport = RecordingDescriptionTransport()
        now = [1000]
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport, clock=lambda: now[0]))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            own = app.selected
            foreign = next(k for k, row in app.rows.items() if row.item == 12)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('2', 'g')
            transport.response = Response(error='Access denied')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('Access denied', app.last_context)
            self.assertIn('Press g on Issue to retry', app.last_context)
            app.select(own)
            await pilot.pause(0.3)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.pause(0.3)
            self.assertEqual(len(transport.calls), 1)
            await pilot.press('g')
            transport.response = Response(error='rate limit', reset=1100)
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('cooldown until', app.last_context)
            await pilot.press('g', 'g')
            other = next(k for k, row in app.rows.items() if row.item == 10)
            app.select(other)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            self.assertEqual(len(transport.calls), 2)
            now[0] = 1100
            await pilot.pause(0.3)
            self.assertEqual(len(transport.calls), 2)  # expiry never auto-retries
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            transport.response = Response('Recovered title', 'Recovered body')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('Recovered body', app.last_context)
            self.assertEqual(len(transport.calls), 3)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_quit_closes_pending_recording_transport_for_both_keys(self):
        for key in ('q', 'ctrl+c'):
            for attached in (False, True):
                with self.subTest(key=key, attached=attached):
                    transport = RecordingDescriptionTransport()
                    launcher = Mock() if attached else None
                    app = View(self.root, self.path, descriptions=DescriptionLoads(transport), launcher=launcher)
                    async with app.run_test(size=(110, 32)) as pilot:
                        await self.ready(app, pilot)
                        app.select(next(k for k, row in app.rows.items() if row.item == 12))
                        await self.ready(app, pilot, lambda: app.local_description is not None)
                        await pilot.press('2', 'g')
                        self.assertIsNotNone(app.descriptions.pending)
                        await pilot.press('?')
                        self.assertIsInstance(app.screen, KeyHelp)
                        await pilot.press(key)
                        if attached:
                            self.assertFalse(app._exit)
                            self.assertFalse(transport.closed)
                            app.exit()  # Launcher cleanup, rather than the key, closes the view.
                    self.assertTrue(transport.closed)
                    self.assertIsNone(app.descriptions.pending)
                    if attached:
                        if key == 'q':
                            launcher.drain.assert_called_once_with()
                            launcher.interrupt.assert_not_called()
                        else:
                            launcher.interrupt.assert_called_once_with()
                            launcher.drain.assert_not_called()
                        launcher.close.assert_called_once_with()
                    app.worker.thread.join(2)

    async def test_pause_survives_sustained_reader_and_render_eviction_tabs_panes_raw_resize(self):
        app = View(self.root, self.path)
        with patch('subprocess.Popen', side_effect=AssertionError('view invoked a process')):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                self.assertFalse(app.nodes[app.selected].children)
                self.assertIn('Supervisor observed running', app.rows[app.selected].reason)
                output = app.query_one('#output', LogPane)
                for name in ('f', 'home', 'pagedown'):
                    await pilot.press(name)
                    await self.settled(app, output)
                key = app.selected
                page, lines, anchor = app.reading.page, tuple(output.lines), output.anchor()
                self.assertFalse(app.reading.follow)
                # A burst beyond the 200-entry reader and 400-line renderer bounds.
                with self.log.open('ab') as stream:
                    stream.write(b''.join(event(i, 1800) for i in range(600, 1700)))
                await self.ready(app, pilot, lambda: app.reading.log and app.reading.log.evicted_entries > 200)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(tuple(output.lines), lines)
                self.assertEqual(output.anchor(), anchor)
                self.assertGreater(app.reading.log.total_entries - app.reading.seen, 200)
                self.assertIn('PAUSED', app.query_one('#log_state', Static).render().plain)
                focus = app.focused
                await pilot.press('2', '3', '1')
                await pilot.pause()
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                foreign = next(k for k, row in app.rows.items() if row.item == 12)
                app.select(foreign)
                await pilot.pause(0.3)
                self.assertIsNone(app.reading.page)
                self.assertEqual(app.reading.empty_message, 'No local log cached for this row.')
                self.assertNotIn('entries hidden', app.query_one('#log_note').render().plain)
                self.assertNotIn('evicted ', app.query_one('#log_note').render().plain)
                app.select(key)
                await pilot.pause(0.3)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                await pilot.press('u')
                await self.settled(app, output)
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.press('u')
                await self.settled(app, output)
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(130, 40)
                await pilot.pause()
                await self.settled(app, output)
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(110, 32)
                await pilot.press('f')
                await self.ready(app, pilot, lambda: app.reading.page != page)
                self.assertTrue(app.reading.follow)
                self.assertLessEqual(len(output.lines), MAX_RENDER_LINES)
                self.assertGreater(output.hidden, 0)
                self.assertNotIn('entries hidden', app.query_one('#log_note').render().plain)
                self.assertNotIn('evicted ', app.query_one('#log_note').render().plain)
                await pilot.press('p')
                raw = app.screen.query_one('#raw_details', Static).render().plain
                self.assertIn(f'Rendered limit {MAX_RENDER_LINES}: {output.hidden} entries hidden', raw)
                self.assertIn('bytes ', raw)
                self.assertIn('evicted ', raw)
                self.assertIn('skipped ', raw)
                self.assertIn('shortened ', raw)
                await pilot.press('escape')
                await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_raw_access_opens_with_current_status_when_an_update_lands_first(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            # A worker result can land between the push and the screen's compose.
            app.action_path()
            app.update_status()
            await self.ready(app, pilot, lambda: app.screen.query('#raw_status'))
            footer = app.screen.query_one('#raw_status', Static).render().plain
            self.assertIn('running assignment', footer)
            self.assertNotIn('FOLLOW', footer)
            self.assertIn('bytes ', app.screen.query_one('#raw_details', Static).render().plain)
            # An update can also land after compose but before the footer's first layout.
            with patch.object(Static, 'size', new_callable=PropertyMock, return_value=Size(0, 0)):
                app.update_status()
            footer = app.screen.query_one('#raw_status', Static).render().plain
            self.assertIn('running assignment', footer)
            self.assertIn('q quit', footer)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_history_navigation_and_paused_generation_notice(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            old_start = app.reading.page.start
            entered, release = threading.Event(), threading.Event()
            original = app.worker.read
            def slow_history(request):
                result = original(request)
                if request.older_end is not None:
                    entered.set()
                    release.wait(5)
                return result
            try:
                with patch.object(app.worker, 'read', side_effect=slow_history):
                    await pilot.press('h')
                    await self.ready(app, pilot, entered.is_set)
                    await pilot.press('f')
                    release.set()
                    await pilot.pause(0.4)
                    self.assertTrue(app.reading.follow)
                    self.assertEqual(app.reading.page.start, old_start)
            finally:
                release.set()
            await pilot.press('h')
            await self.ready(app, pilot, lambda: app.reading.page.start < old_start)
            self.assertFalse(app.reading.follow)
            page = app.reading.page
            other = self.log.with_suffix('.next')
            other.write_bytes(event(9000))
            other.replace(self.log)
            await self.ready(app, pilot, lambda: app.reading.latest.generation != page.generation)
            self.assertEqual(app.reading.page, page)
            self.assertIn('FILE CHANGED', str(app.query_one('#log_note').render()))
            await pilot.press('h')
            self.assertIn('File changed', app.reading.notice)
            await pilot.press('f')
            self.assertIn('event 09000', app.reading.page.refs[-1].value.text)
            self.assertNotIn('event 00599', app.reading.page.refs[-1].value.text)
            await pilot.press('p')
            await self.ready(app, pilot, lambda: app.screen.__class__.__name__ == 'RawAccess'
                             and 'running assignment' in app.screen.query_one('#raw_status', Static).render().plain)
            self.assertNotIn('FOLLOW', app.screen.query_one('#raw_status', Static).render().plain)
            await pilot.press('pageup', 'pagedown', 'home', 'end', 'f', 'u', 'h', '2', '3', '1')
            self.assertEqual(app.screen.__class__.__name__, 'RawAccess')
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_row_picked_during_read_is_retained_from_the_last_drawn_pane(self):
        app = View(self.root, self.path)
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                output = app.query_one(LogPane)
                output.focus()
                key = 'plan:12'
                original = app.worker.read

                def slow_read(request):
                    entered.set()
                    release.wait(5)
                    return original(request)

                with patch.object(app.worker, 'read', side_effect=slow_read):
                    await self.ready(app, pilot, entered.is_set)
                    self.assertIn(key, [row.key for row in app.pane.rows])
                    app.select(key)
                    self.state['latest_pass']['rows'] = []
                    publish_snapshot(self.path, self.state)
                    release.set()
                    await self.ready(app, pilot, lambda: key in app.rows and
                                     app.rows[key].state == 'earlier observation')
                    self.assertEqual(app.selected, key)
                    self.assertEqual(app.pane.selected, key)
                    self.assertEqual(app.groups['Eligible'].label.plain, 'Eligible · 1')
                    self.assertIs(app.focused, output)
                    self.assertIsNone(app.reading.page)
                await pilot.press('q')
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_picking_the_selected_run_supersedes_an_in_flight_follow(self):
        app = View(self.root, self.path)
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                key = app.selected
                original = app.worker.read

                def slow_read(request):
                    entered.set()
                    release.wait(5)
                    return original(request)

                with patch.object(app.worker, 'read', side_effect=slow_read):
                    await self.ready(app, pilot, entered.is_set)
                    self.assertEqual(app.selected, key)
                    app.select(key)
                    self.state['assignment']['run'] = 'next-run'
                    publish_snapshot(self.path, self.state)
                    release.set()
                    await self.ready(app, pilot, lambda: 'assignment:next-run' in app.nodes)
                    self.assertEqual(app.selected, key)
                    self.assertEqual(app.pane.selected, key)
                    self.assertEqual(app.rows[key].state, 'earlier observation')
                    self.assertNotIn(key, app.nodes)
                await pilot.press('q')
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_picking_selected_run_during_history_read_keeps_the_requested_page(self):
        app = View(self.root, self.path)
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                selected, old_start = app.selected, app.reading.page.start
                original = app.worker.read

                def slow_history(request):
                    result = original(request)
                    if request.older_end is not None:
                        entered.set()
                        release.wait(5)
                    return result

                with patch.object(app.worker, 'read', side_effect=slow_history):
                    await pilot.press('h')
                    await self.ready(app, pilot, entered.is_set)
                    app.select(selected)
                    release.set()
                    await self.ready(app, pilot, lambda: app.reading.page.start < old_start)
                    self.assertEqual(app.selected, selected)
                    self.assertFalse(app.reading.follow)
                await pilot.press('q')
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_slow_reads_do_not_block_keys_and_focus_and_snapshot_replacement_stay_stable(self):
        app = View(self.root, self.path)
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                output = app.query_one('#output', LogPane)
                output.focus()
                selected = app.selected
                tree = app.query_one('#work', Tree)
                node = app.nodes[selected]
                self.state['latest_pass']['state'] = 'complete'
                self.state['assignment']['process'] = 'exited'
                next_path = self.path.with_suffix('.next')
                next_path.write_text(json.dumps(self.state))
                next_path.replace(self.path)
                await self.ready(app, pilot, lambda: app.rows[selected].state == 'exited')
                self.assertEqual(app.selected, selected)
                self.assertIs(app.nodes[selected], node)
                self.assertIs(app.focused, output)
                original = app.worker.read
                def slow_read(request):
                    entered.set()
                    release.wait(5)
                    return original(request)
                with patch.object(app.worker, 'read', side_effect=slow_read):
                    await self.ready(app, pilot, entered.is_set)
                    await pilot.press('f', '2', '3', '1', 'u')
                    self.assertFalse(app.reading.follow)
                    self.assertTrue(app.reading.raw)
                    self.assertEqual(app.query_one(TabbedContent).active, 'log')
                    await pilot.press('q')
                    release.set()
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_malformed_snapshot_retains_paused_page_and_content_at_minimum_size(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f')
            page = app.reading.page
            self.state['latest_pass']['state'] = '[/red]'
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.session.data.get('latest_pass', {}).get('state') == '[/red]')
            self.assertIn('[/red]', app.query_one('#work_pane').border_title)
            self.state['assignment'] = None
            publish_snapshot(self.path, self.state)
            await self.ready(app, pilot, lambda: app.rows[app.selected].state == 'earlier observation')
            self.assertIn('earlier observation', app.last_context)
            self.path.write_text('{broken')
            await self.ready(app, pilot, lambda: app.session.error is not None)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(app.rows[app.selected].state, 'earlier observation')
            self.assertIn('malformed', str(app.query_one('#status').render()))
            self.assertGreater(app.query_one('#output').size.height, 15)
            self.assertEqual(app.query_one('#output').size.width, 57)
            await pilot.press('ctrl+c')
        app.worker.thread.join(2)


if __name__ == '__main__':
    unittest.main()
