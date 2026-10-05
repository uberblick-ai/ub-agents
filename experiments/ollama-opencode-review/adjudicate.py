#!/usr/bin/env python3
"""Reproduce the reference review's tab geometry at the pinned reviewed head.

Run with the prepared checkout's Python and that checkout as working directory,
after the blind attempt has ended. This is evaluator evidence, not agent coverage.
"""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

HEAD = "5db1bc3f2a155d11d513bc342a14594e24e27de0"


async def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == HEAD
    sys.path.insert(0, str(Path.cwd()))
    from tests.test_view_ui import ViewUITests
    from ub_agents.view_ui import View
    from textual.widgets import Tabs

    fixture = ViewUITests()
    fixture.setUp()
    try:
        fixture.themed_fixture()
        app = View(fixture.root, fixture.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await fixture.ready(app, pilot)
            await pilot.pause()
            work = app.query_one("#work_pane")
            panes = app.query_one("#panes")
            tabs = app.query_one("#panes Tabs", Tabs)
            active = app.query_one("#panes Tab.-active")
            row = app.screen._compositor.render_strips()[tabs.region.y]
            block = row.crop(active.region.x, active.region.right)
            padding = active.styles.padding
            assert (padding.left, padding.right) == (0, 2)
            assert block.text == "1 Log  "
            colors = [segment.style.bgcolor.name for segment in block if segment.style.bgcolor]
            assert colors and all(c == colors[0] for c in colors)
            assert (work.region.width, panes.region.width) == (46, 63)
            assert panes.region.x == work.region.right + 1
            print(json.dumps({
                "head": HEAD, "size": [110, 32],
                "work_width": work.region.width, "item_width": panes.region.width,
                "gap_columns": panes.region.x - work.region.right,
                "tab_row": row.crop(panes.region.x, panes.region.right).text,
                "active_tab_block": block.text,
                "active_tab_padding_left_right": [padding.left, padding.right],
                "active_tab_backgrounds": colors,
                "conclusion": "Reference review's asymmetric inverted tab is reproduced; whether it blocks acceptance is a design judgment."
            }, indent=2))
            await pilot.press("q")
        app.worker.thread.join(2)
        assert not app.worker.thread.is_alive()
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    asyncio.run(main())
