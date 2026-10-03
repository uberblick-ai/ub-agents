import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_view_data import event, fixture

HAS_UI = importlib.util.find_spec('textual') is not None
if HAS_UI:
    from textual.widgets import Static, TabbedContent, Tree
    from ub_agents.view_ui import LogPane, MAX_RENDER_LINES, View
    from ub_agents.view_worker import LocalWorker


@unittest.skipUnless(HAS_UI, 'install the opt-in UI extra')
class ViewUITests(unittest.IsolatedAsyncioTestCase):
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

    async def test_pause_survives_sustained_reader_and_render_eviction_tabs_panes_raw_resize(self):
        app = View(self.root, self.path)
        with patch('subprocess.Popen', side_effect=AssertionError('view invoked a process')):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                self.assertIn('Supervisor observed running', app.reason_nodes[app.selected].label.plain)
                output = app.query_one('#output', LogPane)
                await pilot.press('f', 'home', 'pagedown')
                await pilot.pause()
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
                self.assertIn('PAUSED', str(app.query_one('#status', Static).render()))
                focus = app.focused
                await pilot.press('2', '3', '1')
                await pilot.pause()
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                foreign = next(k for k, row in app.rows.items() if row.item == 12)
                app.select(foreign)
                await pilot.pause(0.3)
                self.assertIsNone(app.reading.page)
                self.assertIn('Owner: @other-launcher', str(app.query_one('#log_note').render()))
                app.select(key)
                await pilot.pause(0.3)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                await pilot.press('u')
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.press('u')
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(130, 40)
                await pilot.pause()
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(110, 32)
                await pilot.press('f')
                await self.ready(app, pilot, lambda: app.reading.page != page)
                self.assertTrue(app.reading.follow)
                self.assertLessEqual(len(output.lines), MAX_RENDER_LINES)
                self.assertGreater(output.hidden, 0)
                self.assertIn('entries hidden', str(app.query_one('#log_note').render()))
                await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

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
            await pilot.pause()
            self.assertEqual(app.screen.__class__.__name__, 'RawAccess')
            self.assertIn('FOLLOW', str(app.screen.query_one('#raw_status').render()))
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
            self.assertIn('[/red]', app.groups['Latest pass'].label.plain)
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
