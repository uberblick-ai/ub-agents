"""Independent live-work scrolling and a fixed, bounded outcome viewport."""

from datetime import datetime, timezone

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.geometry import Region, Size
from textual.strip import Strip
from textual.widgets import Static, Tree

from .view_data import item_handoff, mapping, outcomes_today, rows, text
from .view_spinner import SPINNER_FPS, spinner_frame
from .attention import attention_state, waiting_time
from .view_theme import SECTION_COLORS, item_reference, theme_style


def section_rule(label, width, style):
    heading = Text(label, style=style, no_wrap=True)
    heading.truncate(width, overflow='ellipsis')
    if heading.cell_len < width:
        heading.append(' ' + '┄' * max(0, width - heading.cell_len - 1))
    return heading


def assignment_claim_time(row):
    if not row.run:
        return None
    # Published histories omit run IDs; the agent and lease expiry identify
    # the assignment's run using only the existing cached snapshot.
    expiry = row.data.get('lease_expires')
    history = rows(mapping(row.data.get('history')).get('runs'), 20)
    claim = next((run for run in reversed(history) if run.get('agent') == row.agent
                  and (not expiry or run.get('expires') == expiry)), {})
    # After a report, history.time is the outcome time rather than claim time.
    # Keep a previously observed claim in the view, never reset to report time.
    if claim.get('acceptance'):
        return None
    try:
        stamp = datetime.fromisoformat(claim['time'].replace('Z', '+00:00'))
        return stamp if stamp.tzinfo is not None else None
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def assignment_elapsed(row, now=None, claimed_at=None):
    stamp = claimed_at or assignment_claim_time(row)
    if stamp is None:
        return 'claiming'
    elapsed = max(0, int(((now or datetime.now(timezone.utc)) - stamp).total_seconds()))
    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def failure_count(data):
    failures, maximum = data.get('failures'), data.get('max_attempts')
    return (f'{failures}/{maximum} failures'
            if type(failures) is int and failures > 0 and type(maximum) is int else '')


def work_lines(row, width, *, next_row=False, stopping=False, now=None, claimed_at=None, app=None):
    """Two cell-bounded lines for one logical live-work row."""
    if width <= 0:
        return Text('', no_wrap=True), Text('', no_wrap=True)
    own = row.key.startswith('assignment:')
    attention = row.group == 'Needs attention' and row.state != 'earlier observation'
    if row.state == 'earlier observation':
        glyph, state = '○', row.state
    elif own:
        now = now or datetime.now(timezone.utc)
        glyph, state = (('■', 'stopping') if stopping else
                        (spinner_frame(now.timestamp()), assignment_elapsed(row, now, claimed_at)))
    elif row.state in {'backoff', 'waiting'}:
        glyph, state = '◷', row.state
    elif row.group == 'Eligible':
        glyph, state = '●', 'held' if stopping else 'next' if next_row else row.state
    elif attention:
        glyph, state = attention_state(row)
    else:
        glyph, state = '!', row.state
    history = mapping(row.data.get('history'))
    kind = row.data.get('kind') or history.get('kind')
    title = text(row.data.get('title') or history.get('title'), '')
    first = Text(f'{glyph} ', no_wrap=True)
    first.append_text(item_reference(row.item, kind, app=app))
    first.append(f' {title}' if title else '')
    right = (waiting_time(row.data.get('waiting_since'), now.timestamp() if now is not None else None)
             if attention else text(state, ''))
    status = Text(right if width > 1 else '', no_wrap=True,
                  style=theme_style(app, 'view-attention') if attention else '')
    status.truncate(max(0, width // 2), overflow='ellipsis')
    room = width - status.cell_len - 1
    if room <= 0:
        first = Text('', no_wrap=True)
    else:
        first.truncate(room, overflow='ellipsis')
    first.append(' ' * max(0, width - first.cell_len - status.cell_len))
    first.append_text(status)
    ownership = 'this launcher' if own else ''
    count = ('finishing run' if own and stopping else
             f'attempt {row.data["attempt"]}' if own and type(row.data.get('attempt')) is int else
             failure_count(row.data) if not own else '')
    detail = Text('  ' + ' · '.join(part for part in (text(row.data.get('agent'), ''), ownership, count)
                                 if part), no_wrap=True)
    if attention:
        detail = Text('  ' + ' · '.join(part for part in
                      (row.agent, text(state, ''), text(row.data.get('attention_reason'), '')) if part), no_wrap=True)
    elif len(row.eligible_plans) > 1:
        detail = Text('  ' + ', '.join(' '.join(part for part in
                      (text(plan.get('agent'), ''), failure_count(plan)) if part)
                      for plan in row.eligible_plans), no_wrap=True)
    detail.truncate(max(0, width), overflow='ellipsis')
    return first, detail


class WorkTree(Tree):
    """One Tree node per row, one or two lines, using pinned Textual 8.2.8."""

    @property
    def row_height(self):
        return 1 if self.app.narrow else 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.claim_times = {}
        self._spacer_lines = set()

    def on_mount(self):
        self.set_interval(1 / SPINNER_FPS, self.refresh)

    def remember_claims(self, work):
        self.claim_times = {key: stamp for key, stamp in self.claim_times.items() if key in work}
        for row in work.values():
            if row.key.startswith('assignment:') and row.key not in self.claim_times:
                stamp = assignment_claim_time(row)
                if stamp is not None:
                    self.claim_times[row.key] = stamp

    def _build(self):
        self._spacer_lines = set()
        super()._build()
        lines = []
        previous = None
        for line in self._tree_lines_cached:
            if (self.row_height == 2 and previous is not None
                    and previous.node.data is not None and line.node.data is not None
                    and previous.node.parent is line.node.parent):
                # Keep Textual's line cache intact, but make this copy inert.
                self._spacer_lines.add(len(lines))
                lines.append(previous)
            line.node._line = len(lines)
            lines.append(line)
            if line.node.data is not None and self.row_height == 2:
                lines.append(line)
            previous = line
        self._tree_lines_cached = lines
        self.virtual_size = Size(self.scrollable_content_region.width, len(lines))
        if self.cursor_node is not None:
            self.cursor_line = self.cursor_node._line

    def get_node_at_line(self, line_no):
        node = super().get_node_at_line(line_no)
        return None if line_no in self._spacer_lines else node

    def _get_node(self, line):
        return None if line == -1 else self.get_node_at_line(line)

    def _get_label_region(self, line):
        node = self.get_node_at_line(line)
        if node is None:
            return None
        return Region(0, node._line, self.scrollable_content_region.width,
                      self.row_height if node.data is not None else 1)

    def render_line(self, y):
        line_no = y + self.scroll_offset.y
        node = self.get_node_at_line(line_no)
        width = self.scrollable_content_region.width
        style = self.rich_style
        if node is None:
            return Strip.blank(width, style)
        label_style = self.get_component_rich_style('tree--label', partial=True)
        if self.hover_line >= 0 and self.get_node_at_line(self.hover_line) is node:
            label_style += self.get_component_rich_style('tree--highlight', partial=True)
        if self.cursor_node is node:
            label_style += self.get_component_rich_style('tree--cursor', partial=False)
        row = self.app.rows.get(node.data)
        if row:
            eligible = next((value.key for value in self.app.rows.values()
                             if value.group == 'Eligible' and value.state in {'ready', 'recover'}), None)
            stopping = mapping(self.app.session.data.get('activity')).get('state') == 'stopping'
            value = work_lines(row, width, next_row=row.key == eligible, stopping=stopping,
                               now=(datetime.fromtimestamp(self.app.descriptions.clock(), timezone.utc)
                                    if row.group == 'Needs attention' else None),
                               claimed_at=self.claim_times.get(row.key), app=self.app)[line_no != node._line]
        elif node is self.app.idle_node:
            label_style += theme_style(self.app, 'view-muted', dim=True)
            value = node.label.copy()
            value.truncate(width, overflow='ellipsis')
        else:
            label_style = theme_style(self.app, SECTION_COLORS.get(node.label.plain.split(' · ')[0], 'view-muted'))
            value = section_rule(node.label.plain, width, label_style)
        line_style = label_style + Style(meta={'line': line_no, 'node': node.id})
        value.stylize_before(line_style)
        if row:
            if line_no == node._line:
                value.stylize(theme_style(self.app, SECTION_COLORS.get(row.group, 'view-muted')), 0, 1)
            else:
                value.stylize(theme_style(self.app, 'view-muted', dim=True))
        return Strip(list(value.render(self.app.console))).extend_cell_length(width, style + line_style)

    def move_cursor(self, node, animate=False):
        # Resolve invalidated lines before Textual reads node._line.
        self.get_node_at_line(0)
        super().move_cursor(node, animate=animate)

    def action_cursor_up(self):
        self.get_node_at_line(0)
        node = self.cursor_node
        line = node._line - 1 if node else self.last_line
        while line > 0 and self.get_node_at_line(line) is None:
            line -= 1
        self.move_cursor(self.get_node_at_line(max(0, line)))

    def action_cursor_down(self):
        self.get_node_at_line(0)
        recent = self.app.query_one(RecentActivity)
        node = self.cursor_node
        line = node._line + (self.row_height if node.data is not None else 1) if node else 0
        while line <= self.last_line and self.get_node_at_line(line) is None:
            line += 1
        if line > self.last_line and recent.visible_rows:
            recent.cursor = recent.visible_rows[0].key
            self.screen.set_focus(recent, scroll_visible=False)
            recent.refresh()
        else:
            self.move_cursor(self.get_node_at_line(min(line, self.last_line)))


class RecentActivity(Static, can_focus=True):
    BINDINGS = [Binding('up', 'previous', show=False),
                Binding('down', 'next', show=False),
                Binding('enter', 'select', show=False)]

    def __init__(self):
        super().__init__('', id='recent', markup=False)
        self.rows = []
        self.cursor = None
        self.today = 0

    @property
    def visible_rows(self):
        available = max(0, self.content_size.height - 1)
        return self.rows[:(available + self.row_spacing) // self.row_stride]

    @property
    def row_height(self):
        return 1 if self.app.narrow else 2

    @property
    def row_spacing(self):
        return 0 if self.app.narrow else 1

    @property
    def row_stride(self):
        return self.row_height + self.row_spacing

    def populate(self, rows, session):
        self.rows = [row for row in rows if row.group == 'Recent activity'
                     and row.state != 'earlier observation'][:20]
        self.today = outcomes_today(session)
        if self.cursor is None and self.rows:
            self.cursor = self.rows[0].key
        self.refresh()

    def render(self):
        width = self.content_size.width
        header = section_rule(f'Recent activity · {self.today} today', width,
                              theme_style(self.app, 'view-muted'))
        for index, row in enumerate(self.visible_rows):
            style = theme_style(self.app, 'foreground' if row.key == self.app.selected else 'view-muted',
                                dim=row.key != self.app.selected)
            if self.has_focus and row.key == self.cursor:
                style += theme_style(self.app, 'view-accent',
                                     bgcolor=self.app.theme_variables['view-selection'])
            result = text(row.data.get('result'))
            glyph = '✗' if result in {'retry', 'blocked', 'failed', 'abandoned'} else (
                '✓' if row.data.get('completed') else '○')
            history = mapping(row.data.get('history'))
            title = text(row.data.get('title'), text(history.get('title'), ''))
            kind = row.data.get('kind') or history.get('kind')
            first = Text(f'{glyph} ', no_wrap=True)
            first.append_text(item_reference(row.item, kind, app=self.app))
            first.append(f' {title}')
            status = Text(result, no_wrap=True)
            status.truncate(max(0, width // 2), overflow='ellipsis')
            first.truncate(max(0, width - status.cell_len - 1), overflow='ellipsis')
            first.append(' ' * max(1, width - first.cell_len - status.cell_len))
            first.append_text(status)
            try:
                stamp = datetime.fromisoformat(row.data['time'].replace('Z', '+00:00'))
                when = stamp.astimezone().strftime('%H:%M') if stamp.tzinfo else ''
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                when = ''
            detail = Text('  ' + ' · '.join(value for value in (row.agent, when) if value), no_wrap=True)
            handoff = item_handoff(row)
            if handoff is not None:
                detail.append(' · opened ')
                detail.append_text(item_reference(handoff, 'pr', app=self.app))
            summary = text(row.data.get('summary'), '')
            if summary:
                detail.append(' · ' + summary)
            detail.truncate(width, overflow='ellipsis')
            if index and self.row_spacing:
                header.append('\n')
            header.append('\n')
            first.stylize_before(style)
            header.append_text(first)
            if self.row_height == 2:
                header.append('\n')
                detail.stylize_before(style)
                header.append_text(detail)
        return header

    def action_previous(self):
        keys = [row.key for row in self.visible_rows]
        index = keys.index(self.cursor) if self.cursor in keys else 0
        if index:
            self.cursor = keys[index - 1]
            self.refresh()
        else:
            tree = self.app.query_one(WorkTree)
            tree.get_node_at_line(0)
            if tree.root.children:
                tree.move_cursor(tree.get_node_at_line(tree.last_line))
                self.screen.set_focus(tree, scroll_visible=False)

    def action_next(self):
        keys = [row.key for row in self.visible_rows]
        if keys:
            index = keys.index(self.cursor) + 1 if self.cursor in keys else 0
            self.cursor = keys[min(index, len(keys) - 1)]
            self.refresh()

    def action_select(self):
        if self.cursor in {row.key for row in self.visible_rows}:
            self.app.select(self.cursor)

    def on_click(self, event):
        index, offset = divmod(event.y - 1, self.row_stride)
        if 0 <= index < len(self.visible_rows) and offset < self.row_height:
            self.cursor = self.visible_rows[index].key
            self.action_select()

    def on_resize(self):
        self.refresh()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()
