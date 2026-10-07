"""Scrollbar visibility follows user input, never programmatic positioning."""

from contextlib import contextmanager
from time import monotonic

from textual import events
from textual.containers import VerticalScroll
from textual.scrollbar import ScrollDown, ScrollTo, ScrollUp


SCROLLBAR_SECONDS = 1.5
SCROLL_ACTIONS = {'page_up', 'page_down', 'home', 'end'}


def scroll_action(name):
    return name in SCROLL_ACTIONS or name.startswith(('scroll_', 'cursor_'))


class ScrollbarVisibility:
    """Mixin for the view's scroll areas; Textual 8.2.8 keeps hidden gutters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_class('view-scroll')
        self._user_scroll = False
        self._scrollbar_deadline = None
        self._scrollbar_watched = False

    @contextmanager
    def user_scroll(self):
        previous = self._user_scroll
        self._user_scroll = True
        try:
            yield
        finally:
            self._user_scroll = previous

    @property
    def vertical_scrollbar(self):
        bar = super().vertical_scrollbar
        if not self._scrollbar_watched:
            self._scrollbar_watched = True
            self.watch(bar, 'mouse_over', self.scrollbar_interaction_changed)
            self.watch(bar, 'grabbed', self.scrollbar_interaction_changed)
        return bar

    def scrollbar_held(self):
        bar = self._vertical_scrollbar
        return bar is not None and (bar.mouse_over or bar.grabbed is not None)

    def scrollbar_interaction_changed(self):
        if self.styles.scrollbar_visibility == 'visible':
            self._scrollbar_deadline = None if self.scrollbar_held() else monotonic() + SCROLLBAR_SECONDS

    def show_scrollbar(self, y):
        # Inspect the requested position before Textual defers/animates it.
        # Watching scroll_y would also count follow, restores and resize clamps.
        if self._user_scroll and y is not None and min(self.max_scroll_y, max(0, y)) != self.scroll_y:
            self.styles.scrollbar_visibility = 'visible'
            self.scrollbar_interaction_changed()

    def update_scrollbar(self):
        if self._scrollbar_deadline is not None and monotonic() >= self._scrollbar_deadline:
            if not self.scrollbar_held():
                self.styles.scrollbar_visibility = 'hidden'
                self._scrollbar_deadline = None

    def scroll_to(self, x=None, y=None, **kwargs):
        self.show_scrollbar(y)
        return super().scroll_to(x, y, **kwargs)

    def _scroll_to(self, x=None, y=None, **kwargs):
        # Pointer-wheel scrolling calls this directly in the pinned Textual.
        self.show_scrollbar(y)
        return super()._scroll_to(x, y, **kwargs)

    def scroll_end(self, **kwargs):
        # Textual's scroll_end bypasses scroll_to to wait for new content layout.
        if kwargs.get('y_axis', True):
            self.show_scrollbar(self.max_scroll_y)
        return super().scroll_end(**kwargs)

    async def _on_message(self, message):
        # Wheel events can bubble from Markdown children; scrollbar messages
        # are sent directly to their owning area, including captured drags.
        if isinstance(message, (events.MouseScrollUp, events.MouseScrollDown,
                                ScrollUp, ScrollDown, ScrollTo)):
            with self.user_scroll():
                await super()._on_message(message)
        else:
            await super()._on_message(message)


class PaneScroll(ScrollbarVisibility, VerticalScroll):
    pass
