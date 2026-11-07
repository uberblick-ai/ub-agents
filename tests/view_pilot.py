"""Drive rendered UI checks using message and refresh barriers instead of CPU idle.

Use RefreshPilot(app) inside app.run_test() for interaction-heavy tests. Explicit
delays still elapse; the default pause waits for queued messages and rendering.
"""

import asyncio

from textual.pilot import Pilot


class RefreshPilot(Pilot):
    async def pause(self, delay=None):
        await self._wait_for_screen()
        if delay is not None:
            await asyncio.sleep(delay)
        refreshed = asyncio.Event()
        self.app.call_after_refresh(refreshed.set)
        await asyncio.wait_for(refreshed.wait(), 5)
        await self._wait_for_screen()
