"""Run from repository root: .venv/bin/python -m experiments.terminal_observer.demo"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane, Tree

from .logs import Tail, MAX_ENTRIES
from .model import Observer, GROUPS, group


class View(App):
    TITLE = '#97 experimental observer'
    CSS = '''
    #work { width: 34%; min-width: 20; border: solid $accent; }
    #panes { width: 1fr; }
    #status { height: auto; max-height: 4; }
    RichLog { height: 1fr; }
    TabPane { padding: 0 1; }
    '''
    BINDINGS = [('q', 'quit', 'Close view'), ('r', 'snapshot', 'Refresh'),
                Binding('ctrl+c', 'quit', show=False, priority=True),
                ('f', 'follow', 'Follow'), ('u', 'raw', 'Raw'),
                ('w', 'quota', 'Quota demo'), ('1', "tab('log')", 'Log'),
                ('2', "tab('issue')", 'Issue'), ('3', "tab('runs')", 'Runs'),
                Binding('pageup', 'older', 'Scroll up', priority=True), Binding('pagedown', 'newer', 'Scroll down', priority=True)]

    def __init__(self, observer, replay=False):
        super().__init__()
        self.observer = observer
        # The selected lease, not its issue number, locates a local log.
        self.tail = None
        self.replay = replay
        self.selected = None
        self.follow = True
        self.raw = False
        self.refreshing = False
        self.seen = -1
        self.render_width = 0
        self.replay_index = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Tree('Work snapshot', id='work')
            with TabbedContent(id='panes'):
                with TabPane('Log', id='log'):
                    yield Static('Select local running work. Runtime output is untrusted evidence.', id='log_note')
                    yield RichLog(id='output', max_lines=400, wrap=True, min_width=1, markup=False, auto_scroll=True)
                with TabPane('Issue', id='issue'):
                    yield Static('', id='issue_text', markup=False)
                with TabPane('Runs', id='runs'):
                    yield Static('', id='runs_text', markup=False)
        yield Static('', id='status', markup=False)
        yield Footer()

    async def on_mount(self):
        await self.action_snapshot()
        self.set_interval(0.1, self.tick)

    async def action_snapshot(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.footer_state()
        try:
            changed = await asyncio.to_thread(self.observer.refresh)
            if changed:
                self.populate()
        finally:
            self.refreshing = False
            self.footer_state()

    def populate(self):
        tree = self.query_one('#work', Tree)
        previous = (self.selected['number'], self.selected['agent']) if self.selected else None
        self.selected = None
        tree.clear()
        for name in GROUPS:
            bucket = tree.root.add(name, expand=name != 'Recent activity')
            for row in self.observer.rows:
                if group(row) != name:
                    continue
                title = self.observer.details.get(row['number'], ('', ''))[0]
                node = bucket.add_leaf(Text(f"#{row['number']} {title or row['agent']}"), data=row)
                if previous == (row['number'], row['agent']):
                    self.selected = row
                    tree.select_node(node)
        if not self.selected and self.observer.rows:
            self.selected = self.observer.rows[0]
        tree.root.expand()
        tree.focus()
        self.render_selection()

    def on_tree_node_selected(self, event):
        if event.node.data:
            self.selected = event.node.data
            self.render_selection()

    def render_selection(self):
        row = self.selected
        if not row:
            return
        title, body = self.observer.details.get(row['number'], ('Issue details not loaded in live spike', ''))
        self.query_one('#issue_text', Static).update(Text(f"#{row['number']} {title}\n\nState: {row['state']}\n{row['reason']}\nBlockers: {', '.join(row['open_blockers']) or 'none'}\n\n{body}"))
        lease = row['lease']
        outcome = row['outcome']
        run_text = f"Attempts: {row['attempts']}\nLast result: {row['result'] or 'none'}\n"
        if lease:
            run_text += f"Owner: @{lease['actor']} on {lease.get('host', 'unknown')}\nRun: {lease['run']}\nExpires: {lease['expires']}\n"
        if outcome:
            accepted = 'ACCEPTED workflow outcome' if outcome.get('accepted') else 'Reported; UNACCEPTED'
            run_text += f"\n{accepted}: {outcome['status']}\n{outcome['summary']}\nEvent time: {outcome['created']}\nTransition complete: {outcome.get('transition_complete', False)}"
        else:
            run_text += '\nNo accepted outcome for this row’s selected run.'
        run_text += '\n\nRuntime tool output is never a workflow outcome. No stop controls.'
        self.query_one('#runs_text', Static).update(Text(run_text))
        path = self.observer.log_path(row)
        if path != (self.tail.path if self.tail else None):
            self.tail = Tail(path) if path else None
            if self.tail:
                self.tail.poll()
        self.seen = -1
        self.query_one('#output', RichLog).clear()
        self.show_log()

    def local_selected(self):
        return self.selected and self.observer.log_path(self.selected) is not None

    def show_log(self):
        if not self.is_running:
            return
        output = self.query_one('#output', RichLog)
        if self.query_one(TabbedContent).active != 'log' or output.scrollable_content_region.width <= 0:
            self.seen = -1
            return
        width = output.scrollable_content_region.width
        if width != self.render_width:
            self.seen = -1
            self.render_width = width
        if not self.local_selected() or not self.tail:
            self.query_one('#log_note', Static).update('Live log unavailable for this row. No remote log or stop control.')
            return
        buffer = self.tail.buffer
        if self.tail.error:
            self.query_one('#log_note', Static).update(Text(f'Local log unreadable: {self.tail.error}'))
            return
        try:
            raw_path = self.tail.path.relative_to(Path.cwd())
        except ValueError:
            raw_path = self.tail.path
        self.query_one('#log_note', Static).update(Text(f"Raw fallback: {raw_path}\n{'FOLLOW' if self.follow else 'PAUSED'} · {len(buffer.entries)}/{MAX_ENTRIES} records · dropped {buffer.discarded}"))
        # Append normally; a bounded redraw is needed when raw mode/selection changes.
        if self.seen < 0 or buffer.total - self.seen > len(buffer.entries):
            output.clear()
            entries = list(buffer.entries)
        else:
            entries = list(buffer.entries)[-(buffer.total - self.seen):] if buffer.total > self.seen else []
        for entry in entries:
            output.write(Text(entry.display(self.raw), no_wrap=False, overflow='fold'), width=output.scrollable_content_region.width, scroll_end=self.follow)
        self.seen = buffer.total
        # Partial records are visible promptly above the log without treating them as events.
        preview = buffer.preview()
        if preview:
            self.query_one('#log_note', Static).update(Text(f"Raw fallback: {raw_path}\nPARTIAL ({len(buffer.pending)} bytes): {preview.text[:140]}"))

    def on_tabbed_content_tab_activated(self, event):
        if event.pane.id == 'log' and self.is_mounted:
            self.seen = -1
            self.call_after_refresh(self.show_log)

    def on_resize(self):
        if self.is_mounted:
            self.seen = -1
            self.call_after_refresh(self.show_log)

    def tick(self):
        if not self.is_running:
            return
        if self.replay and self.tail:
            self.replay_index += 1
            i = self.replay_index
            events = [
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'Claude message {i}: inspecting a local file'}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'fixture-tool', 'content': f'Tool result {i}: completed (not a workflow outcome)'}]}},
                {'type': 'future.event', 'payload': 'unfamiliar event; raw preserved'},
                {'type': 'error', 'message': 'synthetic recoverable error'},
            ]
            record = (f'exec: synthetic Codex human output {i}\n' if i % 5 == 0 else json.dumps(events[i % len(events)]) + '\n').encode()
            with self.tail.path.open('ab') as stream:
                stream.write(record)
        if self.tail:
            self.tail.poll()
            self.show_log()
        self.footer_state()

    def footer_state(self):
        if not self.is_running:
            return
        o = self.observer
        refreshed = datetime.fromtimestamp(o.refreshed, timezone.utc).strftime('%H:%M:%S UTC') if o.refreshed else 'none'
        cost = f'gh calls {o.last_requests}; REST {o.last_rest}; charged {o.last_quota}; GraphQL {o.last_graphql}' if o.live else 'fixture store; no transport'
        self.query_one('#status', Static).update(Text(f"{'Refreshing' if self.refreshing else 'Snapshot'}: {refreshed} · {cost}\n{o.poll} · tail 100ms; selection/tab changes use snapshot"))

    def action_tab(self, tab):
        self.query_one(TabbedContent).active = tab

    def action_follow(self):
        self.follow = not self.follow
        self.query_one('#output', RichLog).auto_scroll = self.follow
        if self.follow:
            self.query_one('#output', RichLog).scroll_end(animate=False)
        self.show_log()

    def action_raw(self):
        self.raw = not self.raw
        self.seen = -1
        self.show_log()

    def action_quota(self):
        self.observer.simulate_quota()
        self.footer_state()

    def action_older(self):
        if self.follow:
            self.action_follow()
        self.query_one('#output', RichLog).scroll_page_up(animate=False)

    def action_newer(self):
        self.query_one('#output', RichLog).scroll_page_down(animate=False)


def main():
    parser = argparse.ArgumentParser(description='Isolated #97 observer experiment')
    parser.add_argument('--log', type=Path, help='Replay file named process.log, bound into a fixture lease')
    parser.add_argument('--config', type=Path, help='Read-only live status and same-host lease logs (end-to-end unverified)')
    args = parser.parse_args()
    if args.config:
        View(Observer(Path.cwd(), args.config)).run()
    elif args.log:
        if args.log.name != 'process.log':
            parser.error('--log must be named process.log, as in a real run directory')
        View(Observer(Path.cwd(), log_path=args.log)).run()
    else:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'process.log'
            path.touch()
            View(Observer(Path.cwd(), log_path=path), replay=True).run()


if __name__ == '__main__':
    main()
