"""Optional Textual two-pane UI. Imported only by the development view entrypoint."""

from dataclasses import dataclass
from queue import Empty

from rich.segment import Segment
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.screen import ModalScreen
from textual.widgets import Static, TabbedContent, TabPane, Tree

from .view_data import mapping, outcome_text, text
from .view_worker import LocalWorker, Request

MAX_RENDER_LINES = 400


class RawAccess(ModalScreen):
    BINDINGS = [Binding('escape,p', 'dismiss', 'Close', priority=True)]
    DEFAULT_CSS = 'RawAccess { padding: 2 4; } RawAccess VerticalScroll { background: $panel; padding: 1 2; } #raw_status { height: 2; background: $panel; }'

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with VerticalScroll():
            yield Static(Text(self.message))
        yield Static('', id='raw_status', markup=False)

    def action_dismiss(self):
        self.dismiss()


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


class LogPane(ScrollView):
    """A fixed retained page, with a logical entry anchor instead of RichLog redraw.

    At most 400 wrapped lines are rendered. Whole hidden entries are accounted for
    at the boundary, and older paging starts at the first rendered entry's byte.
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

    def anchor(self):
        y = int(self.scroll_y)
        if self.positions and y < len(self.positions):
            return self.positions[y]
        return None

    def set_reading(self, reading):
        if self.reading is not None and self.app.query_one(TabbedContent).active == 'log':
            self.reading.anchor = self.anchor()
        self.reading = reading
        self.reflow()

    def reflow(self):
        reading = self.reading
        width = max(20, self.scrollable_content_region.width)
        self.render_width = width
        anchor = reading.anchor if reading else None
        wrapped = []
        if reading and reading.page:
            for ref in reversed(reading.page.refs):
                value = ref.value.display(reading.raw)
                lines = Text(value).wrap(self.app.console, width, overflow='fold')
                if sum(len(part[1]) for part in wrapped) + len(lines) > MAX_RENDER_LINES:
                    break
                wrapped.append((ref, lines))
        wrapped.reverse()
        if reading and reading.page and not reading.follow and anchor and not any(ref.start == anchor[0] for ref, _ in wrapped):
            # Raw text can wrap to more rows than its formatted projection. Keep
            # the anchored entry even when it falls outside the tail line budget.
            start = next((i for i, ref in enumerate(reading.page.refs) if ref.start == anchor[0]), None)
            if start is not None:
                wrapped = []
                count = 0
                for ref in reading.page.refs[start:]:
                    lines = Text(ref.value.display(reading.raw)).wrap(self.app.console, width, overflow='fold')
                    if count + len(lines) > MAX_RENDER_LINES:
                        break
                    count += len(lines)
                    wrapped.append((ref, lines))
        self.visible_refs = tuple(part[0] for part in wrapped)
        self.hidden = len(reading.page.refs) - len(wrapped) if reading and reading.page else 0
        self.lines, self.positions = [], []
        for ref, lines in wrapped:
            for index, line in enumerate(lines):
                self.lines.append(Strip([Segment(line.plain, self.rich_style)], line.cell_len))
                self.positions.append((ref.start, index / max(1, len(lines))))
        self.virtual_size = Size(width, len(self.lines))
        self.call_after_refresh(self.restore, reading, anchor)

    def restore(self, reading, anchor):
        if self.reading is not reading:
            return
        if reading and reading.follow:
            self.scroll_end(animate=False, immediate=True)
        elif anchor and self.positions:
            candidates = [(index, position) for index, position in enumerate(self.positions) if position[0] == anchor[0]]
            if candidates:
                y = min(candidates, key=lambda item: abs(item[1][1] - anchor[1]))[0]
                self.scroll_to(y=y, animate=False, immediate=True)
        elif not anchor:
            self.scroll_home(animate=False, immediate=True)
        self.refresh()

    def render_line(self, y):
        index = y + int(self.scroll_y)
        width = self.size.width
        return (self.lines[index].crop_extend(0, width, self.rich_style) if index < len(self.lines)
                else Strip.blank(width, self.rich_style))

    def on_resize(self):
        if self.reading and self.scrollable_content_region.width > 0 and self.app.query_one(TabbedContent).active == 'log':
            self.call_after_refresh(self.reflow)

    def watch_scroll_y(self, old, value):
        super().watch_scroll_y(old, value)

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


class View(App):
    TITLE = 'ub-agents · one launcher · local read-only view'
    CSS = '''
    #body { height: 1fr; }
    #work { width: 36; border: solid $accent; }
    #panes { width: 1fr; }
    TabPane { padding: 0 1; }
    #log_note { height: 5; overflow: hidden; }
    #output { height: 1fr; }
    #status { height: 2; background: $panel; }
    #keys { height: 1; background: $panel; }
    '''
    BINDINGS = [
        Binding('q,ctrl+c', 'quit', 'Quit', priority=True),
        Binding('f', 'follow', 'Follow/pause', priority=True),
        Binding('u', 'raw', 'Raw', priority=True),
        Binding('h', 'history', 'Older page', priority=True),
        Binding('p', 'path', 'Full raw path', priority=True),
        Binding('1', "tab('log')", 'Log', priority=True),
        Binding('2', "tab('issue')", 'Issue', priority=True),
        Binding('3', "tab('runs')", 'Runs', priority=True),
        Binding('pageup', 'page_up', 'Page up', priority=True),
        Binding('pagedown', 'page_down', 'Page down', priority=True),
        Binding('home', 'home', 'Top', priority=True),
        Binding('end', 'end', 'Bottom', priority=True),
    ]

    def __init__(self, root, session_path, worker=None):
        super().__init__()
        self.worker = worker or LocalWorker(root, session_path)
        self.session = None
        self.rows, self.nodes, self.reason_nodes, self.groups = {}, {}, {}, {}
        self.selected = None
        self.readings = {}
        self.token = 0
        self.busy = False
        self.pending_history = None
        self.last_context = self.last_runs = None

    def compose(self) -> ComposeResult:
        with Horizontal(id='body'):
            yield Tree('Launcher work', id='work')
            with TabbedContent(id='panes'):
                with TabPane('Log', id='log'):
                    yield Static('Reading local session…', id='log_note', markup=False)
                    yield LogPane(id='output')
                with TabPane('Issue', id='issue'):
                    with VerticalScroll():
                        yield Static('Context unavailable.', id='issue_text', markup=False)
                with TabPane('Runs', id='runs'):
                    with VerticalScroll():
                        yield Static('No outcomes cached.', id='runs_text', markup=False)
        yield Static('Snapshot freshness unavailable · FOLLOW · FORMATTED', id='status', markup=False)
        yield Static('f follow/pause · h older · u raw · p path · 1/2/3 tabs · Tab panes · PgUp/PgDn scroll · q quit', id='keys', markup=False)

    def on_mount(self):
        self.worker.start()
        self.set_interval(0.1, self.tick)
        self.tick()

    def on_unmount(self):
        self.worker.close()

    @property
    def reading(self):
        return self.readings.setdefault(self.selected, Reading())

    def tick(self):
        try:
            result = self.worker.results.get_nowait()
        except Empty:
            result = None
        if result:
            self.busy = False
            self.session = result.session
            self.populate(result.rows)
            if result.token == self.token and result.key == self.selected:
                self.apply(result)
        if not self.busy:
            end, generation = self.pending_history or (None, 0)
            if self.worker.request(Request(self.selected, self.token, end, generation)):
                self.busy = True
                self.pending_history = None
        self.update_status()

    def populate(self, rows):
        tree = self.query_one('#work', Tree)
        incoming = {row.key: row for row in rows}
        # A selected row disappearing in a partial pass is retained as an earlier
        # observation; updates never replace the selection or steal pane focus.
        if self.selected in self.rows and self.selected not in incoming:
            incoming[self.selected] = self.rows[self.selected]
        for key in tuple(self.nodes):
            if key not in incoming:
                self.nodes.pop(key).remove()
                self.reason_nodes.pop(key, None)
        for row in incoming.values():
            group_key = 'Latest pass' if row.group.startswith('Latest pass') else row.group
            group = self.groups.get(group_key)
            if group is None:
                group = self.groups[group_key] = tree.root.add(Text(row.group), expand=True)
            label = Text(row.label())
            if row.key in self.nodes:
                self.nodes[row.key].set_label(label)
                self.reason_nodes[row.key].set_label(Text(row.reason))
            else:
                self.nodes[row.key] = group.add(label, data=row.key, expand=True)
                self.reason_nodes[row.key] = self.nodes[row.key].add_leaf(Text(row.reason), data=row.key)
        self.rows = incoming
        tree.root.expand()
        if self.selected is None and incoming:
            self.select(next(iter(incoming)))
            tree.select_node(self.nodes[self.selected])
        # Update the pass header in place, preserving cursor/focus and expansion.
        latest = mapping(self.session.data.get('latest_pass'))
        for name, node in self.groups.items():
            if name.startswith('Latest pass'):
                omitted = mapping(self.session.data.get('omitted')).get('plans', 0)
                node.set_label(Text('Latest pass (' + text(latest.get('state'), 'partial') +
                                    (f'; omitted {text(str(omitted))}' if omitted else '') + ')'))

    def on_tree_node_selected(self, event):
        if event.node.data in self.rows:
            self.select(event.node.data)

    def select(self, key):
        if key == self.selected:
            return
        self.selected = key
        self.token += 1
        self.pending_history = None
        self.query_one('#output', LogPane).set_reading(self.reading)
        self.last_context = None
        self.query_one('#issue_text', Static).update('Reading cached context…')
        self.update_status()

    def apply(self, result):
        reading = self.reading
        reading.log, reading.latest = result.log, result.page
        reading.runtime = result.runtime
        if result.history:
            reading.page = result.history
            reading.follow = False
            reading.anchor = None
            reading.notice = result.history.notice or 'Older page; f returns to latest.'
            self.query_one('#output', LogPane).set_reading(reading)
            self.query_one('#output', LogPane).scroll_home(animate=False, immediate=True)
        elif reading.follow and result.page:
            if result.page != reading.page:
                reading.page = result.page
                reading.seen = result.page.total
                reading.notice = result.page.notice
                self.query_one('#output', LogPane).set_reading(reading)
        if result.error:
            reading.notice = text(result.error)
        if result.context != self.last_context:
            self.query_one('#issue_text', Static).update(Text(result.context))
            self.last_context = result.context
        runs = outcome_text(result.session)
        if runs != self.last_runs:
            self.query_one('#runs_text', Static).update(Text(runs))
            self.last_runs = runs

    def update_status(self):
        if not self.is_mounted:
            return
        reading = self.reading
        page, log = reading.page, reading.log
        mode = 'RAW' if reading.raw else 'FORMATTED'
        state = 'FOLLOW' if reading.follow else 'PAUSED'
        unread = max(0, log.total_entries - reading.seen) if log else 0
        lag = log.unread_bytes if log else 0
        if page and reading.latest and page.generation == reading.latest.generation:
            refs = self.query_one('#output', LogPane).visible_refs
            lag += max(0, reading.latest.end - (refs[-1].end if refs else page.end))
        freshness = self.session.freshness() if self.session else 'freshness unavailable'
        errors = (' · malformed: ' + self.session.error) if self.session and self.session.error else ''
        size = ' · minimum 110×32' if self.size.width < 110 or self.size.height < 32 else ''
        status = Text(f'{freshness} · {state} · {mode} · unread {unread} entries · lag {lag}B{size}\n'
                      f'Local files only · {"reading" if self.busy else "idle"}{errors}')
        self.query_one('#status', Static).update(status)
        if isinstance(self.screen, RawAccess):
            self.screen.query_one('#raw_status', Static).update(status)
        output = self.query_one('#output', LogPane)
        row = self.rows.get(self.selected)
        if not row or not row.log:
            note = row.reason if row and mapping(row.data.get('owner')) else 'No local log cached for this row.'
        else:
            start = output.visible_refs[0].start if output.visible_refs else (page.start if page else 0)
            end = output.visible_refs[-1].end if output.visible_refs else (page.end if page else 0)
            runtime = reading.runtime
            fallback = '' if runtime == 'claude' else f' · {runtime}: plain/raw fallback'
            changed = 'FILE CHANGED; paused earlier generation; f latest. ' if page and reading.latest and page.generation != reading.latest.generation else ''
            notice = changed + reading.notice
            boundary = f'evicted {log.evicted_entries}; skipped {log.skipped_bytes}B; shortened {log.shortened_entries}' if log else ''
            width = max(20, self.query_one('#output').size.width)
            def line(value):
                return value if len(value) <= width else value[:width - 1] + '…'
            note = (line(f'bytes {start}–{end}{fallback}') + '\n' +
                    line(notice or 'h older pages to byte zero · p full raw file path') + '\n' +
                    line(boundary) + '\n' +
                    line(f'Rendered limit {MAX_RENDER_LINES}: {output.hidden} entries hidden; p raw access') + '\n' +
                    line(f'Read error: {log.error}' if log and log.error else
                         f'Unfinished: {log.pending_bytes}B (raw preview)' if log and log.pending_bytes else
                         'Runtime output is not a workflow outcome.'))
        self.query_one('#log_note', Static).update(Text(note))

    def action_follow(self):
        if isinstance(self.screen, RawAccess):
            return
        reading = self.reading
        reading.follow = not reading.follow
        if not reading.follow:
            self.query_one('#output', LogPane).save_anchor()
        if reading.follow and reading.latest:
            reading.page, reading.notice = reading.latest, ''
            reading.seen = reading.latest.total
            self.query_one('#output', LogPane).set_reading(reading)
        self.update_status()

    def action_raw(self):
        if isinstance(self.screen, RawAccess):
            return
        reading = self.reading
        reading.raw = not reading.raw
        self.query_one('#output', LogPane).set_reading(reading)
        self.update_status()

    def action_history(self):
        if isinstance(self.screen, RawAccess):
            return
        reading = self.reading
        output = self.query_one('#output', LogPane)
        page = reading.page
        if page is None:
            return
        if reading.latest and reading.latest.generation != page.generation:
            reading.notice = 'File changed; older page unavailable. Press f for latest.'
        else:
            end = output.visible_refs[0].start if output.visible_refs else page.start
            if not end:
                reading.notice = 'Beginning of file (byte zero).'
            else:
                reading.follow = False
                self.pending_history = (end, page.generation)
                reading.notice = 'Reading older local page…'
        self.update_status()

    def action_tab(self, tab):
        if isinstance(self.screen, RawAccess):
            return
        if self.query_one(TabbedContent).active == 'log':
            self.query_one('#output', LogPane).save_anchor()
        self.query_one(TabbedContent).active = tab

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == 'log' and self.is_mounted:
            self.call_after_refresh(self.query_one('#output', LogPane).reflow)

    def action_path(self):
        row = self.rows.get(self.selected)
        if row and row.log:
            self.push_screen(RawAccess(f'Full raw file (open with an external pager):\n{row.log}\n\n'
                                      'The u view is a bounded raw projection. The file contains all retained bytes.\n'
                                      + self.reading.notice + '\n\nEscape closes this read-only path view.'))

    def action_page_up(self):
        if isinstance(self.screen, RawAccess):
            self.screen.query_one(VerticalScroll).scroll_page_up(animate=False)
            return
        if self.query_one(TabbedContent).active == 'log':
            if self.reading.follow:
                self.action_follow()
            self.query_one('#output', LogPane).scroll_page_up(animate=False)
            self.call_after_refresh(self.query_one('#output', LogPane).save_anchor)
        else:
            self.query_one('#' + self.query_one(TabbedContent).active + ' VerticalScroll', VerticalScroll).scroll_page_up(animate=False)

    def action_page_down(self):
        if isinstance(self.screen, RawAccess):
            self.screen.query_one(VerticalScroll).scroll_page_down(animate=False)
            return
        if self.query_one(TabbedContent).active == 'log':
            self.query_one('#output', LogPane).scroll_page_down(animate=False)
            self.call_after_refresh(self.query_one('#output', LogPane).save_anchor)
        else:
            self.query_one('#' + self.query_one(TabbedContent).active + ' VerticalScroll', VerticalScroll).scroll_page_down(animate=False)

    def action_home(self):
        if isinstance(self.screen, RawAccess):
            self.screen.query_one(VerticalScroll).scroll_home(animate=False)
            return
        if self.reading.follow:
            self.action_follow()
        self.query_one('#output', LogPane).scroll_home(animate=False)
        self.call_after_refresh(self.query_one('#output', LogPane).save_anchor)

    def action_end(self):
        if isinstance(self.screen, RawAccess):
            self.screen.query_one(VerticalScroll).scroll_end(animate=False)
            return
        self.query_one('#output', LogPane).scroll_end(animate=False)
        self.call_after_refresh(self.query_one('#output', LogPane).save_anchor)
