"""Retained log-page state and rendering for the optional Textual view."""

from dataclasses import dataclass

from rich.segment import Segment
from rich.text import Text
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import TabbedContent

from .view_scroll import ScrollbarVisibility
from .view_theme import log_style
from .view_widgets import pane_line

MAX_RENDER_LINES = 400


@dataclass
class Reading:
    page: object = None
    follow: bool = True
    raw: bool = False
    anchor: tuple | None = None
    seen: int = 0
    notice: str = ''
    latest: object = None
    log: object = None
    runtime: str = 'unknown'
    empty_message: str = 'No local log cached for this row.'


class LogPane(ScrollbarVisibility, ScrollView):
    """A fixed retained page, with a logical entry anchor instead of RichLog redraw.

    At most 400 wrapped lines are rendered. Accounting and older paging include
    every entry inside that budget, even when its projection has no lines.
    """
    can_focus = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reading = None
        self.lines = []
        self.positions = []
        self.visible_refs = ()
        self.hidden = 0
        self.render_width = 0
        self.held_anchor = None
        self.held_y = None

    def anchor(self):
        y = int(self.scroll_y)
        if self.held_anchor is not None and y == self.held_y:
            return self.held_anchor
        if self.positions and y < len(self.positions):
            return self.positions[y]
        return None

    def _wrap_entry(self, ref, raw, width):
        value = ref.value
        rich = Text(value.display(raw), style=self.rich_style)
        if raw or not value.compact:
            return rich.wrap(self.app.console, width, overflow='fold')
        for start, end, style in value.styles:
            rich.stylize(log_style(self.app, style), start, end)
        lines = []
        for line in rich.split('\n') if rich.plain else ():
            if any(span.style.italic for span in line.spans):
                # Wrapped assistant continuations share the explicit LF indent.
                column, body = line[:10], line[10:]
                for index, part in enumerate(body.wrap(self.app.console, width - 10, overflow='fold')):
                    lines.append((column if index == 0 else Text(' ' * 10, style=self.rich_style)) + part)
            else:
                suffixes = [span.start for span in line.spans
                            if (line.plain[span.start:span.end][:1] in ('+', '-')
                                and line.plain[span.start:span.end][1:].isdigit())
                            or line.plain[span.start:span.end].startswith(' · ')]
                if suffixes:
                    start = min(suffixes)
                    if line.plain[start:start + 3] != ' · ':
                        start -= 1
                    suffix = line[start:]
                    line = line[:start]
                    line.truncate(width - suffix.cell_len, overflow='ellipsis')
                    line += suffix
                else:
                    line.truncate(width, overflow='ellipsis')
                lines.append(line)
        return lines

    def set_reading(self, reading):
        if self.reading is not None and self.app.query_one(TabbedContent).active == 'log':
            self.reading.anchor = self.anchor()
        self.reading = reading
        self.reflow()

    def reflow(self):  # noqa: C901
        reading = self.reading
        width = max(20, self.scrollable_content_region.width)
        self.render_width = width
        anchor = reading.anchor if reading else None
        wrapped = []
        if reading and reading.page:
            for ref in reversed(reading.page.refs):
                lines = self._wrap_entry(ref, reading.raw, width)
                if sum(len(part[1]) for part in wrapped) + len(lines) > MAX_RENDER_LINES:
                    break
                wrapped.append((ref, lines))
        wrapped.reverse()
        target = anchor[0] if anchor else None
        if reading and reading.page and anchor:
            visible = [ref for ref in reading.page.refs if ref.value.display(reading.raw)]
            if not any(ref.start == target for ref in visible) and visible:
                # Keep the logical hidden anchor; display its next visible
                # neighbor, or the last visible entry at the end of the page.
                target = next((ref.start for ref in visible if ref.start > target), visible[-1].start)
        if reading and reading.page and not reading.follow and anchor and not any(ref.start == target for ref, _ in wrapped):
            # Raw text can wrap to more rows than its formatted projection. Keep
            # the anchored entry even when it falls outside the tail line budget.
            start = next((i for i, ref in enumerate(reading.page.refs) if ref.start == target), None)
            if start is not None:
                wrapped = []
                count = 0
                for ref in reading.page.refs[start:]:
                    lines = self._wrap_entry(ref, reading.raw, width)
                    if count + len(lines) > MAX_RENDER_LINES:
                        break
                    count += len(lines)
                    wrapped.append((ref, lines))
        self.visible_refs = tuple(ref for ref, _ in wrapped)
        self.hidden = len(reading.page.refs) - len(self.visible_refs) if reading and reading.page else 0
        self.lines, self.positions = [], []
        for ref, lines in wrapped:
            for index, line in enumerate(lines):
                self.lines.append(Strip(Segment.apply_style(line.render(self.app.console), self.rich_style), line.cell_len))
                self.positions.append((ref.start, index / max(1, len(lines))))
        self.virtual_size = Size(width, len(self.lines))
        self.call_after_refresh(self.restore, reading, anchor, target, reading.follow if reading else False)

    def restore(self, reading, anchor, target, was_following):
        if self.reading is not reading:
            return
        if reading and was_following and not reading.follow:
            # A later pause or scroll owns the position, even while its anchor
            # save is pending in the app's or pane's after-refresh queue.
            return
        self.held_anchor = self.held_y = None
        # After a reflow, max_scroll_y can be current while the scrollbar
        # flags still prohibit scrolling. Force the restore; bounds still clamp.
        if reading and reading.follow:
            self.scroll_end(animate=False, immediate=True, force=True)
        elif anchor and self.positions:
            candidates = [(index, position) for index, position in enumerate(self.positions) if position[0] == target]
            if candidates:
                y = min(candidates, key=lambda item: abs(item[1][1] - anchor[1]))[0]
                self.scroll_to(y=y, animate=False, immediate=True, force=True)
        elif not anchor:
            self.scroll_home(animate=False, immediate=True, force=True)
        if reading and not reading.follow and anchor and (target != anchor[0] or not self.positions):
            self.held_anchor, self.held_y = anchor, int(self.scroll_y)
        self.refresh()

    def render_line(self, y):
        index = y + int(self.scroll_y)
        width = self.size.width
        if not self.lines and y == 0 and self.reading:
            value = pane_line(self.reading.empty_message, width)
            return Strip([Segment(value.plain, self.rich_style)], value.cell_len).crop_extend(0, width, self.rich_style)
        return (self.lines[index].crop_extend(0, width, self.rich_style) if index < len(self.lines)
                else Strip.blank(width, self.rich_style))

    def on_resize(self):
        if self.reading and self.app.query_one(TabbedContent).active == 'log':
            width = self.scrollable_content_region.width
            if width > 0 and max(20, width) != self.render_width:
                self.call_after_refresh(self.reflow_for_resize)
            elif width > 0 and self.reading.follow:
                self.scroll_end(animate=False, immediate=True)

    def reflow_for_resize(self):
        width = self.scrollable_content_region.width
        if width <= 0 or max(20, width) == self.render_width:
            return
        # Height changes from notices and the pill do not change wrapping. For
        # a width change, use the displayed position, including a pending scroll.
        self.save_anchor()
        self.reflow()

    def watch_scroll_y(self, old, value):
        super().watch_scroll_y(old, value)
        if self.held_anchor is not None and int(value) != self.held_y:
            self.held_anchor = self.held_y = None

    def save_anchor(self):
        if self.reading:
            self.reading.anchor = self.anchor()

    def on_mouse_scroll_up(self):
        if self.reading and self.reading.follow:
            self.app.action_follow()
        self.call_after_refresh(self.save_anchor)

    def on_mouse_scroll_down(self):
        self.call_after_refresh(self.save_anchor)

    def action_scroll_up(self):
        if self.reading and self.reading.follow:
            self.app.action_follow()
        self.scroll_up(animate=False)
        self.call_after_refresh(self.save_anchor)

    def action_scroll_down(self):
        self.scroll_down(animate=False)
        self.call_after_refresh(self.save_anchor)
