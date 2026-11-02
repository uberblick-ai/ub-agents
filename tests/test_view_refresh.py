"""Headless acceptance checks for change-driven terminal refreshes."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Static

from tests.test_view_data import event, fixture, publish_snapshot
from ub_agents.view.ui import View
from ub_agents.view.widgets import ItemTabs, UpdateBanner
from ub_agents.view.worker import LocalWorker, Request


class ViewRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root, count=2)
        self.assignment = self.state['assignment']
        self.state['assignment'] = None
        self.state['activity'] = {'state': 'waiting'}
        self.state['latest_pass']['rows'] = [self.state['latest_pass']['rows'][1]]
        self.state['outcomes'].append(dict(self.assignment, completed=True, result='success'))
        self.state['histories']['114']['runs'][-1]['result'] = 'success'
        publish_snapshot(self.path, self.state)

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def ready(self, pilot, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail('View did not become ready')
            await pilot.pause(0.02)

    async def test_idle_timer_and_each_visible_spinner_choose_the_cadence(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.ready(pilot, lambda: app.local_description is not None)
            self.assertEqual(app._refresh_interval, 1)
            with patch.object(app.worker, 'request', wraps=app.worker.request) as requests:
                await pilot.pause(2.15)
                self.assertIn(requests.call_count, (2, 3))
            # A new assignment is discovered by the next idle read and its
            # result immediately changes cadence, without a second slow tick.
            app._refresh_timer.stop()
            app._refresh_timer = app.set_interval(1, app.tick)
            self.state['assignment'] = self.assignment
            self.state['activity'] = {'state': 'running assignment'}
            self.state['histories']['114']['runs'][-1].pop('result')
            publish_snapshot(self.path, self.state)
            started = time.monotonic()
            await self.ready(pilot, lambda: app._refresh_interval == 0.1)
            self.assertLess(time.monotonic() - started, 1.15)
            # Work is hidden here; only the selected Agent status spins.
            await pilot.resize_terminal(80, 24)
            await pilot.press('enter')
            self.assertTrue(app.item_view)
            self.assertEqual(app._refresh_interval, 0.1)
            await pilot.press('2')
            self.assertEqual(app._refresh_interval, 1)
            await pilot.press('3')
            self.assertEqual(app._refresh_interval, 0.1)
            await pilot.press('escape')
            self.assertEqual(app._refresh_interval, 0.1)
            self.state['assignment'] = None
            self.state['activity'] = {'state': 'waiting'}
            publish_snapshot(self.path, self.state)
            await self.ready(pilot, lambda: app._refresh_interval == 1)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_unchanged_snapshot_skips_panes_and_status_but_follows_log(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.ready(pilot, lambda: app.local_description is not None)
            app.select('outcome:owned-run')
            await self.ready(pilot, lambda: app.reading.page is not None)
            app._refresh_timer.stop()
            await self.ready(pilot, lambda: not app.busy)
            await pilot.pause(0.1)
            now = datetime.now(timezone.utc)
            with patch('ub_agents.view.ui.datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                app.update_status()
                with (patch.object(app, 'populate', wraps=app.populate) as populate,
                      patch.object(app, 'update_status', wraps=app.update_status) as status,
                      patch.object(app.query_one(UpdateBanner), 'set_banner') as banner):
                    for _ in range(3):
                        app.request_read()
                        await self.ready(pilot, lambda: not app.busy)
                    populate.assert_not_called()
                    status.assert_not_called()
                    banner.assert_not_called()
                    end = app.reading.page.end
                    with self.log.open('ab') as stream:
                        stream.write(event(99))
                    app.request_read()
                    await self.ready(pilot, lambda: app.reading.page.end > end)
                    self.assertIn('event 00099', '\n'.join(line.text for line in app.query_one('#output').lines))
                    populate.assert_not_called()
                    banner.assert_not_called()
                    status.assert_called_once()
                    status.reset_mock()
                    self.state['update'] = {'text': 'New version available'}
                    self.state['latest_pass']['rows'].append(
                        {'item': 15, 'agent': 'reviewer', 'state': 'ready'})
                    publish_snapshot(self.path, self.state)
                    app.request_read()
                    await self.ready(pilot, lambda: 'plan:15' in app.rows)
                    populate.assert_called_once()
                    banner.assert_called_once()
                    status.assert_called_once()
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_selection_tabs_follow_poll_and_resize_work_without_timer(self):
        launcher = Mock()
        app = View(self.root, self.path, launcher=launcher)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.ready(pilot, lambda: app.local_description is not None)
            app._refresh_timer.stop()
            app.select('outcome:owned-run')
            await self.ready(pilot, lambda: app.reading.page is not None)
            self.assertIn('#114', app.query_one('#item_header', Static).render().plain)
            await pilot.press('f')
            self.assertFalse(app.reading.follow)
            self.assertIn('f follow', app.query_one('#status', Static).render().plain)
            await pilot.press('3')
            self.assertEqual(app.query_one(ItemTabs).active, 'runs')
            self.assertIn('2 runs', app.query_one('#runs_text', Static).content.renderables[0].plain)
            await pilot.press('1', 'f', 'r')
            self.assertTrue(app.reading.follow)
            launcher.poll.assert_called_once()
            with patch.object(app, 'footer', wraps=app.footer) as footer:
                await pilot.resize_terminal(120, 36)
                footer.assert_called()
                self.assertEqual(app.query_one('#status', Static).content.cell_len, 120)
            await pilot.press('q')
        app.worker.thread.join(2)

    def test_worker_reuses_snapshot_and_retries_file_errors_and_replacement(self):
        worker = LocalWorker(self.root, self.path)
        request = Request('plan:12', 1, chosen=True)
        from ub_agents.view.data import load_session
        with patch('ub_agents.view.worker.load_session', wraps=load_session) as load:
            first = worker.read(request)
            self.assertIs(worker.read(request).session, first.session)
            load.assert_called_once()
            # Atomic replacement must be seen even if size and mtime match.
            info = self.path.stat()
            self.state['repository'] = 'example/else'
            publish_snapshot(self.path, self.state)
            import os
            os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
            self.assertNotEqual(worker.read(request).session, first.session)
            self.path.unlink()
            self.assertIsNotNone(worker.read(request).session.error)
            self.path.write_text('{')
            self.assertIsNotNone(worker.read(request).session.error)
            publish_snapshot(self.path, self.state)
            self.assertIsNone(worker.read(request).session.error)
