import asyncio
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from rich.console import Console
from tests.test_view_data import event, fixture
from tests.test_log_reader import FIXTURE, record, result, tool
from tests.test_view_github import reply
from tests.support import RecordingDescriptionTransport
from ub_agents.view_github import DescriptionLoads, Response, parse_response

from textual.widgets import Markdown, Static, TabbedContent, TabPane, Tree
from ub_agents.view_ui import (KeyHelp, LogPane, MAX_RENDER_LINES, RECENT_ACTIVITY,
                               RawAccess, UpdateBanner, View, pane_line)
from ub_agents.view_worker import LocalWorker


class ViewUITests(unittest.IsolatedAsyncioTestCase):
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
                    {'text': '⬆ ub-agents 0.1.12 is available · you run 0.1.11 · brew upgrade ub-agents, '
                             'then restart the launcher',
                     'released_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()},
                    {'text': '⬆ This launcher runs code 2 commits behind origin/main · restart the launcher'},
                    {'text': '[bold]inert[/bold]\nnext\x1b[31m'}):
                self.state['update'] = value
                self.path.write_text(json.dumps(self.state))
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
            self.path.write_text(json.dumps(self.state))
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
        self.log.write_bytes(b''.join(recorded) + edit + b''.join(event(i, 20) for i in range(30)))
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
            self.assertTrue(any('+2' in segment.text and segment.style.color.name == 'green' for segment in segments))
            self.assertTrue(any('-1' in segment.text and segment.style.color.name == 'red' for segment in segments))
            self.assertTrue(any('✗' in segment.text and segment.style.color.name == 'red' for segment in segments))
            self.assertTrue(any('· thinking' in segment.text and segment.style.dim for segment in segments))
            self.assertTrue(any('SPIKE111_MESSAGE_BEGIN' in segment.text and segment.style.italic for segment in segments))
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
            self.path.write_text(json.dumps(self.state))
        elif source == 'run context.json':
            (self.log.parent / 'context.json').write_text(json.dumps({'title': title, 'body': body}))
        else:
            (self.log.parent / 'context.json').unlink()
        transport = RecordingDescriptionTransport()
        transport.response = parse_response(reply(title, body), b'', 0, 1000)
        return transport

    async def test_one_line_footer_version_activity_and_session_diagnostics(self):
        now = datetime.now(timezone.utc)
        self.state['base_version'] = '9.8.7'
        self.state['activity'] = {'state': 'waiting', 'until': (now + timedelta(seconds=30)).isoformat()}
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        with patch('ub_agents.view_ui.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                footer = app.query_one('#status', Static)
                keys = '↑↓ select ⏎ open 1-3 tabs ? keys q quit'
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
                    self.path.write_text(json.dumps(self.state))
                    await self.ready(app, pilot, lambda: f'· {state}' in footer.render().plain)
                    self.assertTrue(footer.render().plain.endswith(keys))
                self.state['published_at'] = (now - timedelta(seconds=60)).isoformat()
                self.path.write_text(json.dumps(self.state))
                await self.ready(app, pilot, lambda: 'stale' in footer.render().plain)
                self.assertNotIn('snapshot', footer.render().plain)
                self.state['ended'] = True
                self.path.write_text(json.dumps(self.state))
                await self.ready(app, pilot, lambda: 'ended' in footer.render().plain)
                self.path.write_text('{broken')
                await self.ready(app, pilot, lambda: 'malformed:' in footer.render().plain)
                self.assertIn(app.session.error[:20], footer.render().plain)
                await pilot.resize_terminal(80, 24)
                app.update_status()
                self.assertIn('minimum 110×32', footer.render().plain)
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
            self.assertTrue(footer.render().plain.endswith('f follow h older u raw PgUp/PgDn scroll ? keys q quit'))
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
                self.assertNotIn('PAUSED', footer.render().plain)
            await pilot.press('1', 'f')
            await self.ready(app, pilot, lambda: not pill.display)
            self.assertTrue(app.reading.raw)
            self.assertTrue(footer.render().plain.endswith('↑↓ select ⏎ open 1-3 tabs ? keys q quit'))
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
        self.path, self.log, self.state = fixture(self.root, runtime='codex:synthetic-model:high')
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            note = app.query_one('#log_note', Static)
            self.assertEqual(note.render().plain, 'codex: plain/raw fallback')
            self.log.write_bytes(b'unfinished record')
            await self.ready(app, pilot, lambda: 'Unfinished:' in note.render().plain)
            self.assertIn('plain/raw fallback', note.render().plain)
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
        self.path.write_text(json.dumps(self.state))
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
                style = value.get_style_at_offset(len(title) + 1)
                self.assertTrue(style.dim)
                self.assertFalse(style.bold)
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

    async def test_log_status_follows_process_and_report_with_right_aligned_history(self):
        self.state['outcomes'].extend([
            {'item': 114, 'run': 'before', 'agent': 'preparer'},
            {'item': 114, 'run': 'other', 'agent': 'implementer'}])
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            status = app.query_one('#run_status', Static)
            lines = status.render().plain.split('\n')
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], '┄' * status.size.width)
            self.assertIn('implementer running · no outcome reported', lines[1])
            self.assertIn(lines[1][0], '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
            self.assertTrue(lines[1].endswith('2 earlier runs'))
            self.assertEqual(pane_line(lines[1], 1000).cell_len, status.size.width)
            self.assertEqual(status.region.bottom, app.query_one('#log').region.bottom)
            self.assertEqual(status.region.bottom, app.query_one('#status').region.y)
            self.assertFalse(app.query_one('#log_note').display)
            self.state['assignment']['process'] = 'exited'
            self.state['outcomes'].append({'item': 114, 'run': 'owned-run', 'result': 'success',
                                           'acceptance': 'finalized'})
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: 'finalized' in status.render().plain)
            self.assertIn('implementer exited · reported success (finalized)', status.render().plain)
            self.assertTrue(status.render().plain.endswith('2 earlier runs'))
            await pilot.resize_terminal(65, 25)
            await pilot.pause()
            for widget in (status, app.query_one('#item_header', Static)):
                self.assertTrue(all(pane_line(line, 1000).cell_len <= widget.content_region.width
                                    for line in widget.render().plain.split('\n')))
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_notice_is_one_highlighted_line_only_for_exceptional_log_states(self):
        self.path, self.log, self.state = fixture(self.root, runtime='codex:model:high')
        self.log.write_bytes(b'unfinished raw fragment')
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            note = app.query_one('#log_note', Static)
            self.assertEqual(note.size.height, 1)
            self.assertIn('Unfinished:', note.render().plain)
            self.assertIn('plain/raw fallback', note.render().plain)
            self.assertTrue(any(span.style == 'bold yellow' for span in note.render().spans))
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

    async def test_sections_counts_hidden_empty_sections_and_dim_partial_marker(self):
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
            {'item': 21, 'agent': 'worker', 'state': 'blocked', 'reason': 'Cleanup unconfirmed'},
            {'item': 22, 'agent': 'worker', 'state': 'waiting', 'reason': 'Runtime paused'},
        ])
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one('#work', Tree)
            self.assertEqual([node.label.plain for node in tree.root.children[:4]],
                             ['Running · 2', 'Needs attention · 1', 'Eligible · 1', 'Waiting · 1'])
            self.assertEqual(sum(row.item == 114 for row in app.rows.values()), 1)
            self.assertIn('partial', tree.root.label.plain)
            self.assertTrue(any(span.style == 'dim' for span in tree.root.label.spans))
            self.assertFalse(app.groups['Recent activity'].is_expanded)
            self.state['latest_pass'] = {'state': 'complete', 'rows': []}
            self.state['outcomes'] = []
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: list(app.groups) == ['Running'])
            self.assertEqual(app.groups['Running'].label.plain, 'Running · 1')
            self.assertEqual(tree.root.label.plain, 'Launcher work')
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
            app.select('plan:12:reviewer')
            await self.ready(app, pilot, lambda: '⌥12 Foreign candidate' in identity())
            self.assertNotIn('Foreign candidate', displayed())
            self.assertIn('closes #11 · 4 runs', displayed())
            self.assertIn('3 earlier runs omitted.', displayed())
            self.assertNotIn('filed by', displayed())
            self.assertNotIn('#114', displayed())
            self.state['histories']['12']['title'] = 'Updated candidate'
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: '⌥12 Updated candidate' in identity())
            self.state['latest_pass']['rows'] = []
            self.state['histories'].pop('12')
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.rows[app.selected].state == 'earlier observation')
            self.assertIn('⌥12 Updated candidate', identity())
            app.select(RECENT_ACTIVITY)
            self.assertIn('Select an item', displayed())
            app.select('outcome:previous-run')
            await self.ready(app, pilot, lambda: '#10 Earlier item' in identity())
            self.assertEqual(transport.calls, [])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_cursor_follows_reason_when_tree_changes_before_a_render(self):
        self.state['latest_pass']['rows'].append(
            {'item': 21, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'})
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            key = 'plan:21:worker'
            app.select(key)
            tree = app.query_one('#work', Tree)
            tree.move_cursor(app.reason_nodes[key])
            old_reason = tree.cursor_node
            changed = [replace(row, group='Waiting', state='parked', reason='Waiting for blockers #31')
                       if row.key == key else row for row in app.rows.values()]
            app.populate(changed)
            # Assert before yielding to Textual's next layout or idle callback.
            self.assertIsNot(old_reason, app.reason_nodes[key])
            self.assertIs(tree.cursor_node, app.reason_nodes[key])
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_view_follows_a_run_that_starts_after_it_opens_until_a_row_is_picked(self):
        assignment = self.state['assignment']
        self.state['assignment'] = None
        self.state['latest_pass']['rows'][0]['state'] = 'ready'
        self.path.write_text(json.dumps(self.state))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot, lambda: app.rows)
            self.assertTrue(app.selected.startswith('plan:'))
            self.state['assignment'] = assignment
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.selected == 'assignment:owned-run')
            self.assertNotIn('plan:114:implementer', app.rows)
            app.select('plan:12:reviewer')
            self.state['assignment'] = dict(assignment, run='next-run')
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: 'assignment:next-run' in app.rows)
            self.assertEqual(app.selected, 'plan:12:reviewer')
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_selected_plan_survives_section_order_change_and_disappearance(self):
        description = {'available': True, 'text': '# Planned work\n\n**Cached body**'}
        self.state['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
            {'item': 21, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched',
             'description': description},
        ])
        self.path.write_text(json.dumps(self.state))
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
            app.move_cursor(app.reason_nodes[key])
            output.focus()
            self.assertIs(tree.cursor_node, app.reason_nodes[key])
            self.state['latest_pass']['rows'] = [
                {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for blockers #31',
                 'description': description}]
            self.path.write_text(json.dumps(self.state))
            # The tree moves its cursor back to the kept node after the next refresh.
            await self.ready(app, pilot, lambda: app.nodes[key].parent is app.groups.get('Waiting')
                             and tree.cursor_node is app.reason_nodes[key])
            self.assertEqual(app.selected, key)
            self.assertIs(app.focused, output)
            self.assertEqual(markdown.source, description['text'])
            self.assertNotIn('Eligible', app.groups)
            self.assertEqual([node.label.plain for node in tree.root.children[:2]],
                             ['Running · 1', 'Waiting · 1'])
            self.state['latest_pass']['rows'] = []
            self.path.write_text(json.dumps(self.state))
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

    async def test_recent_activity_today_count_enter_refresh_and_paused_outcome_log(self):
        now = datetime.now(timezone.utc)
        self.state['outcomes'][0]['time'] = now.isoformat()
        self.state['outcomes'].append(dict(self.state['outcomes'][0], run='older-run',
                                           time=(now - timedelta(days=2)).isoformat()))
        self.path.write_text(json.dumps(self.state))
        previous = self.root / '.ub-agents' / 'runs' / 'previous-run'
        previous.mkdir()
        log = previous / 'process.log'
        log.write_bytes(b''.join(event(i) for i in range(600)))
        (previous / 'context.json').write_text(json.dumps({
            'title': 'Earlier work', 'body': '# Earlier work\n\n**Outcome context**'}))
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            tree = app.query_one('#work', Tree)
            group = app.groups['Recent activity']
            self.assertEqual(group.label.plain, 'Recent activity · 1 today')
            self.assertEqual(len(group.children), 2)
            self.assertFalse(group.is_expanded)
            tree.focus()
            app.move_cursor(group)
            await self.settled(app, tree)
            await pilot.press('enter')
            await self.settled(app, tree)
            self.assertTrue(group.is_expanded)
            self.assertEqual(app.selected, RECENT_ACTIVITY)
            key = 'outcome:previous-run'
            tree.select_node(app.nodes[key])
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
            self.assertTrue(group.is_expanded)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            app.move_cursor(group)
            await self.settled(app, tree)
            await pilot.press('enter')
            await self.settled(app, tree)
            self.assertFalse(group.is_expanded)
            self.assertEqual(app.selected, RECENT_ACTIVITY)
            self.assertEqual(markdown.source, '')
            self.assertIs(tree.cursor_node, group)
            await pilot.pause(0.3)
            self.assertFalse(group.is_expanded)
            await pilot.press('enter')
            await self.settled(app, tree)
            tree.select_node(app.nodes[key])
            await self.ready(app, pilot, lambda: 'Outcome context' in markdown.source)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(output.anchor(), anchor)
            # Mouse/arrow collapse also selects the header when an outcome is selected.
            group.collapse()
            await pilot.pause()
            self.assertEqual(app.selected, RECENT_ACTIVITY)
            self.assertIs(tree.cursor_node, group)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_description_requests_local_sources_cache_and_navigation(self):
        self.state['latest_pass']['rows'].append({
            'item': 15, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched',
            'description': {'available': True, 'text': 'Cached description'}})
        self.path.write_text(json.dumps(self.state))
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
            self.path.write_text(json.dumps(self.state))
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
            transport = RecordingDescriptionTransport()
            app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                app.select(next(k for k, row in app.rows.items() if row.item == 12))
                await self.ready(app, pilot, lambda: app.local_description is not None)
                await pilot.press('2', 'g')
                self.assertIsNotNone(app.descriptions.pending)
                await pilot.press(key)
            self.assertTrue(transport.closed)
            self.assertIsNone(app.descriptions.pending)
            app.worker.thread.join(2)

    async def test_pause_survives_sustained_reader_and_render_eviction_tabs_panes_raw_resize(self):
        app = View(self.root, self.path)
        with patch('subprocess.Popen', side_effect=AssertionError('view invoked a process')):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                self.assertIn('Supervisor observed running', app.reason_nodes[app.selected].label.plain)
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
                self.assertIn('Owner: @other-launcher', app.reading.empty_message)
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
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.session.data.get('latest_pass', {}).get('state') == '[/red]')
            self.assertIn('[/red]', app.query_one('#work', Tree).root.label.plain)
            self.state['assignment'] = None
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.rows[app.selected].state == 'earlier observation')
            self.assertIn('earlier observation', app.last_context)
            self.path.write_text('{broken')
            await self.ready(app, pilot, lambda: app.session.error is not None)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(app.rows[app.selected].state, 'earlier observation')
            self.assertIn('malformed', str(app.query_one('#status').render()))
            self.assertGreater(app.query_one('#output').size.height, 15)
            self.assertGreater(app.query_one('#output').size.width, 60)
            await pilot.press('ctrl+c')
        app.worker.thread.join(2)


if __name__ == '__main__':
    unittest.main()
