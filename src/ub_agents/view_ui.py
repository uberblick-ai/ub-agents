"""Optional Textual two-pane UI. Imported only by the view process."""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from queue import Empty
import time

from markdown_it import MarkdownIt
from rich.segment import Segment
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TabbedContent, TabPane, Tabs, Tree

from .view_data import (WORK_GROUPS, context_header, context_text, item_header, item_history, mapping,
                        run_status, text)
from .view_github import DescriptionLoads
from .view_runs import run_status as history_status, runs_view
from .view_worker import LocalWorker, Request
from .view_work import RecentActivity, WorkTree
from .updates import release_age

MAX_RENDER_LINES = 400


class UpdateBanner(Static):
    """A single inert row; Rich measures truncation in terminal cells."""
    def __init__(self):
        super().__init__('', id='update', markup=False)
        self.banner = {}

    def set_banner(self, banner):
        banner = mapping(banner)
        self.display = bool(banner.get('text')) and isinstance(banner.get('text'), str)
        if self.banner != banner:
            self.banner = banner
            self.refresh()

    def render(self):
        width = max(0, self.content_size.width)
        line = Text(text(self.banner.get('text'), ''), no_wrap=True)
        age = release_age(self.banner.get('released_at')) if width >= 60 else ''
        room = max(0, width - len(age) - 2) if age else width
        line.truncate(room, overflow='ellipsis')
        if age:
            line.append(' ' * max(2, width - line.cell_len - len(age)) + age)
        return line


def pane_line(value, width, style=''):
    line = Text(text(value, ''), style=style)
    line.truncate(max(0, width), overflow='ellipsis')
    return line


class ItemTabs(TabbedContent):
    """One header below the tab bar, shared even by subsequently added tabs."""

    def compose(self):
        for widget in super().compose():
            yield widget
            if isinstance(widget, Tabs):
                yield Static('', id='item_header', markup=False)


def description_parser():
    # Show links, images and HTML as source text, without interactive targets.
    # Entities stay literal so parsing cannot introduce escaped control characters.
    parser = MarkdownIt('commonmark', {'html': False}).disable(
        ['link', 'autolink', 'image', 'reference', 'entity'])

    def line_breaks(state):
        # Textual otherwise renders Markdown soft breaks as spaces.
        for token in state.tokens:
            for child in token.children or ():
                if child.type == 'softbreak':
                    child.type = 'hardbreak'

    parser.core.ruler.after('inline', 'description_line_breaks', line_breaks)
    return parser


class RawAccess(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss', 'Close', priority=True)]
    DEFAULT_CSS = 'RawAccess { padding: 2 4; } RawAccess VerticalScroll { background: $panel; padding: 1 2; } #raw_status { height: 1; background: $panel; }'
    footer_keys = 'Esc close ? keys q quit'

    def __init__(self, message=None):
        super().__init__()
        self.message = message

    def compose(self):
        # Read the view's state now: updates that land between the push and
        # this compose find no widgets to update.
        with VerticalScroll():
            yield Static(Text(self.message if self.message is not None else self.app.raw_details()), id='raw_details')
        yield Static(self.app.footer(self.app.size.width - self.styles.padding.width, self.footer_keys),
                     id='raw_status', markup=False)

    def action_dismiss(self):
        self.dismiss()


class KeyHelp(RawAccess, inherit_bindings=False):
    BINDINGS = [Binding('escape,question_mark', 'dismiss', 'Close', priority=True)]
    footer_keys = 'Esc/? close q quit'

    def __init__(self):
        super().__init__(
            'Keys\n\n'
            'Tab / arrows / Enter   Focus a pane and select a work row\n'
            '1 / 2 / 3   Log / Issue / Runs\n'
            'g on Issue   Load a missing description or retry a failed read\n'
            'f   Toggle follow/pause; resuming loads the latest generation\n'
            'h   Read an older bounded page toward byte zero\n'
            'u   Toggle formatted/raw projection of the same page\n'
            'p   Show the full raw path and log diagnostics; Escape closes it\n'
            'Page Up / Page Down / Home / End   Scroll; scrolling up pauses follow\n'
            '?   Open or close this help; Escape also closes it\n'
            'q   Close only the view; launcher continues with plain output\n'
            'Ctrl-C   Interrupt an attached launcher; close a standalone view')


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


class LogPane(ScrollView):
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
            rich.stylize(style, start, end)
        lines = []
        for line in rich.split('\n') if rich.plain else ():
            if any(span.style in ('dim italic', 'italic') for span in line.spans):
                # Wrapped assistant continuations share the explicit LF indent.
                column, body = line[:10], line[10:]
                for index, part in enumerate(body.wrap(self.app.console, width - 10, overflow='fold')):
                    lines.append((column if index == 0 else Text(' ' * 10, style=self.rich_style)) + part)
            else:
                counts = [span.start for span in line.spans if span.style in ('green', 'red')
                          and line.plain[span.start:span.end].lstrip('+-').isdigit()]
                if counts:
                    start = min(counts) - 1
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

    def reflow(self):
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
        self.call_after_refresh(self.restore, reading, anchor, target)

    def restore(self, reading, anchor, target):
        if self.reading is not reading:
            return
        self.held_anchor = self.held_y = None
        if reading and reading.follow:
            self.scroll_end(animate=False, immediate=True)
        elif anchor and self.positions:
            candidates = [(index, position) for index, position in enumerate(self.positions) if position[0] == target]
            if candidates:
                y = min(candidates, key=lambda item: abs(item[1][1] - anchor[1]))[0]
                self.scroll_to(y=y, animate=False, immediate=True)
        elif not anchor:
            self.scroll_home(animate=False, immediate=True)
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


class View(App):
    TITLE = 'ub-agents · one launcher · read-only view'
    CSS = '''
    #update { height: 1; padding: 0 1; background: #d7af00; color: #161616; display: none; overflow: hidden; }
    #body { height: 1fr; }
    #work_pane { width: 36; border: solid $accent; }
    #work { height: 1fr; }
    #recent { height: 1fr; overflow: hidden; }
    #panes { width: 1fr; }
    #panes > ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 0 1; }
    #item_header { height: 3; padding: 0 1; overflow: hidden; }
    #log_note { height: 1; overflow: hidden; }
    #output { height: 1fr; scrollbar-gutter: stable; overflow-x: hidden; }
    #run_status { height: 2; overflow: hidden; }
    #log_state { height: 1; content-align: right middle; }
    #issue_body { padding: 0; }
    #status { height: 1; background: $panel; }
    '''
    BINDINGS = [
        Binding('q', 'quit', 'Close view', priority=True),
        Binding('ctrl+c', 'interrupt', 'Interrupt', priority=True),
        Binding('f', 'follow', 'Follow/pause', priority=True),
        Binding('u', 'raw', 'Raw', priority=True),
        Binding('h', 'history', 'Older page', priority=True),
        Binding('p', 'path', 'Full raw path', priority=True),
        Binding('question_mark', 'help', 'Keys', priority=True),
        Binding('g', 'load_description', 'Load/retry description', priority=True),
        Binding('1', "tab('log')", 'Log', priority=True),
        Binding('2', "tab('issue')", 'Issue', priority=True),
        Binding('3', "tab('runs')", 'Runs', priority=True),
        Binding('pageup', 'page_up', 'Page up', priority=True),
        Binding('pagedown', 'page_down', 'Page down', priority=True),
        Binding('home', 'home', 'Top', priority=True),
        Binding('end', 'end', 'Bottom', priority=True),
    ]

    def __init__(self, root, session_path, worker=None, descriptions=None, launcher=None):
        super().__init__()
        self.launcher = launcher
        self.worker = worker or LocalWorker(root, session_path)
        self.descriptions = descriptions or DescriptionLoads()
        self.local_description = None
        self.session = None
        self.rows, self.nodes, self.reason_nodes, self.groups = {}, {}, {}, {}
        self.selected = None
        self.chosen = False  # a person picked a row; the view stops following the run
        self.readings = {}
        self.token = 0
        self.busy = False
        self.pending_history = None
        self.last_context = self.last_runs = None

    def compose(self) -> ComposeResult:
        yield UpdateBanner()
        with Horizontal(id='body'):
            with Vertical(id='work_pane'):
                yield WorkTree('Launcher work', id='work')
                yield RecentActivity()
            with ItemTabs(id='panes'):
                with TabPane('Log', id='log'):
                    yield Static('', id='log_note', markup=False)
                    yield LogPane(id='output')
                    yield Static('', id='log_state', markup=False)
                    yield Static('', id='run_status', markup=False)
                with TabPane('Issue', id='issue'):
                    with VerticalScroll():
                        yield Static('Context unavailable.', id='issue_text', markup=False)
                        yield Markdown('', id='issue_body', parser_factory=description_parser, open_links=False)
                        yield Static('', id='issue_note', markup=False)
                with TabPane('Runs', id='runs'):
                    with VerticalScroll():
                        yield Static('Select an item to see its history.', id='runs_text', markup=False)
        yield Static('', id='status', markup=False)

    def on_mount(self):
        self.worker.start()
        self.set_interval(0.1, self.tick)
        self.tick()
        if self.launcher is not None:
            self.launcher.mounted(self)

    def action_interrupt(self):
        if self.launcher is not None:
            self.launcher.interrupt()
        self.exit()

    def on_unmount(self):
        self.descriptions.close()
        self.worker.close()
        if self.launcher is not None:
            self.launcher.close()

    @property
    def reading(self):
        return self.readings.setdefault(self.selected, Reading())

    def tick(self):
        self.descriptions.poll()
        try:
            result = self.worker.results.get_nowait()
        except Empty:
            result = None
        if result:
            self.busy = False
            self.session = result.session
            self.query_one(UpdateBanner).set_banner(self.session.data.get('update'))
            self.populate(result.rows)
            if result.token == self.token and result.key == self.selected:
                self.apply(result)
        if not self.busy:
            end, generation = self.pending_history or (None, 0)
            if self.worker.request(Request(self.selected, self.token, end, generation)):
                self.busy = True
                self.pending_history = None
        self.update_issue()
        self.update_runs()
        self.update_status()

    def populate(self, rows):
        tree = self.query_one('#work', Tree)
        recent = self.query_one(RecentActivity)
        recent.populate(rows, self.session)
        cursor = tree.cursor_node
        cursor_reason = cursor is not None and cursor is self.reason_nodes.get(cursor.data)
        cursor_group = next((name for name, node in self.groups.items() if node is cursor), None)
        incoming = {row.key: row for row in rows}
        # Until a person picks a row, the view follows the launcher's own run,
        # which can start after the view first read the snapshot.
        own = next((key for key in incoming if key.startswith('assignment:')), None)
        follow = own is not None and not self.chosen and own != self.selected
        # A selected row disappearing from a snapshot is retained as an earlier
        # observation; updates never replace a picked row or steal pane focus.
        if self.selected in self.rows and self.selected not in incoming and not follow:
            incoming[self.selected] = self.rows[self.selected]
        for key in tuple(self.nodes):
            if key not in incoming:
                self.nodes.pop(key).remove()
                self.reason_nodes.pop(key, None)
        previous = None
        for name in WORK_GROUPS[:-1]:
            grouped = [row for row in incoming.values() if row.group == name]
            if not grouped:
                continue
            group = self.groups.get(name)
            if group is None:
                group = self.groups[name] = tree.root.add(
                    Text(name), after=previous, before=0 if previous is None else None,
                    expand=True)
            previous = group
            group.set_label(Text(f'{name} · {len(grouped)}'))
            for index, row in enumerate(grouped):
                node = self.nodes.get(row.key)
                expanded = node.is_expanded if node else True
                if node is not None and (node.parent is not group or group.children[index] is not node):
                    node.remove()
                    node = None
                if node is None:
                    node = self.nodes[row.key] = group.add(Text(row.label()), data=row.key,
                                                          before=index, expand=expanded)
                    self.reason_nodes[row.key] = node.add_leaf(Text(row.reason), data=row.key)
                else:
                    node.set_label(Text(row.label()))
                    self.reason_nodes[row.key].set_label(Text(row.reason))
        for name, group in tuple(self.groups.items()):
            if not group.children:
                group.remove()
                del self.groups[name]
        self.rows = incoming
        tree.root.expand()
        title = Text('Launcher work')
        latest = mapping(self.session.data.get('latest_pass'))
        if latest and latest.get('state') != 'complete':
            title.append(' · ' + text(latest.get('state'), 'partial'), style='dim')
        omitted = mapping(self.session.data.get('omitted')).get('plans', 0)
        if omitted:
            title.append(f' · omitted {text(str(omitted))}', style='dim')
        tree.root.set_label(title)
        if follow or self.selected is None and incoming:
            first = next(iter(incoming.values()))
            self.select(own if follow else first.key, chosen=False)
            if self.selected in self.nodes:
                tree.move_cursor(self.nodes[self.selected])
            else:
                recent.cursor = self.selected
                recent.focus()
        elif cursor:
            target = (self.groups.get(cursor_group) if cursor_group else
                      (self.reason_nodes if cursor_reason else self.nodes).get(cursor.data))
            if target is not None and target is not cursor:
                tree.move_cursor(target)

    def move_cursor(self, node):
        self.query_one('#work', Tree).move_cursor(node)

    def on_tree_node_selected(self, event):
        if event.node.data in self.rows:
            self.select(event.node.data)

    def select(self, key, chosen=True):
        self.chosen = self.chosen or chosen
        if key == self.selected:
            return
        self.selected = key
        self.query_one(RecentActivity).refresh()
        self.token += 1
        self.pending_history = None
        self.query_one('#output', LogPane).set_reading(self.reading)
        self.last_context = None
        self.last_runs = None
        self.local_description = None
        self.query_one('#issue_text', Static).update('Reading cached context…')
        self.update_runs()
        self.query_one('#issue_body', Markdown).update('')
        self.query_one('#issue_note', Static).update('')
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
        self.local_description = result.description

    def update_runs(self):
        if self.session is None:
            return
        row = self.rows.get(self.selected)
        history = item_history(row, self.session)
        now = datetime.now().astimezone()
        active = any(history_status(run, now)[0] == 'running' for run in history.get('runs', []))
        signature = (row.key if row else None, repr(history), int(now.timestamp() * (5 if active else 1)))
        if signature != self.last_runs:
            self.query_one('#runs_text', Static).update(runs_view(row, self.session))
            self.last_runs = signature

    def description_key(self):
        row = self.rows.get(self.selected)
        return self.descriptions.key(self.session.data.get('repository'), row.item) if self.session and row else None

    def update_issue(self):
        if self.local_description is None:
            return
        key = self.description_key()
        local = self.local_description
        description = local if local.available else (self.descriptions.get(key) or local)
        row = self.rows.get(self.selected)
        details = description.details()
        extra = ''
        if self.descriptions.pending == key and key is not None:
            extra += '\n\nLoading title/body from GitHub…'
        elif not description.available:
            extra += '\n\nPress g on Issue to ' + ('retry' if description.error else 'load') + ' title/body from GitHub.'
        if self.descriptions.clock() < self.descriptions.cooldown:
            reset = datetime.fromtimestamp(self.descriptions.cooldown, timezone.utc).isoformat()
            extra += f'\nGitHub cooldown until {reset}; no loads or retries before then.'
        elif self.descriptions.pending is not None and self.descriptions.pending != key:
            extra += '\nAnother description read is pending; no requests are queued.'
        value = context_text(row, description) + extra
        if value != self.last_context:
            self.query_one('#issue_text', Static).update(Text(context_header(row, description)))
            body = description.body if row and description.available and not description.error else ''
            markdown = self.query_one('#issue_body', Markdown)
            if body != markdown.source:
                markdown.update(body)
            self.query_one('#issue_note', Static).update(Text(details + extra))
            self.last_context = value

    def current_description(self):
        local = self.local_description
        return local if local and local.available else (self.descriptions.get(self.description_key()) or local)

    def raw_details(self):
        row = self.rows.get(self.selected)
        reading = self.reading
        page, log = reading.page, reading.log
        output = self.query_one('#output', LogPane)
        start = output.visible_refs[0].start if output.visible_refs else (page.start if page else 0)
        end = output.visible_refs[-1].end if output.visible_refs else (page.end if page else 0)
        details = [f'Full raw file (open with an external pager):\n{text(str(row.log), "")}' if row and row.log else '',
                   'The u view is a bounded raw projection. The file contains all retained bytes.',
                   f'Displayed bytes {start}–{end}',
                   f'Rendered limit {MAX_RENDER_LINES}: {output.hidden} entries hidden; h recovers earlier bytes']
        if page:
            details.append(f'Page bytes {page.start}–{page.end} · generation {page.generation}')
        if log:
            details.append(f'evicted {log.evicted_entries}; skipped {log.skipped_bytes}B; shortened {log.shortened_entries}')
            details.append(f'unfinished {log.pending_bytes}B; file resets {log.resets}')
            if log.error:
                details.append('Read error: ' + text(log.error))
        if reading.notice:
            details.append(reading.notice)
        details.extend(('Runtime output is not a workflow outcome.', 'Escape closes this read-only raw-access view.'))
        return '\n\n'.join(part for part in details if part)

    def action_load_description(self):
        if (isinstance(self.screen, RawAccess) or self.query_one(TabbedContent).active != 'issue' or
                self.local_description is None or self.local_description.available):
            return
        self.descriptions.request(self.description_key())
        self.update_issue()

    def log_lag(self):
        reading = self.reading
        page, log = reading.page, reading.log
        unread = max(0, log.total_entries - reading.seen) if log else 0
        lag = log.unread_bytes if log else 0
        if page and reading.latest and page.generation == reading.latest.generation:
            refs = self.query_one('#output', LogPane).visible_refs
            lag += max(0, reading.latest.end - (refs[-1].end if refs else page.end))
        return unread, lag

    def footer(self, width, keys):
        parts = ['ub-agents']
        if self.session:
            version = text(self.session.data.get('base_version'), '')
            if version:
                parts[0] += f' v{version}'
        if self.size.width < 110 or self.size.height < 32:
            parts.append('minimum 110×32')
        if self.session:
            state = self.session.state()
            if state == 'malformed':
                parts.append('malformed: ' + text(self.session.error))
            elif state in {'stale', 'ended'}:
                parts.append(state)
            activity = mapping(self.session.data.get('activity'))
            value = text(activity.get('state'), '')
            if value == 'waiting':
                try:
                    until = datetime.fromisoformat(activity['until'].replace('Z', '+00:00'))
                    if until.tzinfo is not None:
                        value = f'next poll {max(0, ceil((until - datetime.now(timezone.utc)).total_seconds()))}s'
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    pass
            if value:
                parts.append(value)
        else:
            parts.append('reading session')
        # Keep the main keys intact at the supported minimum width. Below that,
        # reserve space for the minimum-size hint and session diagnostics.
        right = Text(keys if width >= 110 else '? keys q quit')
        left = Text(' · '.join(parts), no_wrap=True, overflow='ellipsis')
        left.truncate(max(0, width - right.cell_len - 1), overflow='ellipsis')
        left.append(' ' * max(1, width - left.cell_len - right.cell_len))
        left.append_text(right)
        return left

    def update_status(self):
        if not self.is_mounted:
            return
        reading = self.reading
        page, log = reading.page, reading.log
        unread, lag = self.log_lag()
        keys = ('f follow h older u raw PgUp/PgDn scroll ? keys q quit' if not reading.follow else
                '↑↓ select ⏎ open 1-3 tabs ? keys q quit')
        status = self.footer(self.size.width, keys)
        self.query_one('#status', Static).update(status)
        if isinstance(self.screen, RawAccess):
            for footer in self.screen.query('#raw_status').results(Static):
                footer.update(self.footer(footer.size.width, self.screen.footer_keys))
        pill = self.query_one('#log_state', Static)
        pill.display = bool((page or log) and (not reading.follow or unread or lag))
        parts = ['⏸ PAUSED' if not reading.follow else '↓ BEHIND']
        if unread:
            parts.append(f'{unread} new ↓')
        if lag:
            parts.append(f'{lag}B lag')
        if reading.raw:
            parts.append('RAW')
        parts.append('f follow')
        pill.update(Text(' ' + ' · '.join(parts) + ' ', style='reverse', no_wrap=True, overflow='ellipsis'))
        output = self.query_one('#output', LogPane)
        row = self.rows.get(self.selected)
        header = self.query_one('#item_header', Static)
        width = header.content_region.width
        title, metadata = item_header(row, self.current_description(), self.session)
        header_text = Text()
        header_text.append_text(pane_line(title, width, 'bold'))
        header_text.append('\n').append_text(pane_line(metadata, width, 'dim'))
        header_text.append('\n' + '┄' * width, style='dim')
        header.update(header_text)
        width = output.size.width
        notices = []
        if row and row.log:
            if page and reading.latest and page.generation != reading.latest.generation:
                notices.append('FILE CHANGED; paused earlier generation; f latest.')
            elif page and page.generation:
                notices.append('File replaced or changed generation.')
            if log and log.error:
                notices.append('Read error: ' + log.error)
            if log and log.pending_bytes:
                notices.append(f'Unfinished: {log.pending_bytes}B (raw preview)')
            if reading.notice:
                notices.append(reading.notice)
            if reading.runtime != 'claude':
                notices.append(f'{reading.runtime}: plain/raw fallback')
        note = self.query_one('#log_note', Static)
        note.display = bool(notices)
        note.update(pane_line(' · '.join(notices), width, 'bold yellow'))
        empty_message = (row.reason if row and mapping(row.data.get('owner')) else
                         'No local log cached for this row.' if not row or not row.log else 'No log output yet.')
        if reading.empty_message != empty_message:
            reading.empty_message = empty_message
            output.refresh()
        left, right, running = run_status(row, self.session)
        if running:
            left = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[int(time.monotonic() * 10) % 10] + ' ' + left
        right_line = pane_line(right, max(0, width - 1))
        left_line = pane_line(left, width - right_line.cell_len - (1 if right_line.cell_len else 0))
        status_line = left_line
        if right_line.cell_len:
            status_line.append(' ' * max(1, width - left_line.cell_len - right_line.cell_len)).append_text(right_line)
        run_note = Text('┄' * width + '\n', style='dim')
        run_note.append_text(status_line)
        self.query_one('#run_status', Static).update(run_note)
        if isinstance(self.screen, RawAccess) and not isinstance(self.screen, KeyHelp):
            for details in self.screen.query('#raw_details').results(Static):
                details.update(Text(self.raw_details()))

    def action_follow(self):
        if isinstance(self.screen, RawAccess):
            return
        reading = self.reading
        reading.follow = not reading.follow
        if not reading.follow:
            self.query_one('#output', LogPane).save_anchor()
        else:
            # A slow older-page request must not override a later resume command.
            self.token += 1
            self.pending_history = None
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
        if isinstance(self.screen, RawAccess):
            return
        row = self.rows.get(self.selected)
        if row and row.log:
            self.push_screen(RawAccess())

    def action_help(self):
        if isinstance(self.screen, KeyHelp):
            self.screen.dismiss()
        else:
            self.push_screen(KeyHelp())

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
