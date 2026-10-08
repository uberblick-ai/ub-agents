import asyncio
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from textual import events

from tests.support import RecordingDescriptionTransport
from tests.test_view_data import event, fixture, publish_snapshot
from tests.terminal import Terminal
from ub_agents.view_github import DescriptionLoads
from ub_agents.view_ui import LogPane, View
from ub_agents.view_work import RecentActivity, WorkTree


def recent_fixture(root):
    path, _, state = fixture(root)
    state['outcomes'] = [dict(state['outcomes'][0], run=f'recent-{n}', item=100 + n,
                              title=f'Outcome {n}', summary=f'Summary {n}') for n in range(20)]
    state['latest_pass']['rows'] = [
        {'item': n, 'agent': 'worker', 'state': 'blocked', 'title': f'Live {n}'}
        for n in range(20, 30)]
    run = root / '.ub-agents' / 'runs' / 'recent-0'
    run.mkdir()
    (run / 'process.log').write_bytes(event(987, 20))
    publish_snapshot(path, state)
    return path, state


class RecentActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.state = recent_fixture(self.root)
        clock_patch = patch('ub_agents.view_scroll.monotonic', return_value=100)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    async def ready(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(0.03)
            if condition():
                return
        self.fail('Recent activity did not become ready')

    def wheel(self, area, down=True):
        event_type = events.MouseScrollDown if down else events.MouseScrollUp
        area.post_message(event_type(area, 2, 2, 0, 0, 0, False, False, False))

    async def test_heading_counts_all_listed_outcomes_and_rows_follow_immediately(self):
        template = self.state['outcomes'][0]
        for size in ((110, 32), (80, 24)):
            with self.subTest(size=size):
                app = View(self.root, self.path)
                async with app.run_test(size=size) as pilot:
                    recent = app.query_one(RecentActivity)
                    for total in (0, 1, 7, 20, 42, 0):
                        with self.subTest(total=total):
                            available = [dict(template, run=f'listed-{n}', title=f'Listed outcome {n}',
                                              time=self.state['published_at'] if n % 2 else '2020-01-01T00:00:00Z')
                                         for n in range(total)]
                            # The session cache retains only the latest 20 outcomes.
                            self.state['outcomes'] = available[-20:]
                            omitted = {'outcomes': max(0, total - 20)}
                            self.state['omitted'] = omitted
                            publish_snapshot(self.path, self.state)
                            keys = [f'outcome:listed-{n}' for n in reversed(range(max(0, total - 20), total))]
                            await self.ready(pilot, lambda: app.session is not None
                                             and app.session.data.get('omitted') == omitted
                                             and [row.key for row in recent.rows] == keys)
                            recent.scroll_home(animate=False, immediate=True)
                            await pilot.pause()
                            lines = recent.render().plain.splitlines()
                            self.assertTrue(lines[0].startswith(f'Recent activity · showing {min(total, 20)}'))
                            self.assertNotIn('not retained', '\n'.join(lines))
                            self.assertEqual(recent.header_height, 1)
                            self.assertEqual(app.session.data['omitted'], omitted)
                            if total:
                                self.assertIn(f'Listed outcome {total - 1}', lines[1])
                                first = app.screen._compositor.render_strips()[recent.region.y + 1]
                                first = first.crop(recent.region.x,
                                                   recent.region.x + recent.scrollable_content_region.width).text
                                self.assertIn(f'Listed outcome {total - 1}', first)
                            else:
                                self.assertEqual(len(lines), 1)
                    await pilot.press('q')
                app.worker.thread.join(2)
                self.assertFalse(app.worker.thread.is_alive())

    async def test_wheel_click_last_log_arrows_and_independent_halves(self):
        for size in ((110, 32), (80, 24)):
            with self.subTest(size=size):
                app = View(self.root, self.path, descriptions=DescriptionLoads(RecordingDescriptionTransport()))
                async with app.run_test(size=size) as pilot:
                    tree, recent = app.query_one(WorkTree), app.query_one(RecentActivity)
                    await self.ready(pilot, lambda: len(recent.rows) == 20 and recent.max_scroll_y > 0)
                    # Initial layout defers revealing the selection until refresh.
                    await pilot.pause()
                    sizes = tree.size, recent.size
                    width = recent.scrollable_content_region.width
                    self.assertEqual(recent.styles.scrollbar_visibility, 'hidden')
                    self.assertEqual(recent.size.width - width, 1)
                    self.assertNotIn('not retained', recent.render().plain)
                    upper = tree.scroll_y
                    for _ in range(30):
                        self.wheel(recent)
                    await self.ready(pilot, lambda: recent.scroll_y == recent.max_scroll_y)
                    self.assertEqual(tree.scroll_y, upper)
                    self.assertEqual(recent.styles.scrollbar_visibility, 'visible')
                    self.assertEqual(recent.scrollable_content_region.width, width)
                    self.assertEqual(recent.visible_rows[-1].key, 'outcome:recent-0')
                    y = recent.header_height + 19 * recent.row_stride - recent.scroll_offset.y
                    await pilot.click(recent, offset=(2, y))
                    self.assertEqual(app.selected, 'outcome:recent-0')
                    await pilot.press('enter')
                    await self.ready(pilot, lambda: any('event 00987' in line.text
                                                      for line in app.query_one(LogPane).lines))
                    self.assertIn('Outcome 0', app.query_one('#item_header').render().plain)
                    if app.narrow:
                        self.assertTrue(app.item_view)
                        await pilot.press('escape')
                    lower = recent.scroll_y
                    self.wheel(tree)
                    await self.ready(pilot, lambda: tree.scroll_y > upper)
                    self.assertEqual(recent.scroll_y, lower)
                    for _ in range(30):
                        self.wheel(recent, down=False)
                    await self.ready(pilot, lambda: recent.scroll_y == 0)
                    self.assertEqual(recent.visible_rows[0].key, 'outcome:recent-19')
                    self.assertEqual((tree.size, recent.size), sizes)
                    # Arrows operate on every retained row, regardless of the viewport.
                    recent.cursor = recent.rows[0].key
                    recent.focus(scroll_visible=False)
                    for row in recent.rows[1:]:
                        await pilot.press('down')
                        self.assertEqual(recent.cursor, row.key)
                        self.assertIn(row, recent.visible_rows)
                    self.assertEqual(recent.scroll_y, recent.max_scroll_y)
                    for row in reversed(recent.rows[:-1]):
                        await pilot.press('up')
                        self.assertEqual(recent.cursor, row.key)
                        self.assertIn(row, recent.visible_rows)
                        y = recent.header_height + recent.rows.index(row) * recent.row_stride - recent.scroll_offset.y
                        self.assertGreaterEqual(y, recent.header_height)
                        self.assertLessEqual(y + recent.row_height, recent.scrollable_content_region.height)
                    await pilot.press('up')
                    self.assertIs(app.focused, tree)
                    await pilot.press('down')
                    self.assertIs(app.focused, recent)
                    self.assertEqual(recent.cursor, recent.rows[0].key)
                    self.assertEqual(app.descriptions.transport.calls, [])
                    await pilot.press('q')
                app.worker.thread.join(2)
                self.assertFalse(app.worker.thread.is_alive())

    async def test_header_stays_fixed_and_retention_counts_do_not_change_geometry(self):
        for size in ((110, 32), (80, 24)):
            with self.subTest(size=size):
                app = View(self.root, self.path)
                async with app.run_test(size=size) as pilot:
                    recent = app.query_one(RecentActivity)
                    await self.ready(pilot, lambda: len(recent.rows) == 20 and recent.max_scroll_y > 0)
                    geometry = recent.virtual_size, recent.max_scroll_y, len(recent.visible_rows)
                    for omitted in (0, 1, 3, 0):
                        self.state['omitted'] = {'outcomes': omitted}
                        publish_snapshot(self.path, self.state)
                        await self.ready(pilot, lambda: app.session.data.get('omitted') == {'outcomes': omitted})
                        self.assertEqual(recent.header_height, 1)
                        self.assertEqual((recent.virtual_size, recent.max_scroll_y), geometry[:2])
                        recent.scroll_home(animate=False, immediate=True)
                        await pilot.pause()
                        self.assertEqual(len(recent.visible_rows), geometry[2])
                        recent.scroll_end(animate=False, immediate=True)
                        await pilot.pause()
                        self.assertEqual(recent.scroll_y, recent.max_scroll_y)
                        strips = app.screen._compositor.render_strips()
                        lines = [strip.crop(recent.region.x,
                                            recent.region.x + recent.scrollable_content_region.width).text
                                 for strip in strips[recent.region.y:recent.region.bottom]]
                        self.assertTrue(lines[0].startswith('Recent activity · showing 20'))
                        self.assertNotIn('not retained', '\n'.join(lines))
                        self.assertIn('Outcome 0', '\n'.join(lines[recent.header_height:]))
                        self.assertEqual(recent.visible_rows[-1], recent.rows[-1])
                        # The fixed header remains inert even above scrolled rows.
                        cursor, selection = recent.cursor, app.selected
                        for y in range(recent.header_height):
                            await pilot.click(recent, offset=(2, y))
                            self.assertEqual((recent.cursor, app.selected), (cursor, selection))
                        y = recent.header_height + 19 * recent.row_stride - recent.scroll_offset.y
                        await pilot.click(recent, offset=(2, y))
                        self.assertEqual(app.selected, 'outcome:recent-0')
                        if omitted == 3:
                            for row in reversed(recent.rows[:-1]):
                                await pilot.press('up')
                                y = recent.header_height + recent.rows.index(row) * recent.row_stride - recent.scroll_offset.y
                                self.assertEqual(recent.cursor, row.key)
                                self.assertGreaterEqual(y, recent.header_height)
                                self.assertLessEqual(y + recent.row_height, recent.scrollable_content_region.height)
                    await pilot.press('q')
                app.worker.thread.join(2)
                self.assertFalse(app.worker.thread.is_alive())

    async def test_refresh_new_outcomes_retention_and_resize_preserve_browsing(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            recent = app.query_one(RecentActivity)
            await self.ready(pilot, lambda: len(recent.rows) == 20 and recent.max_scroll_y > 0)
            await pilot.pause()
            recent.scroll_to(y=25, animate=False, immediate=True)
            recent.cursor = recent.rows[10].key
            recent.action_select()
            recent.focus(scroll_visible=False)
            await pilot.pause()
            selection, cursor, anchor = app.selected, recent.cursor, recent.viewport_anchor()
            visible = [row.key for row in recent.visible_rows]
            position = recent.scroll_y
            self.assertEqual(position, 25)
            self.state['latest_pass']['state'] = 'complete'
            publish_snapshot(self.path, self.state)
            await self.ready(pilot, lambda: app.pane.title == 'Work · pass complete')
            self.assertEqual((app.selected, recent.cursor, recent.scroll_y), (selection, cursor, position))
            self.assertEqual([row.key for row in recent.visible_rows], visible)
            # Evict the oldest cache entry and prepend a new one to the displayed list.
            self.state['outcomes'] = self.state['outcomes'][1:] + [
                dict(self.state['outcomes'][-1], run='newest', title='Newest outcome')]
            self.state['omitted'] = {'outcomes': 1}
            publish_snapshot(self.path, self.state)
            await self.ready(pilot, lambda: recent.rows[0].key == 'outcome:newest'
                             and recent.viewport_anchor() == anchor)
            self.assertEqual((app.selected, recent.cursor), (selection, cursor))
            self.assertEqual([row.key for row in recent.visible_rows], visible)
            self.assertEqual(len(recent.rows), 20)
            self.assertTrue(recent.render().plain.startswith('Recent activity · showing 20'))
            self.assertNotIn('not retained', recent.render().plain)
            self.assertEqual(recent.styles.scrollbar_visibility, 'hidden')
            for size in ((80, 24), (110, 32), (140, 44)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertEqual((app.selected, recent.cursor), (selection, cursor))
                self.assertIn(selection, [row.key for row in recent.visible_rows])
                index = [row.key for row in recent.rows].index(selection)
                y = recent.header_height + index * recent.row_stride - recent.scroll_offset.y
                self.assertGreaterEqual(y, recent.header_height)
                self.assertLessEqual(y + recent.row_height, recent.scrollable_content_region.height)
                self.assertEqual(recent.styles.scrollbar_visibility, 'hidden')
            recent.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            self.state['outcomes'] = self.state['outcomes'][1:] + [
                dict(self.state['outcomes'][-1], run='newer', title='Newer outcome')]
            self.state['omitted']['outcomes'] = 2
            publish_snapshot(self.path, self.state)
            await self.ready(pilot, lambda: recent.rows[0].key == 'outcome:newer')
            await pilot.pause()
            self.assertEqual(recent.scroll_y, 0)
            self.assertEqual((app.selected, recent.cursor), (selection, cursor))
            self.assertTrue(recent.render().plain.startswith('Recent activity · showing 20'))
            self.assertNotIn('not retained', recent.render().plain)
            await pilot.click(recent, offset=(2, 0))
            self.assertEqual(app.selected, selection)
            # User cursor scrolling follows the shared timeout, unlike restoration.
            await pilot.press('down')
            self.assertEqual(recent.styles.scrollbar_visibility, 'visible')
            self.clock.return_value += 1.5
            app.tick()
            await pilot.pause()
            self.assertEqual(recent.styles.scrollbar_visibility, 'hidden')
            self.assertNotIn(recent.vertical_scrollbar, app.screen._compositor.visible_widgets)
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())


class RecentActivityTerminalTests(unittest.TestCase):
    def test_real_terminal_wheel_input(self):
        self.check_terminal(tmux=False)

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
    def test_real_terminal_wheel_input_through_tmux(self):
        self.check_terminal(tmux=True)

    def check_terminal(self, tmux):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, state = recent_fixture(root)
            state['omitted'] = {'outcomes': 3}
            publish_snapshot(path, state)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from ub_agents.view_ui import LogPane, View
from ub_agents.view_work import RecentActivity, WorkTree
class ProofView(checkpoint_view(View, sys.argv[3])):
    def proof_values(self):
        tree, recent = self.query_one(WorkTree), self.query_one(RecentActivity)
        strips = self.screen._compositor.render_strips()
        return {'ready': len(recent.rows) == 20, 'narrow': self.narrow,
                'pass_state': self.session.data['latest_pass']['state'] if self.session else None,
                'selected': self.selected, 'cursor': recent.cursor,
                'upper': tree.scroll_y, 'lower': recent.scroll_y, 'max': recent.max_scroll_y,
                'anchor': recent.viewport_anchor(),
                'recent_region': list(recent.region), 'work_region': list(tree.region),
                'bar': recent.vertical_scrollbar in self.screen._compositor.visible_widgets,
                'width': recent.scrollable_content_region.width,
                'header': self.query_one('#item_header').render().plain,
                'log': [line.text for line in self.query_one(LogPane).lines],
                'visible': [strip.crop(recent.region.x, recent.region.x + recent.scrollable_content_region.width).text
                            for strip in strips[recent.region.y:recent.region.bottom]]}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof, tmux=tmux) as terminal:
                for size in ((110, 32), (80, 24)):
                    terminal.resize(*size)
                    rest = terminal.checkpoint(lambda value: value['ready'] and value['max'] > 0)
                    self.assertFalse(rest['bar'])
                    if rest['lower']:
                        self.assertEqual(rest['selected'], 'outcome:recent-0')
                        self.assertIn('Outcome 0', '\n'.join(rest['visible']))
                        x, y, _, _ = rest['recent_region']
                        terminal.send(f'\x1b[<64;{x + 3};{y + 3}M'.encode() * 30)
                        terminal.checkpoint(lambda value: value['lower'] == 0)
                        rest = terminal.checkpoint(lambda value: not value['bar'])
                    x, y, _, _ = rest['recent_region']
                    down = f'\x1b[<65;{x + 3};{y + 3}M'.encode()
                    up = f'\x1b[<64;{x + 3};{y + 3}M'.encode()
                    terminal.send(down * 30)
                    bottom = terminal.checkpoint(lambda value: value['lower'] == value['max'])
                    self.assertTrue(bottom['visible'][0].startswith('Recent activity · showing 20'))
                    self.assertNotIn('not retained', '\n'.join(bottom['visible']))
                    self.assertIn('Outcome 0', '\n'.join(bottom['visible']))
                    self.assertEqual(bottom['upper'], rest['upper'])
                    self.assertTrue(bottom['bar'])
                    self.assertEqual(bottom['width'], rest['width'])
                    stride = 1 if size[0] < 110 else 3
                    header_height = 1
                    last_y = y + header_height + 19 * stride - int(bottom['lower'])
                    terminal.send(f'\x1b[<0;{x + 3};{last_y + 1}M\x1b[<0;{x + 3};{last_y + 1}m'.encode())
                    terminal.checkpoint(lambda value: value['selected'] == 'outcome:recent-0')
                    self.state_refresh(path, state)
                    refreshed = terminal.checkpoint(lambda value:
                        value['pass_state'] == state['latest_pass']['state'])
                    self.assertEqual(refreshed['selected'], 'outcome:recent-0')
                    self.assertEqual(refreshed['lower'], bottom['lower'])
                    ux, uy, _, _ = refreshed['work_region']
                    terminal.send(f'\x1b[<65;{ux + 3};{uy + 3}M'.encode())
                    upper = terminal.checkpoint(lambda value: value['upper'] > rest['upper'])
                    self.assertEqual(upper['lower'], bottom['lower'])
                    terminal.send(up * 30)
                    top = terminal.checkpoint(lambda value: value['lower'] == 0)
                    self.assertTrue(top['visible'][0].startswith('Recent activity · showing 20'))
                    self.assertIn('Outcome 19', top['visible'][1])
                    # Return to the selected row without exposing a bar on resize.
                    terminal.send(down * 30)
                    terminal.checkpoint(lambda value: value['lower'] == value['max'])
                    terminal.checkpoint(lambda value: not value['bar'])
                    terminal.send(b'\r')
                    opened = terminal.checkpoint(lambda value: 'Outcome 0' in value['header']
                        and any('event 00987' in line for line in value['log']))
                    self.assertIn('Outcome 0', opened['header'])
                terminal.send(b'q')
                terminal.wait_exit()

    def state_refresh(self, path, state):
        state['latest_pass']['state'] = ('partial' if state['latest_pass']['state'] == 'complete'
                                         else 'complete')
        publish_snapshot(path, state)
