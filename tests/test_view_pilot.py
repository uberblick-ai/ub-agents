import asyncio
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Button, Static

from tests.view_pilot import RefreshPilot


class FrameApp(App):
    BINDINGS = [('a', 'change', 'Change'), ('q', 'quit', 'Quit')]

    def compose(self):
        yield Button('Change', id='change')
        yield Static('0', id='value')

    def action_change(self):
        self.value += 1
        self.call_after_refresh(lambda: self.query_one('#value', Static).update(str(self.value)))

    def on_mount(self):
        self.value = 0

    def on_button_pressed(self):
        self.action_change()


class RefreshPilotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_input_dispatch_refresh_and_quit(self):
        app = FrameApp()
        async with app.run_test():
            pilot = RefreshPilot(app)
            await pilot.press('a')
            self.assertEqual(app.query_one('#value', Static).render().plain, '1')
            self.assertTrue(await pilot.hover('#change'))
            self.assertTrue(await pilot.click('#change'))
            self.assertEqual(app.query_one('#value', Static).render().plain, '2')
            await pilot.resize_terminal(60, 16)
            self.assertEqual(app.size.width, 60)
            self.assertEqual(app.size.height, 16)
            await pilot.press('q')
            self.assertTrue(app._exit)

    async def test_explicit_delay_still_allows_timers_to_fire(self):
        app = FrameApp()
        async with app.run_test():
            fired = asyncio.Event()
            asyncio.get_running_loop().call_later(0.02, fired.set)
            await RefreshPilot(app).pause(0.03)
            self.assertTrue(fired.is_set())

    async def test_resize_waits_for_deferred_screen_layout(self):
        app = FrameApp()
        async with app.run_test(size=(80, 24)):
            pilot = RefreshPilot(app)
            await pilot.pause()
            requested = asyncio.Event()
            check_resize = app._check_resize
            # Hold Textual's debounced resize until ordinary refreshes have
            # completed, so a fast frame cannot masquerade as the resized one.
            with patch.object(app, '_check_resize', side_effect=requested.set):
                resizing = asyncio.create_task(pilot.resize_terminal(60, 16))
                try:
                    await asyncio.wait_for(requested.wait(), 5)
                    await pilot.pause()
                    self.assertFalse(resizing.done())
                    self.assertEqual(app.screen.region.size, (80, 24))
                finally:
                    check_resize()
                    await asyncio.wait_for(resizing, 5)
            self.assertEqual(app.screen.region.size, (60, 16))
            self.assertEqual(app.query_one('#value').region.width, 60)
