"""Bounded #111 view variant; the archived #99 sources remain unchanged.

Same adapter, immutable paused window, explicit disk-backed older pages. This is
a replay experiment, not a supported launcher command or runtime integration.
"""
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.widgets import RichLog, Static, TabbedContent

from experiments.terminal_observer.demo import View as ArchivedView
from experiments.terminal_observer.logs import Buffer, Entry, MAX_ENTRIES, READ_BUDGET, Tail

PAGE_BYTES = 256 * 1024
RENDER_BATCH = 32


def page_start(path, end):
    """At most 256KiB, aligned after a newline when not at the file start."""
    start = max(0, end - PAGE_BYTES)
    if start:
        with path.open('rb') as stream:
            stream.seek(start - 1)
            if stream.read(1) != b'\n':
                data = stream.read(end - start)
                newline = data.find(b'\n')
                start = start + newline + 1 if newline >= 0 else end
    return start


class IndexedBuffer(Buffer):
    """Track byte ranges alongside the existing bounded adapter records."""
    def __init__(self, start=0):
        super().__init__()
        self.source_end = self.last_end = start
        self.refs = deque(maxlen=MAX_ENTRIES)

    def feed(self, chunk, capture=None):
        self.source_end += len(chunk)
        super().feed(chunk, capture)

    def append(self, entry):
        end = self.source_end - len(self.pending)
        self.refs.append((self.last_end, end, entry))
        self.last_end = end
        super().append(entry)


class WindowTail(Tail):
    def __init__(self, path):
        super().__init__(path)
        self.start = page_start(path, self.initial_size) if not self.error and path.exists() else 0
        self.offset = self.start
        self.buffer = IndexedBuffer(self.start)
        self.resets = 0

    def poll(self):
        if self.buffer.ready():
            self.buffer.feed(b'')
            return
        try:
            stat = self.path.stat()
            file_id = (stat.st_dev, stat.st_ino)
            if stat.st_size < self.offset or (self.file_id is not None and self.file_id != file_id):
                self.start = self.offset = page_start(self.path, stat.st_size)
                self.initial_size = stat.st_size
                self.buffer = IndexedBuffer(self.start)
                self.resets += 1
                self.buffer.append(Entry(None, None, 'observer', 'Raw file truncated/replaced; new window', ''))
            self.file_id = file_id
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                historical = self.offset < self.initial_size
                limit = min(READ_BUDGET, self.initial_size - self.offset) if historical else READ_BUDGET
                chunk = stream.read(limit)
            self.error = None
        except OSError as error:
            self.error = str(error)
            return
        if chunk:
            self.offset += len(chunk)
            captured = None if historical else datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            self.buffer.feed(chunk, captured)


def older_page(path, end):
    start = page_start(path, end)
    buffer = IndexedBuffer(start)
    with path.open('rb') as stream:
        stream.seek(start)
        data = stream.read(end - start)
    buffer.feed(data)
    while buffer.ready():
        buffer.feed(b'')
    return list(buffer.refs), start


class View(ArchivedView):
    TITLE = '#111 Claude-first replay — synthetic launcher observations'
    CSS = ArchivedView.CSS + '\n#log_note { height: 5; overflow: hidden; }'
    BINDINGS = [binding for binding in ArchivedView.BINDINGS
                if not isinstance(binding, tuple) or binding[0] != 'o'] + [
        Binding('h', 'history', 'Older page', priority=True),
    ]

    def __init__(self, observer):
        super().__init__(observer)
        self.display_refs = []
        self.draw_queue = deque()
        self.display_total = -1
        self.display_resets = 0
        self.history_notice = ''
        self.last_note = None

    def render_selection(self):
        # Reuse the local observation/details projection, never a network reader.
        previous = self.tail.path if self.tail else None
        super().render_selection()
        if self.tail and not isinstance(self.tail, WindowTail):
            self.tail = WindowTail(self.tail.path)
        if self.tail and self.tail.path != previous:
            self.follow = True
            self.display_refs = []
            self.display_total = -1
            self.draw_queue.clear()
        self.show_log()

    def redraw(self, refs):
        output = self.query_one('#output', RichLog)
        # The page is bounded by 200 decoded projections (2048 chars each), not
        # by rendered rows. Appending cannot evict rows from a paused page.
        output.max_lines = None
        output.clear()
        self.display_refs = list(refs)
        self.draw_queue = deque(refs)
        self.render_width = output.scrollable_content_region.width

    def note(self):
        if not self.tail:
            return
        buffer = self.tail.buffer
        start = self.display_refs[0][0] if self.display_refs else self.tail.start
        end = self.display_refs[-1][1] if self.display_refs else start
        partial = len(buffer.pending) if buffer.preview() else 0
        try:
            raw_path = self.tail.path.relative_to(Path.cwd())
        except ValueError:
            raw_path = self.tail.path
        state = self.history_notice or f'{len(self.display_refs)}/200 · evicted {buffer.discarded} · partial {partial}B'
        if not self.follow and self.tail.resets != self.display_resets:
            state = 'File changed; paused page retained; f latest'
        text = (f"{'FOLLOW' if self.follow else 'PAUSED'} {'RAW' if self.raw else 'FORMATTED'} · bytes {start}–{end}\n"
                f'h older · f follow/latest · u raw\n'
                f'{state}\nFull raw: {raw_path}')
        if self.tail.error:
            text = f'Local log unreadable: {self.tail.error}\n' + text
        if text != self.last_note:
            self.query_one('#log_note', Static).update(Text(text))
            self.last_note = text

    def show_log(self):
        if not self.is_running or not isinstance(self.tail, WindowTail):
            return
        output = self.query_one('#output', RichLog)
        self.note()
        if self.query_one(TabbedContent).active != 'log' or output.scrollable_content_region.width <= 0:
            return
        if self.follow and not self.draw_queue and (
                self.tail.buffer.total != self.display_total or self.tail.resets != self.display_resets):
            refs = list(self.tail.buffer.refs)
            difference = self.tail.buffer.total - self.display_total
            if (self.display_total >= 0 and 0 < difference <= len(refs)
                    and len(self.display_refs) + difference <= MAX_ENTRIES
                    and self.tail.resets == self.display_resets):
                new = refs[-difference:]
                self.display_refs.extend(new)
                self.draw_queue.extend(new)
            else:
                self.redraw(refs)
            self.display_total = self.tail.buffer.total
            self.display_resets = self.tail.resets
            self.history_notice = ''
        if output.scrollable_content_region.width != self.render_width:
            self.redraw(self.display_refs)
        for _ in range(min(RENDER_BATCH, len(self.draw_queue))):
            _, _, entry = self.draw_queue.popleft()
            output.write(Text(entry.display(self.raw), overflow='fold'),
                         width=output.scrollable_content_region.width, scroll_end=self.follow)
        self.seen = self.display_total if not self.draw_queue else -1
        self.note()

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == 'log' and self.is_mounted:
            self.call_after_refresh(self.show_log)

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.show_log)

    def action_follow(self):
        self.follow = not self.follow
        output = self.query_one('#output', RichLog)
        output.auto_scroll = self.follow
        if self.follow:
            self.display_total = -1
            self.draw_queue.clear()
            self.show_log()
            output.scroll_end(animate=False)
        else:
            self.note()

    def action_raw(self):
        self.raw = not self.raw
        self.redraw(self.display_refs)
        self.show_log()

    def action_history(self):
        if not isinstance(self.tail, WindowTail) or self.draw_queue:
            return
        if self.tail.resets != self.display_resets:
            self.history_notice = 'File changed; f loads the new file first'
            self.note()
            return
        end = self.display_refs[0][0] if self.display_refs else self.tail.start
        if not end:
            self.history_notice = 'Beginning available; Page Up scrolls this page'
            self.note()
            return
        self.follow = False
        self.query_one('#output', RichLog).auto_scroll = False
        try:
            refs, start = older_page(self.tail.path, end)
        except OSError as error:
            self.history_notice = f'Older page unavailable: {error}'
            self.note()
            return
        self.redraw(refs)
        self.history_notice = ('Older page; f returns to current output' if refs
                               else f'No complete record in bounded page {start}–{end}; use raw file')
        self.show_log()
