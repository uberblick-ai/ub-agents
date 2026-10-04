"""Independent live-work scrolling and a fixed, bounded outcome viewport."""

from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static, Tree

from .view_data import mapping, outcomes_today, text


class WorkTree(Tree):
    def move_cursor(self, node, animate=False):
        # Resolve invalidated lines before Textual reads node._line.
        self.get_node_at_line(0)
        super().move_cursor(node, animate=animate)

    def action_cursor_down(self):
        self.get_node_at_line(0)
        recent = self.app.query_one(RecentActivity)
        if self.cursor_line == self.last_line and recent.visible_rows:
            recent.cursor = recent.visible_rows[0].key
            self.screen.set_focus(recent, scroll_visible=False)
            recent.refresh()
        else:
            super().action_cursor_down()


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
        return self.rows[:max(0, (self.content_size.height - 1) // 2)]

    def populate(self, rows, session):
        self.rows = [row for row in rows if row.group == 'Recent activity'
                     and row.state != 'earlier observation'][:20]
        self.today = outcomes_today(session)
        if self.cursor is None and self.rows:
            self.cursor = self.rows[0].key
        self.refresh()

    def render(self):
        width = self.content_size.width
        header = Text(f'Recent activity · {self.today} today', no_wrap=True)
        header.truncate(width, overflow='ellipsis')
        for row in self.visible_rows:
            style = '' if row.key == self.app.selected else 'dim'
            if self.has_focus and row.key == self.cursor:
                style += ' reverse'
            result = text(row.data.get('result'))
            glyph = '✗' if result in {'retry', 'blocked', 'failed', 'abandoned'} else (
                '✓' if row.data.get('completed') else '○')
            title = text(row.data.get('title'), text(mapping(row.data.get('history')).get('title'), ''))
            first = Text(f'{glyph} #{row.item} {title}', no_wrap=True)
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
            detail = Text('  ' + ' · '.join(value for value in (row.agent, when, row.reason) if value), no_wrap=True)
            detail.truncate(width, overflow='ellipsis')
            header.append('\n')
            first.stylize(style.strip())
            header.append_text(first)
            header.append('\n')
            detail.stylize(style.strip())
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
        index = (event.y - 1) // 2
        if 0 <= index < len(self.visible_rows):
            self.cursor = self.visible_rows[index].key
            self.action_select()

    def on_resize(self):
        self.refresh()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()
