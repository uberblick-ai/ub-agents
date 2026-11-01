# Optional responsive Textual UI. Imported only by the view process.

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty
import sys
import time
from weakref import WeakKeyDictionary

from rich.control import Control
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Collapsible, Markdown, Static, TabbedContent, TabPane, Tree

from .view_clipboard import copy_with_pbcopy, local_pbcopy
from .view_data import (context_header, context_text, item_header, item_history, mapping,
                        related_assignment, related_plan, text, work_pane)
from .view_github import DescriptionLoads
from .view_log_pane import LogPane, MAX_RENDER_LINES, Reading
from .view_markdown import description_parser
from .view_unblock import (ActionComment, comment_sections, local_action, needs_attention,
                           resume_section, trust_reason, unblock_body)
from .view_runs import run_status as history_status, runs_view
from .view_spinner import SPINNER_FPS
from .view_status import (StatusStyles, empty_log_message, footer, header, key_hints, log_mode,
                          log_notices, pane_line, paused_pill, run_status_line)
from .view_scroll import PaneScroll, ScrollbarVisibility, scroll_action
from .view_widgets import (HealthBanner, ItemHeader, ItemTabs, KeyHelp, RawAccess, SIZE_WARNING,
                           UpdateBanner)
from .view_worker import LocalWorker, Request
from .view_work import RecentActivity, WorkTree, assignment_elapsed
from .view_theme import VIEW_THEME, item_reference, theme_style, variable_defaults
from .poll_now import COOLDOWN_SECONDS
from .log_format import inert
from .view_time import deadline as poll_deadline, pause_status, plan_pauses, remaining_time, runtime_pauses


class View(App):
    TITLE = 'ub-agents launch'
    CSS = '''
    Screen { background: $background; color: $foreground; }
    .view-scroll {
        scrollbar-visibility: hidden;
        scrollbar-size-vertical: 1;
        scrollbar-color: $view-muted;
        scrollbar-color-hover: $view-muted;
        scrollbar-color-active: $view-muted;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
    }
    #update, #health_notice { height: 1; padding: 0 1; background: $view-warning; color: $background; display: none; overflow: hidden; }
    #body { height: 1fr; }
    #size_warning { height: 1fr; content-align: center middle; text-wrap: nowrap; text-overflow: ellipsis; display: none; }
    #shutdown { height: 1fr; content-align: center middle; text-align: center; display: none; }
    #work_pane { width: 36; }
    #work_pane, #panes {
        border: round $view-border; border-title-color: $view-border;
        border-title-align: left; border-title-style: none;
        padding: 1 2 0 2;
    }
    #work_pane:focus-within, #panes:focus-within {
        border: round $view-accent; border-title-color: $view-accent;
    }
    #work { height: 1fr; overflow-x: hidden; }
    #work, #work:focus { background: $background; background-tint: $background 0%; }
    #work > .tree--cursor, #work:focus > .tree--cursor {
        background: $view-selection; color: $view-accent; text-style: none;
    }
    #work > .tree--highlight { background: $view-selection; }
    #recent { height: 1fr; overflow-x: hidden; overflow-y: auto; scrollbar-gutter: stable; }
    #panes { width: 1fr; }
    #panes Tabs { height: 1; }
    #panes Underline { display: none; }
    #panes Tab { padding: 0 1; color: $view-muted; text-style: none; }
    #panes Tab.-active, #panes Tabs:focus Tab.-active {
        color: $background; background: $foreground; text-style: none;
    }
    #log_mode { overlay: screen; position: absolute; offset: 24 0; width: auto; height: 1; }
    #tab_rule { height: 1; color: $view-muted; }
    #panes > ContentSwitcher { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    #item_header {
        height: 3; padding: 0; overflow: hidden;
        link-style: underline;
        link-color-hover: $view-link;
        link-background-hover: transparent;
        link-style-hover: underline;
    }
    #log_note { height: 1; overflow: hidden; }
    #output { height: 1fr; scrollbar-gutter: stable; overflow-x: hidden; }
    #run_status { height: 2; overflow: hidden; }
    #log_state { height: 1; content-align: right middle; }
    #issue_body, #issue_pointers_body, #issue_actions, #unblock_body { padding: 0; }
    MarkdownFence { color: $foreground; background: $panel; }
    MarkdownBlockQuote { border: none; background: $panel; }
    MarkdownHorizontalRule { border-bottom: dashed $view-muted; }
    #unblock_note { color: $view-muted; text-style: dim; }
    #status { height: 1; background: $panel; }
    '''
    BINDINGS = [
        Binding('q', 'quit', 'Stop after run', priority=True),
        Binding('ctrl+c', 'stop_now', 'Stop now', priority=True),
        Binding('y', 'copy_selection', 'Copy selection', priority=True),
        Binding('enter', 'open_item', 'Open item', priority=True),
        Binding('escape', 'back', 'Back', priority=True),
        Binding('f', 'follow', 'Follow/pause', priority=True),
        Binding('u', 'raw', 'Raw', priority=True),
        Binding('h', 'history', 'Older page', priority=True),
        Binding('p', 'path', 'Full raw path', priority=True),
        Binding('r', 'poll_now', 'Poll now', priority=True),
        Binding('c', 'keep_awake', 'Keep awake (macOS)', priority=True),
        Binding('question_mark', 'help', 'Keys', priority=True),
        Binding('g', 'load_description', 'Reload/load/retry', priority=True),
        Binding('1', "tab('log')", 'Agent', priority=True),
        Binding('2', "tab('issue')", 'Issue', priority=True),
        Binding('3', "tab('runs')", 'Runs', priority=True),
        Binding('4', "tab('unblock')", 'Unblock', priority=True),
        Binding('pageup', 'page_up', 'Page up', priority=True),
        Binding('pagedown', 'page_down', 'Page down', priority=True),
        Binding('home', 'home', 'Top', priority=True),
        Binding('end', 'end', 'Bottom', priority=True),
    ]

    def __init__(self, root, session_path, worker=None, descriptions=None, launcher=None):
        super().__init__()
        self.root = Path(root)
        self.register_theme(VIEW_THEME)
        self.theme = VIEW_THEME.name
        self.launcher = launcher
        self.awake_enabled = False
        self.awake_supported = sys.platform == 'darwin'
        self.awake_notice = ''
        self.awake_notice_until = 0
        self.shutdown = None
        self.worker = worker or LocalWorker(root, session_path)
        self.descriptions = descriptions or DescriptionLoads()
        self.local_description = None
        self.issue_load_key = None
        self.issue_pointers_key = None
        self.unblock_visible = False
        self.unblock_details_key = None
        self.session = None
        self.pane = None
        self.rows = {}
        self.selected = None
        self.chosen = False  # a person picked a row; the view stops following the run
        self.readings = {}
        self.token = 0
        self.busy = False
        self.pending_history = None
        self.pending_reload = False
        self.last_context = self.last_runs = None
        self._static_values = WeakKeyDictionary()
        self._tree_second = None
        self._window_title = None
        self.narrow = False
        self.too_small = False
        self.item_view = False
        self.layout_focus = None
        self.copy_notice = ''
        self.copy_notice_until = 0
        self.poll_feedback = {}
        self.poll_next_allowed = None

    def compose(self) -> ComposeResult:
        yield Static('', id='shutdown', markup=False)
        yield Static(Text(SIZE_WARNING, no_wrap=True, overflow='ellipsis'), id='size_warning')
        yield UpdateBanner()
        yield HealthBanner()
        with Horizontal(id='body'):
            with Vertical(id='work_pane'):
                yield WorkTree('Work', id='work')
                yield RecentActivity()
            with ItemTabs(id='panes'):
                with TabPane('1 Agent', id='log'):
                    yield Static('', id='log_note', markup=False)
                    yield LogPane(id='output')
                    yield Static('', id='log_state', markup=False)
                    yield Static('', id='run_status', markup=False)
                with TabPane('2 Issue', id='issue'):
                    with PaneScroll():
                        yield Static('Context unavailable.', id='issue_text', markup=False)
                        yield Markdown('', id='issue_body', parser_factory=description_parser, open_links=False)
                        with Collapsible(title='Pointers', collapsed=True, id='issue_pointers'):
                            yield Markdown('', id='issue_pointers_body', parser_factory=description_parser, open_links=False)
                            yield Static('', id='issue_pointers_note', markup=False)
                        yield Markdown('', id='issue_actions', parser_factory=description_parser, open_links=False)
                        yield Static('', id='issue_note', markup=False)
                with TabPane('3 Runs', id='runs'):
                    with PaneScroll():
                        yield Static('Select an item to see its history.', id='runs_text', markup=False)
                with TabPane('4 Unblock', id='unblock'):
                    with PaneScroll():
                        yield Markdown('', id='unblock_body', parser_factory=description_parser, open_links=False)
                        with Collapsible(title='Reasoning and evidence',
                                         collapsed=True, id='unblock_details'):
                            yield Markdown('', id='unblock_details_body', parser_factory=description_parser, open_links=False)
                        with Collapsible(title='To send it back instead',
                                         collapsed=True, id='unblock_resume'):
                            yield Markdown('', id='unblock_resume_body', parser_factory=description_parser, open_links=False)
                        yield Static('', id='unblock_note', markup=False)
        yield Static('', id='status', markup=False)

    def on_mount(self):
        self.query_one(ItemTabs).hide_tab('unblock')
        self.update_layout(self.size)
        self.query_one('#work_pane').border_title = 'Work'
        self.query_one(ItemTabs).border_title = 'Agent'
        self.query_one(WorkTree).show_root = False
        self.theme_changed_signal.subscribe(self, self.restyle)
        self.worker.start()
        self.set_interval(1 / SPINNER_FPS, self.tick)
        self.tick()
        if self.launcher is not None:
            self.launcher.mounted(self)

    def on_resize(self, event):
        self.update_layout(event.size)

    def update_layout(self, size):  # noqa: C901
        pane = self.query_one_optional('#work_pane')
        if pane is None:
            return
        if self.shutdown is not None:
            for selector in ('#body', '#status', '#size_warning', '#update', '#health_notice'):
                self.query_one(selector).display = False
            self.query_one('#shutdown').display = True
            self.update_shutdown()
            return
        narrow = size.width < 110 or size.height < 32
        too_small = size.width < 60 or size.height < 16
        output = self.query_one(LogPane)
        if narrow != self.narrow or too_small != self.too_small:
            if (not self.too_small and self.query_one(ItemTabs).display
                    and self.query_one(ItemTabs).active == 'log'):
                output.save_anchor()
        if narrow and not self.narrow:
            self.item_view = self.query_one(ItemTabs).has_focus_within
        if too_small and not self.too_small:
            self.layout_focus = self.focused
        restore_focus = self.too_small and not too_small
        changed = narrow != self.narrow
        self.narrow, self.too_small = narrow, too_small
        pane.styles.width = '1fr' if narrow else min(64, max(46, size.width // 3))
        pane.styles.margin = (0, 0 if narrow else 1, 0, 0)
        pane.display = not narrow or not self.item_view
        self.query_one(ItemTabs).display = not narrow or self.item_view
        self.query_one('#body').display = not too_small
        self.query_one('#status').display = not too_small
        self.query_one('#size_warning').display = too_small
        banner = self.query_one(UpdateBanner)
        banner.display = not too_small and bool(text(banner.banner.get('text'), ''))
        health = self.query_one(HealthBanner)
        health.display = not too_small and bool(text(health.banner.get('text'), ''))
        if changed:
            tree = self.query_one(WorkTree)
            tree._invalidate()
            self.query_one(RecentActivity).refresh(layout=True)
        for screen in self.screen_stack:
            if isinstance(screen, RawAccess) and screen.is_mounted:
                screen.set_floor(too_small)
        if restore_focus and self.layout_focus is not None:
            self.layout_focus.focus(scroll_visible=False)
        self.update_status()

    def check_action(self, action, parameters):
        if self.shutdown is not None:
            return action in {'quit', 'stop_now'}
        item_actions = {'follow', 'raw', 'history', 'path', 'load_description',
                        'tab', 'page_up', 'page_down', 'home', 'end'}
        if self.too_small and action in item_actions | {'help', 'open_item', 'back', 'focus_next', 'focus_previous'}:
            return False
        if self.narrow and not self.item_view and action in item_actions:
            return False
        # Leave wide Enter handling and modal Escape handling to their widgets.
        if action == 'open_item':
            return self.narrow and not self.item_view and not isinstance(self.screen, RawAccess)
        if action == 'back':
            return self.narrow and self.item_view and not isinstance(self.screen, RawAccess)
        return True

    def action_open_item(self):
        recent = self.query_one(RecentActivity)
        node = self.query_one(WorkTree).cursor_node
        key = recent.cursor if recent.has_focus else node.data if node else None
        if key not in self.rows:
            return
        self.select(key)
        self.item_view = True
        self.update_layout(self.size)
        tab = self.query_one(ItemTabs).active
        target = self.query_one(LogPane) if tab == 'log' else self.query_one('#' + tab + ' VerticalScroll')
        target.focus(scroll_visible=False)
        if tab == 'log':
            output = self.query_one(LogPane)
            self.call_after_refresh(output.reflow)

    def action_back(self):
        if self.query_one(ItemTabs).active == 'log':
            self.query_one(LogPane).save_anchor()
        self.item_view = False
        self.update_layout(self.size)
        tree, recent = self.query_one(WorkTree), self.query_one(RecentActivity)
        if self.selected in tree.nodes:
            tree.move_cursor(tree.nodes[self.selected])
            tree.focus(scroll_visible=False)
        elif self.selected in {row.key for row in recent.rows}:
            recent.cursor = self.selected
            recent.focus(scroll_visible=False)
        else:
            tree.focus(scroll_visible=False)

    def action_quit(self):
        if self.launcher is None:
            self.exit()
        elif self.shutdown is None:
            self.begin_shutdown('draining')
            # Draw even the idle screen before the launcher can close the view.
            self.call_after_refresh(self.launcher.drain)

    def action_stop_now(self):
        if self.launcher is None:
            self.exit()
        elif self.shutdown != 'stopping':
            self.begin_shutdown('stopping')
            self.call_after_refresh(self.launcher.interrupt)

    def on_text_selected(self):
        self.action_copy_selection()

    def action_copy_selection(self):
        value = self.screen.get_selected_text()
        if not value:
            return
        self.copy_to_clipboard(value)
        command = local_pbcopy()
        if command is not None:
            self.run_worker(copy_with_pbcopy(command, value), group='clipboard',
                            exclusive=True, exit_on_error=False)
        self.copy_notice = f'copied {len(value)} characters'
        self.copy_notice_until = time.monotonic() + 2
        self.update_status()

    def begin_shutdown(self, mode):
        self.shutdown = mode
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.set_focus(None)
        self.update_layout(self.size)
        # Mark the screen itself dirty before scheduling the stop callback;
        # widget layout notifications may still be waiting in other queues.
        self.refresh(layout=True)

    def update_shutdown(self):
        assignment = mapping(self.session.data.get('assignment')) if self.session else {}
        key = 'assignment:' + text(assignment.get('run'), 'claiming')
        row = self.rows.get(key) if assignment else None
        middle = 'No run in progress.'
        if row is not None:
            history = mapping(row.data.get('history'))
            reference = item_reference(row.item, row.data.get('kind') or history.get('kind'), app=self).plain
            if self.shutdown == 'draining':
                elapsed = assignment_elapsed(row, claimed_at=self.query_one(WorkTree).claim_times.get(key))
                middle = f'Waiting for {reference} ({row.agent}, {elapsed}) to finish.'
            else:
                middle = f'Terminating {reference} ({row.agent}) and releasing its claim…'
        title = 'Shutting down the launcher' if self.shutdown == 'draining' else 'Stopping the launcher'
        message = title + '\n\n' + middle
        if self.shutdown == 'draining':
            message += '\nNo new work will be claimed. Press Ctrl-C to stop now.'
        self.query_one('#shutdown', Static).update(Text(message, justify='center'))

    def action_keep_awake(self):
        if self.launcher is not None and self.shutdown is None:
            self.launcher.awake()

    def keep_awake_state(self, state):
        self.awake_supported = state.get('supported') is True
        self.awake_enabled = state.get('enabled') is True
        self.awake_notice = text(state.get('notice'), '')
        self.awake_notice_until = time.monotonic() + 2 if self.awake_notice else 0
        self.update_status()

    def action_poll_now(self):
        if self.launcher is not None:
            activity = mapping(self.session.data.get('activity')) if self.session else {}
            if (activity.get('state') in {'running assignment', 'waiting'} and
                    activity.get('reason') in {None, 'next poll or runtime pause'}):
                control = mapping(self.session.data.get('poll_now')) | self.poll_feedback
                now = datetime.now(timezone.utc)
                cooldown = poll_deadline(control.get('cooldown_until'), now)
                if self.poll_next_allowed is not None and self.poll_next_allowed > now:
                    cooldown = max(cooldown or now, self.poll_next_allowed)
                if poll_deadline(control.get('rate_limit_until'), now) is None:
                    feedback = {}
                    if control.get('refreshing') is True:
                        feedback = {'refreshing': True, 'cooldown_until': None}
                    elif cooldown is not None:
                        feedback = {'refreshing': False, 'cooldown_until': cooldown.isoformat()}
                    elif control.get('waiting') is True or activity['state'] == 'waiting':
                        self.poll_next_allowed = now + timedelta(seconds=COOLDOWN_SECONDS)
                        feedback = {'refreshing': True, 'cooldown_until': None}
                    if activity['state'] == 'running assignment':
                        self.poll_feedback = feedback
            self.launcher.poll()
            self.update_status()

    def on_unmount(self):
        if self._window_title is not None and self._driver is not None:
            self._driver.write(str(Control.title('')))
            self._window_title = None
        self.descriptions.close()
        self.worker.close()
        if self.launcher is not None:
            self.launcher.close()

    @property
    def reading(self):
        return self.readings.setdefault(self.selected, Reading())

    def tick(self):  # noqa: C901, PLR0912
        # Textual clears is_running before removing screen widgets, while
        # refresh timers may still fire until teardown closes the message pump.
        if not self.is_running:
            return
        for screen in self.screen_stack:
            for area in screen.query('.view-scroll'):
                area.update_scrollbar()
        self.descriptions.poll()
        try:
            result = self.worker.results.get_nowait()
        except Empty:
            result = None
        if result:
            self.busy = False
            if self.session is None or result.session.data != self.session.data:
                self.poll_feedback = {}
            self.session = result.session
            self.title = 'ub-agents launch — ' + text(self.session.data.get('repository'), 'unknown')
            if self.session.data.get('queue_agent'):
                self.title += ' · agent ' + text(self.session.data['queue_agent'])
            # App.title only updates Header widgets in the pinned Textual.
            if self._driver is not None and not self.is_headless and self.title != self._window_title:
                self._driver.write(str(Control.title(self.title)))
                self._window_title = self.title
            self.query_one(UpdateBanner).set_banner(self.session.data.get('update'))
            self.query_one(HealthBanner).set_banner({'text': self.session.data.get('health_notice')})
            # A person may pick a row while this read is in flight. Retain that
            # selection from the pane we last drew, using the returned snapshot.
            pane = (result.pane if result.token == self.token and result.chosen == self.chosen else
                    work_pane(self.session, self.root, self.pane, self.selected, self.chosen))
            self.populate(pane)
            if result.token == self.token and result.key == self.selected:
                self.apply(result)
        if not self.busy:
            end, generation = self.pending_history or (None, 0)
            if self.worker.request(Request(self.selected, self.token, end, generation, self.chosen,
                                           self.pane, reload=self.pending_reload)):
                self.busy = True
                self.pending_history = None
                self.pending_reload = False
        self.update_issue()
        self.update_unblock()
        self.update_runs()
        self.update_status()
        tree = self.query_one(WorkTree)
        second = int(time.monotonic())
        if second != self._tree_second:
            tree.refresh_idle(self.pane)
            tree.refresh()
            self._tree_second = second
        else:
            tree.refresh_spinners()

    def idle_label(self, pane, width=None):  # noqa: C901
        complete = mapping(self.session.data.get('latest_pass')).get('state') == 'complete'
        reason = 'nothing eligible for this launcher' if complete and pane.next is None else 'polling'
        if self.session.data.get('queue_agent'):
            reason = (text(self.session.data.get('queue_idle'), reason) if complete and pane.next is None
                      else 'polling for agent ' + text(self.session.data['queue_agent']))
        now = datetime.now(timezone.utc)
        pauses = runtime_pauses(self.session.data, now)
        held = {}
        for row in pane.rows:
            if row.group == 'Eligible' and row.state != 'earlier observation':
                for plan in row.eligible_plans or (row.data,):
                    held.update(plan_pauses(plan, pauses))
        if held:
            reason = ', '.join(pause_status({cli: end}, now) for cli, end in pauses.items() if cli in held)
            reason += ' (usage limit)'
        label = Text(f'    Idle · {reason}', style='dim')
        if held and width is not None:
            for phrase in (' paused ', ' '):
                if label.cell_len <= width:
                    break
                reason = ', '.join(f'{cli}{phrase}{remaining_time(end, now)}'
                                   for cli, end in pauses.items() if cli in held)
                label = Text(f'    Idle · {reason} (usage limit)', style='dim')
            if label.cell_len > width:
                label = Text(f'Idle · {reason} (usage limit)', style='dim')
            if label.cell_len > width:
                label = Text(f'Idle · {reason}', style='dim')
        return label

    def migrate_reading(self, pane):
        if pane.selected != self.selected:
            replacement = (related_assignment(pane.rows, self.rows.get(self.selected)) or
                           related_plan(pane.rows, self.rows.get(self.selected)))
            if replacement is not None and replacement.key == pane.selected:
                if self.selected in self.readings:
                    self.readings[replacement.key] = self.readings.pop(self.selected)
                self.selected = replacement.key

    def populate(self, pane):
        tree = self.query_one(WorkTree)
        recent = self.query_one(RecentActivity)
        recent.populate(pane.recent)
        cursor = tree.cursor_node
        cursor_row = self.rows.get(cursor.data) if cursor else None
        self.migrate_reading(pane)
        self.rows = {row.key: row for row in pane.rows}
        nodes = tree.populate(pane)
        self.pane = pane
        work = self.query_one('#work_pane')
        title = Text(pane.title)
        if work.border_title != title.markup:
            work.border_title = title
        if pane.selected != self.selected:
            self.select(pane.selected, chosen=False)
            if self.selected in nodes:
                tree.move_cursor(nodes[self.selected])
            else:
                recent.cursor = self.selected
                recent.focus()
        elif cursor:
            target = nodes.get(cursor.data)
            if target is None:
                replacement = (related_assignment(pane.rows, cursor_row) or
                               related_plan(pane.rows, cursor_row))
                target = nodes.get(replacement.key) if replacement else None
            target = target or nodes.get(self.selected)
            if target is not None:
                # Even a reused node can now occupy a different cursor line.
                tree.get_node_at_line(0)
                if target is not tree.cursor_node:
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
        self.pending_reload = False
        self.query_one('#output', LogPane).set_reading(self.reading)
        self.last_context = None
        self.last_runs = None
        self.local_description = None
        self.issue_load_key = None
        row = self.rows.get(self.selected)
        identity = (self.description_key(), row.item if row else None)
        fold = self.query_one('#issue_pointers', Collapsible)
        if self.issue_pointers_key is None or self.issue_pointers_key[:2] != identity:
            self.issue_pointers_key = None
            fold.collapsed = True
        fold.display = False
        self.query_one('#issue_text', Static).update('Reading cached context…')
        self.update_runs()
        self.query_one('#issue_body', Markdown).update('')
        self.query_one('#issue_note', Static).update('')
        self.update_unblock()
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

    def update_static(self, widget, value, *, layout=True):
        """Keep unchanged content from requesting another repaint or layout."""
        if isinstance(value, Text):
            signature = (value.copy(), value.style, value.justify, value.overflow, value.no_wrap)
        elif isinstance(value, str):
            signature = value
        else:
            # Rich Groups and Tables compare by identity. Compare their rendered
            # cells, including styles, so a clock tick with unchanged text is inert.
            options = self.console.options.update(width=max(1, widget.content_size.width))
            signature = tuple(self.console.render(value, options))
        if signature != self._static_values.get(widget):
            widget.update(value, layout=layout)
            self._static_values[widget] = signature

    def update_runs(self):
        if self.session is None:
            return
        row = self.rows.get(self.selected)
        history = item_history(row, self.session, full=True)
        now = datetime.now().astimezone()
        active = any(history_status(run, now)[0] == 'running' for run in history.get('runs', []))
        visible = self.query_one(ItemTabs).active == 'runs'
        signature = (row.key if row else None, repr(history),
                     int(now.timestamp() * (SPINNER_FPS if active and visible else 1)))
        if signature != self.last_runs:
            self.update_static(self.query_one('#runs_text', Static),
                               runs_view(row, self.session, now=now, app=self, history=history))
            self.last_runs = signature

    def description_key(self):
        row = self.rows.get(self.selected)
        return self.descriptions.key(self.session.data.get('repository'), row.item) if self.session and row else None

    def item_url(self):
        key = self.description_key()
        if key is None:
            return None
        repository, number = key
        title, _ = item_header(self.rows.get(self.selected), self.current_description(), self.session)
        path = 'pull' if title.startswith('⌥') else 'issues'
        return f'https://github.com/{repository}/{path}/{number}'

    def action_open_reference(self):
        # Resolve at click time so a refreshed selection cannot open a stale URL.
        url = self.item_url()
        if url is not None:
            self.open_url(url)

    def update_issue(self):  # noqa: C901, PLR0912, PLR0915
        if self.local_description is None:
            return
        key = self.description_key()
        local = self.local_description
        if self.query_one(ItemTabs).active == 'issue' and self.issue_load_key != key:
            # One attempt per activation/item, including when a cooldown or
            # another pending request prevents it. Timers do not queue reads.
            self.issue_load_key = key
            if not local.available and self.descriptions.get(key) is None:
                self.descriptions.request(key)
        description = local if local.available else (self.descriptions.get(key) or local)
        row = self.rows.get(self.selected)
        pointers = description.pointers if row and description.available and not description.error else ''
        fold = self.query_one('#issue_pointers', Collapsible)
        fold.title = 'Pointers (snapshot may be incomplete)' if description.incomplete else 'Pointers'
        fold.display = bool(pointers)
        markdown = self.query_one('#issue_pointers_body', Markdown)
        if pointers != markdown.source:
            markdown.update(pointers)
        self.update_static(self.query_one('#issue_pointers_note', Static), Text(description.pointers_notice))
        pointers_key = (key, row.item if row else None, description.title,
                        description.body_digest, description.incomplete)
        if pointers_key != self.issue_pointers_key:
            fold.collapsed = True
            self.issue_pointers_key = pointers_key
        details = Text()
        muted = theme_style(self, 'view-muted', dim=True)
        for index, (line, secondary) in enumerate(description.detail_lines()):
            if index:
                details.append('\n')
            details.append(line, style=muted if secondary else None)
        extra = ''
        if self.descriptions.pending == key and key is not None and self.descriptions.pending_kind == 'issue':
            extra += f'\n\nLoading #{row.item}…'
        elif not description.available:
            extra += '\n\nPress g on Issue to ' + ('retry' if description.error else 'load') + ' title/body from GitHub.'
        if self.descriptions.clock() < self.descriptions.cooldown:
            reset = datetime.fromtimestamp(self.descriptions.cooldown, timezone.utc).isoformat()
            extra += f'\nGitHub cooldown until {reset}; no loads or retries before then.'
        elif self.descriptions.pending is not None and (self.descriptions.pending != key or self.descriptions.pending_kind != 'issue'):
            extra += '\nAnother GitHub read is pending; no requests are queued.'
        value = context_text(row, description) + extra
        if value != self.last_context:
            self.query_one('#issue_text', Static).update(Text(context_header(row, description)))
            body = description.body if row and description.available and not description.error else ''
            markdown = self.query_one('#issue_body', Markdown)
            if body != markdown.source:
                markdown.update(body)
            self.query_one('#issue_note', Static).update(details.append(extra))
            self.last_context = value
        actions = unblock_body(row, self.current_action()) if needs_attention(row) else ''
        lead, supporting = comment_sections(actions)
        lead, resume_title, resume = resume_section(lead)
        actions = '\n\n'.join(part for part in (lead, resume_title, resume, supporting) if part)
        markdown = self.query_one('#issue_actions', Markdown)
        markdown.display = bool(actions)
        if actions != markdown.source:
            markdown.update(actions)

    def current_description(self):
        local = self.local_description
        return local if local and local.available else (self.descriptions.get(self.description_key()) or local)

    def current_action(self):
        row = self.rows.get(self.selected)
        local = local_action(row, self.session)
        if local.available:
            return local
        cached = self.descriptions.get(self.description_key(), 'unblock')
        if cached and cached.available:
            reason = trust_reason(cached.author, mapping(self.session.data.get('coordination_authors')))
            if reason:
                # A changed verification cannot keep a previously trusted body
                # visible, or prevent an explicit retry after trust is restored.
                cached = replace(cached, body='', available=False, error=reason)
                self.descriptions.cache[self.description_key()]['unblock'] = cached
        return replace(cached, omitted=local.omitted) if cached else local

    def load_missing_action(self):
        """Opening Unblock may load an unseen notice, but never retry a cached result."""
        if not self.unblock_visible:
            return
        local = local_action(self.rows.get(self.selected), self.session)
        key = self.description_key()
        if local.available or local.error or self.descriptions.get(key, 'unblock') is not None:
            return
        self.descriptions.request(key, 'unblock', mapping(self.session.data.get('coordination_authors')))
        self.update_unblock()

    def update_unblock(self):  # noqa: C901, PLR0915
        visible = needs_attention(self.rows.get(self.selected))
        tabs = self.query_one(ItemTabs)
        if visible != self.unblock_visible:
            if visible:
                tabs.show_tab('unblock')
            else:
                if tabs.active == 'unblock':
                    tabs.active = 'log'
                tabs.hide_tab('unblock')
            self.unblock_visible = visible
        comment = self.current_action()
        body = unblock_body(self.rows.get(self.selected), comment) if visible else ''
        lead, supporting = comment_sections(body)
        lead, resume_title, resume = resume_section(lead)
        markdown = self.query_one('#unblock_body', Markdown)
        if lead != markdown.source:
            markdown.update(lead)
        resume_fold = self.query_one('#unblock_resume', Collapsible)
        resume_fold.title = resume_title
        resume_fold.display = bool(resume)
        resume_markdown = self.query_one('#unblock_resume_body', Markdown)
        if resume != resume_markdown.source:
            resume_markdown.update(resume)
        fold = self.query_one('#unblock_details', Collapsible)
        fold.title = ('Reasoning, evidence and resume instructions'
                      if '<summary>Reasoning, evidence and resume instructions</summary>' in body
                      else 'Reasoning and evidence')
        fold.display = bool(supporting)
        details_markdown = self.query_one('#unblock_details_body', Markdown)
        if supporting != details_markdown.source:
            details_markdown.update(supporting)
        details_key = (self.description_key(), comment.comment_id, comment.created_at, comment.author, body)
        if details_key != self.unblock_details_key:
            resume_fold.collapsed = True
            fold.collapsed = True
            self.unblock_details_key = details_key
        extra = ''
        key = self.description_key()
        pending = self.descriptions.pending == key and key is not None and self.descriptions.pending_kind == 'unblock'
        if pending and not comment.omitted:
            extra += 'Loading action-needed comments from GitHub…\n'
        elif self.descriptions.pending is not None and not pending:
            extra += 'Another GitHub read is pending; no requests are queued.\n'
        if self.descriptions.clock() < self.descriptions.cooldown:
            reset = datetime.fromtimestamp(self.descriptions.cooldown, timezone.utc).isoformat()
            extra += f'GitHub cooldown until {reset}; no loads or retries before then.\n'
        details = Text(extra + comment.details(self.descriptions.clock(), pending=pending))
        if details:
            details.append('\n')
        details.append('Or decide in Claude Code or Codex with the ')
        details.append('ub-agents-unblock', style=Style(link='https://skills.uberblick.ai/skills/ub-agents-unblock'))
        details.append(' skill')
        self.update_static(self.query_one('#unblock_note', Static), details)

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
        if not isinstance(self.screen, RawAccess) and self.query_one(TabbedContent).active == 'log':
            # Read the latest snapshot and replace the cached reader on its
            # local worker. In-flight pages must not override this reload.
            self.token += 1
            self.pending_history = None
            self.pending_reload = True
            self.reading.follow = True
            self.reading.anchor = None
            self.reading.notice = ''
            self.query_one(LogPane).set_reading(self.reading)
            self.update_status()
            return
        if not isinstance(self.screen, RawAccess) and self.query_one(TabbedContent).active == 'unblock':
            if self.unblock_visible and not self.current_action().available:
                self.descriptions.request(self.description_key(), 'unblock',
                                          mapping(self.session.data.get('coordination_authors')))
                self.update_unblock()
            return
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
        return footer(self.session, width, keys, now=datetime.now(timezone.utc), monotonic_now=time.monotonic(),
                      narrow=self.narrow, launcher_attached=self.launcher is not None,
                      awake_enabled=self.awake_enabled, awake_supported=self.awake_supported,
                      poll_feedback=self.poll_feedback, copy_notice=self.copy_notice,
                      copy_notice_until=self.copy_notice_until, awake_notice=self.awake_notice,
                      awake_notice_until=self.awake_notice_until)

    def update_status(self):
        if not self.is_mounted:
            return
        if self.shutdown is not None:
            self.update_shutdown()
            return
        reading = self.reading
        styles = StatusStyles(theme_style(self, 'view-muted'), theme_style(self, 'view-accent'),
                              theme_style(self, 'view-error'), theme_style(self, 'view-warning'))
        indicator = self.query_one('#log_mode', Static)
        tab_width = sum(tab.region.width for tab in self.query('#panes Tab') if tab.display)
        mode_width = self.query_one(ItemTabs).content_size.width - tab_width
        mode = log_mode(reading, mode_width, styles)
        indicator.styles.offset = (tab_width, 0)
        indicator.styles.width = mode.cell_len
        self.update_static(indicator, mode, layout=False)
        rule = self.query_one('#tab_rule', Static)
        self.update_static(rule, '┄' * rule.content_size.width, layout=False)
        unread, lag = self.log_lag()
        keys = key_hints(reading, narrow=self.narrow, item_view=self.item_view,
                         unblock_visible=self.unblock_visible, active_tab=self.query_one(ItemTabs).active)
        status = self.footer(self.size.width, keys)
        self.update_static(self.query_one('#status', Static), status, layout=False)
        if isinstance(self.screen, RawAccess):
            for raw_footer in self.screen.query('#raw_status').results(Static):
                # Before its first layout the footer has no width; use the width compose used.
                width = raw_footer.size.width or self.size.width - self.screen.styles.padding.width
                self.update_static(raw_footer, self.footer(width, self.screen.footer_keys), layout=False)
        pill = self.query_one('#log_state', Static)
        pill.display, pill_text = paused_pill(reading, unread, lag)
        self.update_static(pill, pill_text, layout=False)
        output = self.query_one('#output', LogPane)
        row = self.rows.get(self.selected)
        item = self.query_one('#item_header', Static)
        description = self.current_description()
        unblock = self.query_one(ItemTabs).active == 'unblock'
        action, now = None, None
        if unblock:
            action, now = self.current_action(), self.descriptions.clock()
        header_text = header(row, description, self.session, item.content_region.width, styles,
                             clickable=self.item_url() is not None, unblock=unblock, action=action, now=now)
        self.update_static(item, header_text, layout=False)
        width = output.size.width
        note = self.query_one('#log_note', Static)
        note.display, note_text = log_notices(row, reading, width, styles)
        self.update_static(note, note_text, layout=False)
        empty_message = empty_log_message(row)
        if reading.empty_message != empty_message:
            reading.empty_message = empty_message
            output.refresh()
        run_note = run_status_line(row, self.session, width, styles, time.monotonic())
        self.update_static(self.query_one('#run_status', Static), run_note, layout=False)
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
        if isinstance(self.screen, RawAccess) or tab == 'unblock' and not self.unblock_visible:
            return
        if self.query_one(TabbedContent).active == 'log':
            self.query_one('#output', LogPane).save_anchor()
        self.query_one(TabbedContent).active = tab
        if tab == 'issue':
            self.issue_load_key = None
            self.update_issue()
        if tab == 'unblock':
            self.load_missing_action()

    def on_click(self, event):
        # Clicking an already active tab does not emit TabActivated, but is
        # still an explicit activation after selecting a different item.
        if event.widget is self.query_one(ItemTabs).get_tab('issue'):
            self.issue_load_key = None
            self.update_issue()
        if self.unblock_visible and event.widget is self.query_one(ItemTabs).get_tab('unblock'):
            self.load_missing_action()

    def on_tabbed_content_tab_activated(self, event):
        self.query_one(ItemTabs).border_title = Text(
            {'log': 'Agent', 'issue': 'Issue', 'runs': 'Runs', 'unblock': 'Unblock'}.get(event.pane.id, event.tab.label.plain))
        if event.pane.id == 'log' and self.is_mounted:
            self.call_after_refresh(self.query_one('#output', LogPane).reflow)
        if self.is_mounted:
            if event.pane.id == 'issue':
                self.issue_load_key = None
                self.update_issue()
            if event.pane.id == 'unblock':
                self.load_missing_action()
            if event.pane.id == 'runs':
                self.last_runs = None
                # Before its first layout a hidden table has no width, so
                # rendered-cell comparisons can hide changed history.
                self._static_values.pop(self.query_one('#runs_text', Static), None)
                self.update_runs()
            self.update_status()

    def get_theme_variable_defaults(self):
        return variable_defaults(self.current_theme)

    def restyle(self, theme):
        # Rich strips and tables retain resolved colors; rebuild them when CSS changes.
        output = self.query_one(LogPane)
        output.save_anchor()
        output.reflow()
        self.last_runs = None
        self.update_runs()
        self.last_context = None
        self.update_issue()
        self.update_status()
        self.query_one(WorkTree).refresh()
        self.query_one(RecentActivity).refresh()

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
            self.push_screen(KeyHelp(self.unblock_visible, attached=self.launcher is not None,
                                     awake_supported=self.awake_supported))

    def scroll_area(self):
        if isinstance(self.screen, RawAccess):
            return self.screen.query_one(PaneScroll)
        tab = self.query_one(TabbedContent).active
        return self.query_one(LogPane) if tab == 'log' else self.query_one('#' + tab + ' VerticalScroll')

    async def _dispatch_action(self, namespace, action_name, params):
        # Textual dispatches bindings at App level, rather than as widget key
        # events. Scope only scroll/cursor actions, including inherited ones.
        area = namespace if isinstance(namespace, ScrollbarVisibility) else (
            self.scroll_area() if namespace is self and scroll_action(action_name) else None)
        if area is not None and scroll_action(action_name):
            with area.user_scroll():
                return await super()._dispatch_action(namespace, action_name, params)
        return await super()._dispatch_action(namespace, action_name, params)

    def scroll_key(self, action):
        area = self.scroll_area()
        if isinstance(area, LogPane) and action in {'page_up', 'scroll_home'} and self.reading.follow:
            self.action_follow()
        method = 'scroll_' + action if action.startswith('page_') else action
        getattr(area, method)(animate=False)
        if isinstance(area, LogPane):
            self.call_after_refresh(area.save_anchor)

    def action_page_up(self):
        self.scroll_key('page_up')

    def action_page_down(self):
        self.scroll_key('page_down')

    def action_home(self):
        self.scroll_key('scroll_home')

    def action_end(self):
        self.scroll_key('scroll_end')
