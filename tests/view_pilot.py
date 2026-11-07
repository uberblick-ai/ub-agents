"""Drive rendered UI checks using message and refresh barriers instead of CPU idle.

Use RefreshPilot(app) inside app.run_test() for interaction-heavy tests. Explicit
delays still elapse; the default pause waits for queued messages and rendering.
"""

import asyncio
from unittest.mock import patch

from textual.geometry import Size
from textual.pilot import Pilot


class RefreshPilot(Pilot):
    # Internal hooks below were checked against the pinned Textual 8.2.8.
    async def press(self, *keys):
        # Keep Textual's key parsing, driver dispatch and animation waits. Only
        # replace its CPU-idle heuristic, as pause() does for mouse input.
        with patch('textual.app.wait_for_idle', self.pause):
            await super().press(*keys)

    async def resize_terminal(self, width, height):
        # Textual debounces Resize delivery to the screen with a timer. app.size
        # changes before the screen's rendered geometry, even across a refresh.
        async with asyncio.timeout(5):
            await super().resize_terminal(width, height)
            while self.app.screen.region.size != Size(width, height):
                await self.pause()
            # Drain widget resize messages and their after-refresh callbacks.
            await self.pause()

    async def pause(self, delay=None):
        await self._wait_for_screen()
        if delay is not None:
            await asyncio.sleep(delay)
        # A quit key can stop rendering while Textual is still finishing its
        # input dispatch. There is no next frame once exit has been requested.
        if self.app._exit:
            return
        refreshed = asyncio.Event()
        self.app.call_after_refresh(refreshed.set)
        await asyncio.wait_for(refreshed.wait(), 5)
        await self._wait_for_screen()
