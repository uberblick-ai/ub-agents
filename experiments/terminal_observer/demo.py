"""Run from repository root: .venv/bin/python -m experiments.terminal_observer.demo"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane, Tree

from .logs import Tail, MAX_ENTRIES
from .model import GROUPS, group
from .local import LocalObserver, fixture_observer

MAX_RENDER_ENTRIES = 32


class View(App):
    TITLE = '#97 launcher-local experiment'
    CSS = '''
    #work { width: 34%; min-width: 20; border: solid $accent; }
    #panes { width: 1fr; }
    #status { height: auto; max-height: 4; }
    RichLog { height: 1fr; }
    TabPane { padding: 0 1; }
    '''
    BINDINGS = [('q', 'quit', 'Close view'), ('r', 'snapshot', 'Read local'),
                ('o', 'details', 'Open details'),
                Binding('ctrl+c', 'quit', show=False, priority=True),
                ('f', 'follow', 'Follow'), ('u', 'raw', 'Raw'),
                ('1', "tab('log')", 'Log'),
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
            yield Tree('Last observations', id='work')
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
        previous = self.selection_key(self.selected) if self.selected else None
        self.selected = None
        tree.clear()
        for name in getattr(self.observer, 'groups', GROUPS):
            bucket = tree.root.add(name, expand=name not in {'Recent activity', 'Local recent activity'})
            for row in self.observer.rows:
                if getattr(self.observer, 'group', group)(row) != name:
                    continue
                title = self.observer.details.get(row['number'], ('', ''))[0]
                node = bucket.add_leaf(Text(f"#{row['number']} {title or row['agent']}"), data=row)
                if previous == self.selection_key(row):
                    self.selected = row
                    tree.select_node(node)
        if not self.selected and self.observer.rows:
            self.selected = self.observer.rows[0]
        tree.root.expand()
        tree.focus()
        self.render_selection()

    @staticmethod
    def selection_key(row):
        lease = row.get('lease') or {}
        return row['number'], row['agent'], lease.get('run'), bool(row.get('local_recent'))

    def on_tree_node_selected(self, event):
        if event.node.data:
            self.selected = event.node.data
            self.render_selection()

    def render_selection(self):
        row = self.selected
        if not row:
            self.tail = None
            self.query_one('#output', RichLog).clear()
            self.query_one('#issue_text', Static).update('No item observed yet')
            self.query_one('#runs_text', Static).update('No local activity observed yet')
            return
        title, body = self.observer.details.get(row['number'], ('Issue details not loaded in live spike', ''))
        detail_state = self.observer.detail_state(row) if hasattr(self.observer, 'detail_state') else 'Legacy scan fixture'
        observed = f'Observation age: {max(0, time.time() - row["observed_at"]):.0f}s; last observation may be stale' if row.get('observed_at') else 'Observation age unavailable'
        self.query_one('#issue_text', Static).update(Text(f"#{row['number']} {title}\n\nState: {row['state']}\n{row['reason']}\n{observed}\n{detail_state}\nBlockers: {', '.join(row['open_blockers']) or 'none'}\n\n{body}"))
        lease = row['lease']
        outcome = row['outcome']
        run_text = f"Attempts: {row['attempts']}\nLast result: {row['result'] or 'none'}\n"
        if lease:
            run_text += f"Observed owner: @{lease['actor']} on {lease.get('host', 'unknown')}\nRun: {lease['run']}\nExpires: {lease['expires']}\n"
        if outcome:
            accepted = 'ACCEPTED workflow outcome' if outcome.get('accepted') else 'Reported; UNACCEPTED'
            run_text += f"\n{accepted}: {outcome['status']}\n{outcome['summary']}\nEvent time: {outcome['created']}\nTransition complete: {outcome.get('transition_complete', False)}"
        else:
            run_text += '\nNo accepted outcome for this row’s selected run.'
        run_text += '\n\nRuntime tool output is never a workflow outcome. No stop controls.'
        self.query_one('#runs_text', Static).update(Text(run_text))
        path = self.observer.log_path(row)
        changed = path != (self.tail.path if self.tail else None)
        if changed:
            self.tail = Tail(path) if path else None
            if self.tail:
                self.tail.poll()
        if changed:
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
            self.query_one('#log_note', Static).update('Runtime log unavailable. Observed ownership does not prove execution or grant log access.')
            return
        buffer = self.tail.buffer
        if self.tail.error:
            self.query_one('#log_note', Static).update(Text(f'Local log unreadable: {self.tail.error}'))
            return
        try:
            raw_path = self.tail.path.relative_to(Path.cwd())
        except ValueError:
            raw_path = self.tail.path
        self.query_one('#log_note', Static).update(Text(f"Raw fallback: {raw_path}\n{'FOLLOW' if self.follow else 'PAUSED'} · {len(buffer.entries)}/{MAX_ENTRIES} records · evicted {buffer.discarded} · last 400 rendered lines · unread {max(0, buffer.total - max(0, self.seen))}"))
        # Append normally; a bounded redraw is needed when raw mode/selection changes.
        if self.seen < 0 or buffer.total - self.seen > len(buffer.entries):
            output.clear()
            entries = list(buffer.entries)
        else:
            entries = list(buffer.entries)[-(buffer.total - self.seen):] if buffer.total > self.seen else []
        # Limit formatting work even when a single read contains many records.
        entries = entries[:MAX_RENDER_ENTRIES]
        for entry in entries:
            output.write(Text(entry.display(self.raw), no_wrap=False, overflow='fold'), width=output.scrollable_content_region.width, scroll_end=self.follow)
        if self.seen < 0 or buffer.total - self.seen > len(buffer.entries):
            self.seen = buffer.total - len(buffer.entries) + len(entries)
        else:
            self.seen += len(entries)
        # Partial records are visible promptly above the log without treating them as events.
        preview = buffer.preview()
        if preview:
            self.query_one('#log_note', Static).update(Text(f"Raw fallback: {raw_path}\n{'FOLLOW' if self.follow else 'PAUSED'} · evicted {buffer.discarded} · last 400 rendered lines · PARTIAL ({len(buffer.pending)} bytes): {preview.text[:140]}"))

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
        if isinstance(self.observer, LocalObserver) and self.observer.refresh():
            self.populate()
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
        if isinstance(o, LocalObserver):
            age = f'{max(0, time.time() - o.refreshed):.0f}s old' if o.refreshed else 'unavailable'
            discovery = 'partial' if 'partial' in o.phase or 'stopped early' in o.phase else 'observed'
            state = o.poll if 'unavailable/stale' in o.poll or 'tap error' in o.poll else f'Discovery {discovery} · local view · no background GitHub reads'
            self.query_one('#status', Static).update(Text(f'Local {age} · recent {o.recent_count}/20 · evicted {o.evicted} · detail reads {o.detail_reads}\n{state}'))
            return
        refreshed = datetime.fromtimestamp(o.refreshed, timezone.utc).strftime('%H:%M:%S UTC') if o.refreshed else 'none'
        cost = f'gh calls {o.last_requests}; REST {o.last_rest}; charged {o.last_quota}; GraphQL {o.last_graphql}' if o.live else 'fixture store; no transport'
        self.query_one('#status', Static).update(Text(f"{'Refreshing' if self.refreshing else 'Snapshot'}: {refreshed} · {cost}\n{o.poll} · tail 100ms; selection/tab changes use snapshot"))

    def action_details(self):
        if self.selected and isinstance(self.observer, LocalObserver):
            row = self.selected.copy()
            self.run_worker(self.load_details(row), group='details', exit_on_error=False)

    async def load_details(self, row):
        task = asyncio.create_task(asyncio.to_thread(self.observer.open_details, row))
        await asyncio.sleep(.01)
        self.render_selection()
        await task
        if self.is_running:
            self.render_selection()
            self.footer_state()

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
        if hasattr(self.observer, 'simulate_quota'):
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
    parser.add_argument('--snapshot', type=Path, help='Local snapshot from the experiment harness; no GitHub polling')
    parser.add_argument('--detail-repository', help='Opt in to one explicit missing-detail read (owner/repo)')
    args = parser.parse_args()
    if args.snapshot:
        observer = LocalObserver(args.snapshot)
        if args.detail_repository:
            from ub_agents.github import GitHub
            data = json.loads(args.snapshot.read_text())
            if data['repository'] != args.detail_repository:
                parser.error('--detail-repository must match this launcher projection')
            github = GitHub(args.detail_repository)
            observer.detail_reader = lambda row: github.request(f'{github.prefix}/issues/{row["number"]}')
        View(observer).run()
    elif args.log:
        if args.log.name != 'process.log':
            parser.error('--log must be named process.log, as in a real run directory')
        View(fixture_observer(Path.cwd(), args.log)).run()
    else:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'process.log'
            path.touch()
            View(fixture_observer(Path.cwd(), path), replay=True).run()


if __name__ == '__main__':
    main()
