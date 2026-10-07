import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual import events
from textual.color import Color
from textual.widgets import Static

from tests.test_view_data import event, fixture, publish_snapshot
from tests.terminal import Terminal
from ub_agents.view_scroll import PaneScroll
from ub_agents.view_ui import LogPane, View
from ub_agents.view_unblock import ACTION_MARKER
from ub_agents.view_work import WorkTree


class ViewScrollbarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root, count=600)
        self.state['latest_pass']['rows'] = [
            {'item': number, 'agent': 'worker', 'state': 'ready', 'title': f'Work {number}'}
            for number in range(20, 50)]
        publish_snapshot(self.path, self.state)
        clock_patch = patch('ub_agents.view_scroll.monotonic', return_value=100)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    async def ready(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(0.03)
            if condition():
                return
        self.fail('View did not become ready')

    def expire(self, app, seconds):
        self.clock.return_value += seconds
        app.tick()

    def bar_rendered(self, area):
        return area.vertical_scrollbar in area.screen._compositor.visible_widgets

    async def test_log_at_rest_page_down_timeout_and_no_reflow(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            output = app.query_one(LogPane)
            await self.ready(pilot, lambda: output.max_scroll_y > 0 and app.reading.page is not None)
            output.focus(scroll_visible=False)
            await pilot.press('f')
            output.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            self.assertEqual(output.styles.scrollbar_visibility, 'hidden')
            self.assertFalse(self.bar_rendered(output))
            self.assertEqual(output.styles.scrollbar_size_vertical, 1)
            self.assertEqual(output.styles.scrollbar_gutter, 'stable')
            width, lines = output.scrollable_content_region.width, tuple(output.lines)
            self.assertEqual(output.size.width - width, 1)
            await pilot.press('pagedown')
            self.assertGreater(output.scroll_y, 0)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            self.assertTrue(self.bar_rendered(output))
            self.assertEqual(output.scrollable_content_region.width, width)
            self.assertEqual(tuple(output.lines), lines)
            self.assertEqual(app.query_one(WorkTree).styles.scrollbar_visibility, 'hidden')
            self.expire(app, 1.4)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            await pilot.press('pagedown')
            self.expire(app, 1.4)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            self.expire(app, 0.1)
            await pilot.pause()
            self.assertEqual(output.styles.scrollbar_visibility, 'hidden')
            self.assertFalse(self.bar_rendered(output))
            self.assertEqual(output.scrollable_content_region.width, width)
            self.assertEqual(tuple(output.lines), lines)
            await pilot.press('q')

    async def test_follow_append_restore_switch_and_resize_stay_hidden(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            output = app.query_one(LogPane)
            await self.ready(pilot, lambda: output.max_scroll_y > 0 and app.reading.page is not None)
            before = app.reading.page.end
            with self.log.open('ab') as stream:
                stream.write(event(601, 20))
            await self.ready(pilot, lambda: app.reading.page.end > before
                             and output.scroll_y == output.max_scroll_y)
            self.assertTrue(app.reading.follow)
            self.assertEqual(output.styles.scrollbar_visibility, 'hidden')
            await pilot.press('f')
            output.scroll_to(y=3, animate=False, immediate=True)
            output.save_anchor()
            await pilot.press('2', '1')
            await pilot.resize_terminal(130, 40)
            await pilot.pause()
            self.assertEqual(output.styles.scrollbar_visibility, 'hidden')
            self.assertFalse(self.bar_rendered(output))
            await pilot.press('q')

    async def test_each_text_tab_and_overlay_shows_only_its_own_bar(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(pilot, lambda: app.reading.page is not None)
            for key, selector in (('2', '#issue VerticalScroll'), ('3', '#runs VerticalScroll'),
                                  ('p', '#raw_content'), ('?', '#raw_content')):
                with self.subTest(surface=key):
                    await pilot.press(key)
                    area = app.screen.query_one(selector, PaneScroll)
                    await area.mount(Static('\n'.join(f'Line {i}' for i in range(100))))
                    await self.ready(pilot, lambda: area.max_scroll_y > 0)
                    self.assertEqual(area.styles.scrollbar_visibility, 'hidden')
                    self.assertFalse(self.bar_rendered(area))
                    for color in (area.styles.scrollbar_color, area.styles.scrollbar_color_hover,
                                  area.styles.scrollbar_color_active):
                        self.assertEqual(color, Color.parse(app.theme_variables['view-muted']))
                    width = area.scrollable_content_region.width
                    await pilot.press('pagedown')
                    self.assertGreater(area.scroll_y, 0)
                    self.assertEqual(area.styles.scrollbar_visibility, 'visible')
                    self.assertTrue(self.bar_rendered(area))
                    self.assertEqual(area.scrollable_content_region.width, width)
                    self.assertEqual(app.query_one(LogPane).styles.scrollbar_visibility, 'hidden')
                    self.expire(app, 1.5)
                    self.assertEqual(area.styles.scrollbar_visibility, 'hidden')
                    if key in ('p', '?'):
                        await pilot.press('escape')
            await pilot.press('q')

    async def test_unblock_scroll_keys_and_wheel_from_child_show_bar(self):
        self.state['latest_pass']['rows'].append(
            {'item': 114, 'agent': 'worker', 'state': 'blocked', 'reason': 'Needs a decision'})
        self.state['coordination_authors'] = {'operator': {'trusted': True}}
        self.state['action_needed'] = {'114': {
            'text': ACTION_MARKER + 'run -->\n**Action needed**\n\n'
                    + '\n'.join(f'Line {i}' for i in range(100)), 'author': 'operator'}}
        publish_snapshot(self.path, self.state)
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(pilot, lambda: app.reading.page is not None)
            app.select('plan:114:worker')
            await self.ready(pilot, lambda: app.unblock_visible)
            await pilot.press('4')
            area = app.query_one('#unblock VerticalScroll', PaneScroll)
            await self.ready(pilot, lambda: area.max_scroll_y > 0)
            self.assertFalse(self.bar_rendered(area))
            for key in ('pagedown', 'end', 'pageup', 'home'):
                await pilot.press(key)
                self.assertEqual(area.styles.scrollbar_visibility, 'visible')
                self.expire(app, 1.5)
                self.assertEqual(area.styles.scrollbar_visibility, 'hidden')
            body = app.query_one('#unblock_body')
            body.post_message(events.MouseScrollDown(body, 1, 1, 0, 0, 0, False, False, False))
            await pilot.pause()
            self.assertGreater(area.scroll_y, 0)
            self.assertEqual(area.styles.scrollbar_visibility, 'visible')
            area.focus(scroll_visible=False)
            self.expire(app, 1.5)
            await pilot.press('down')
            self.assertEqual(area.styles.scrollbar_visibility, 'visible')
            self.assertEqual(app.query_one(LogPane).styles.scrollbar_visibility, 'hidden')
            await pilot.press('q')

    async def test_work_cursor_and_wheel_show_bar_but_in_view_cursor_does_not(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            tree = app.query_one(WorkTree)
            await self.ready(pilot, lambda: tree.max_scroll_y > 0 and app.reading.page is not None)
            self.assertFalse(self.bar_rendered(tree))
            await pilot.press('down')
            self.assertEqual(tree.styles.scrollbar_visibility, 'hidden')
            for _ in range(10):
                await pilot.press('down')
                if tree.scroll_y:
                    break
            self.assertGreater(tree.scroll_y, 0)
            self.assertEqual(tree.styles.scrollbar_visibility, 'visible')
            self.expire(app, 1.5)
            tree.post_message(events.MouseScrollDown(tree, 1, 1, 0, 0, 0, False, False, False))
            await pilot.pause()
            self.assertEqual(tree.styles.scrollbar_visibility, 'visible')
            self.assertEqual(app.query_one(LogPane).styles.scrollbar_visibility, 'hidden')
            await pilot.press('q')

    async def test_hover_and_captured_drag_hold_bar_until_leaving(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            output = app.query_one(LogPane)
            await self.ready(pilot, lambda: output.max_scroll_y > 0 and app.reading.page is not None)
            output.focus(scroll_visible=False)
            await pilot.press('home', 'pagedown')
            bar = output.vertical_scrollbar
            await pilot.hover(bar)
            self.assertTrue(bar.mouse_over)
            self.expire(app, 10)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            # Return to the top, where the handle starts at the first row.
            await pilot.press('home')
            await pilot.mouse_down(bar)
            self.assertIsNotNone(bar.grabbed)
            before = output.scroll_y
            await pilot.hover(output, offset=(0, 10))
            await pilot.pause()
            self.assertGreater(output.scroll_y, before)
            self.expire(app, 10)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            await pilot.mouse_up(output, offset=(0, 10))
            self.assertIsNone(bar.grabbed)
            await pilot.hover(output)
            self.assertFalse(bar.mouse_over)
            self.expire(app, 1.4)
            self.assertEqual(output.styles.scrollbar_visibility, 'visible')
            self.expire(app, 0.1)
            self.assertEqual(output.styles.scrollbar_visibility, 'hidden')
            await pilot.press('q')


class TerminalScrollbarTests(unittest.TestCase):
    def test_scrollbar_visibility_and_reserved_column_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = fixture(root, count=600)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from textual.binding import Binding
from ub_agents import view_scroll
from ub_agents.view_ui import LogPane, View
clock = [100]
view_scroll.monotonic = lambda: clock[0]
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('t', 'expire_bar', priority=True)]
    def action_expire_bar(self):
        clock[0] += 1.5
        self.tick()
    def proof_values(self):
        output = self.query_one(LogPane)
        return {'ready': self.reading.page is not None and output.max_scroll_y > 0,
                'visible': output.vertical_scrollbar in self.screen._compositor.visible_widgets,
                'width': output.scrollable_content_region.width, 'scroll': output.scroll_y,
                'lines': [line.text for line in output.lines]}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof) as terminal:
                rest = terminal.checkpoint(lambda value: value['ready'])
                self.assertFalse(rest['visible'])
                terminal.send(b'\x1b[H')
                top = terminal.checkpoint(lambda value: value['scroll'] == 0 and value['visible'])
                terminal.send(b'\x1b[6~')
                scrolling = terminal.checkpoint(lambda value: value['scroll'] > 0 and value['visible'])
                self.assertEqual(scrolling['width'], rest['width'])
                self.assertEqual(scrolling['lines'], top['lines'])
                terminal.send(b't')
                hidden = terminal.checkpoint(lambda value: not value['visible'])
                self.assertEqual(hidden['width'], scrolling['width'])
                self.assertEqual(hidden['lines'], scrolling['lines'])
                self.assertEqual(hidden['scroll'], scrolling['scroll'])
                terminal.send(b'q')
                terminal.wait_exit()
