"""Actual owned PTY acceptance, separate from Textual headless pilots."""

from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.terminal import Terminal
from tests.test_view_data import event, fixture, publish_snapshot

class TerminalViewTests(unittest.TestCase):
    def test_mouse_copy_and_y_in_real_terminal_restore_after_ctrl_c(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, _, _ = fixture(root)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from ub_agents.view_ui import RawAccess, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    copies = 0
    def copy_to_clipboard(self, value):
        self.copies += 1
        super().copy_to_clipboard(value)
    def proof_values(self):
        overlay = isinstance(self.screen, RawAccess)
        widget = self.screen.query_one('#raw_details' if overlay else '#issue_text')
        footer = self.screen.query_one('#raw_status' if overlay else '#status')
        return {'ready': self.last_context is not None, 'screen': type(self.screen).__name__,
                'tab': self.query_one('#panes').active, 'copies': self.copies,
                'clipboard': self.clipboard, 'selection': self.screen.get_selected_text(),
                'content': widget.render().plain, 'region': list(widget.region),
                'footer': footer.render().plain}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof) as terminal:
                terminal.checkpoint(lambda value: value['ready'])
                copies = 0
                for key, screen in ((b'2', 'Screen'), (b'p', 'RawAccess'), (b'?', 'KeyHelp')):
                    terminal.send(key)
                    surface = terminal.checkpoint(lambda value: value['screen'] == screen
                                                  and (screen != 'Screen' or value['tab'] == 'issue'))
                    x, y = surface['region'][:2]
                    expected = surface['content'].splitlines()[0][:4]
                    sequence = b'\x1b]52;c;' + base64.b64encode(expected.encode('utf-8')) + b'\a'
                    terminal.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<32;{x + 4};{y + 1}M'.encode('ascii'))
                    selected = terminal.checkpoint(lambda value: value['selection'] == expected)
                    self.assertEqual(selected['copies'], copies)
                    terminal.send(f'\x1b[<0;{x + 4};{y + 1}m'.encode('ascii'))
                    copies += 1
                    released = terminal.checkpoint(lambda value: value['copies'] == copies)
                    self.assertEqual(released['clipboard'], expected)
                    self.assertIn('copied 4 characters', released['footer'])
                    terminal.expect(sequence)
                    terminal.send(b'y')
                    copies += 1
                    repeated = terminal.checkpoint(lambda value: value['copies'] == copies)
                    self.assertEqual(repeated['clipboard'], expected)
                    terminal.expect(sequence)
                    terminal.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m'.encode('ascii'))
                    cleared = terminal.checkpoint(lambda value: value['selection'] is None)
                    self.assertEqual(cleared['copies'], copies)
                    terminal.send(b'y')
                    normal = terminal.checkpoint(lambda value: 'copied' not in value['footer'])
                    self.assertEqual(normal['copies'], copies)
                    if screen != 'Screen':
                        terminal.send(b'\x1b')
                        terminal.checkpoint(lambda value: value['screen'] == 'Screen')
                terminal.send(b'\x03')
                terminal.wait_exit()

    def test_claiming_log_pickup_and_g_reload_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root)
            log.unlink()
            state['assignment'].pop('run')
            publish_snapshot(path, state)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from tests.support import RecordingDescriptionTransport
from ub_agents.view_github import DescriptionLoads
from ub_agents.view_ui import LogPane, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    def proof_values(self):
        output = self.query_one(LogPane)
        row = self.rows.get(self.selected)
        return {'selected': self.selected, 'chosen': self.chosen,
                'state': row.state if row else None, 'follow': self.reading.follow,
                'empty': self.reading.empty_message, 'token': self.token,
                'reader': id(next(iter(self.worker.readers.values()), None)),
                'start': self.reading.page.start if self.reading.page else 0,
                'end': self.reading.page.end if self.reading.page else 0,
                'lines': [line.text for line in output.lines],
                'bottom': output.scroll_y == output.max_scroll_y,
                'footer': self.query_one('#status').render().plain,
                'calls': self.descriptions.transport.calls}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]),
          descriptions=DescriptionLoads(RecordingDescriptionTransport())).run()
'''
            with Terminal(script, root, path, proof, proof=proof) as terminal:
                claiming = terminal.checkpoint(lambda value: value['selected'] == 'assignment:claiming')
                self.assertEqual(claiming['empty'], 'No local log cached for this row.')
                terminal.send(b'\r')
                terminal.checkpoint(lambda value: value['chosen'])
                state['assignment']['run'] = 'owned-run'
                publish_snapshot(path, state)
                named = terminal.checkpoint(lambda value: value['selected'] == 'assignment:owned-run'
                                            and value['empty'] == 'No log output yet.')
                self.assertEqual(named['state'], 'running')
                terminal.send(b'g')
                missing = terminal.checkpoint(lambda value: value['token'] > named['token']
                                              and value['reader'] != named['reader'])
                self.assertEqual(missing['empty'], 'No log output yet.')
                log.write_bytes(event(1, 20))
                picked_up = terminal.checkpoint(lambda value: any('event 00001' in line for line in value['lines']))
                self.assertTrue(picked_up['follow'])
                self.assertEqual(picked_up['selected'], 'assignment:owned-run')
                terminal.send(b'f')
                paused = terminal.checkpoint(lambda value: not value['follow'])
                with log.open('ab') as stream:
                    stream.write(b''.join(event(i, 20) for i in range(2, 1000)))
                terminal.send(b'g')
                reloaded = terminal.checkpoint(lambda value: value['follow'] and value['bottom']
                                               and value['reader'] != paused['reader']
                                               and value['end'] == log.stat().st_size
                                               and any('event 00999' in line for line in value['lines']))
                self.assertGreater(reloaded['start'], 0)
                self.assertIn('g reload', reloaded['footer'])
                with log.open('ab') as stream:
                    stream.write(event(1000, 20))
                followed = terminal.checkpoint(lambda value: any('event 01000' in line for line in value['lines']))
                self.assertEqual(followed['calls'], [])
                terminal.send(b'q')
                terminal.wait_exit()

    def test_single_pane_resize_navigation_floor_and_q_in_real_terminal(self):
        self.check_single_pane_terminal(b'q')

    def test_single_pane_resize_navigation_floor_and_interrupt_in_real_terminal(self):
        self.check_single_pane_terminal(b'\x03')

    def check_single_pane_terminal(self, quit_key):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=600)
            state['base_version'] = '0.1.11'
            state['histories']['114']['runs'][0]['denials'] = [
                {'tool': 'Bash', 'command': 'pytest'}, {'tool': 'Write', 'command': 'report.md'}]
            state['activity'] = {'state': 'waiting', 'until':
                                 (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
            path.write_text(json.dumps(state))
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from textual.widgets import TabbedContent, Tree
from ub_agents.view_ui import LogPane, RecentActivity, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    def proof_values(self):
        output, tree, recent = self.query_one(LogPane), self.query_one(Tree), self.query_one(RecentActivity)
        tree.get_node_at_line(0)
        value = {'size': list(self.size), 'narrow': self.narrow, 'floor': self.too_small,
                 'item': self.item_view, 'work': self.query_one('#work_pane').display,
                 'panes': self.query_one('#panes').display,
                 'work_width': self.query_one('#work_pane').region.width,
                 'item_width': self.query_one('#panes').region.width,
                 'selected': self.selected, 'cursor': tree.cursor_node.data if tree.cursor_node else None,
                 'focus': self.focused.id if self.focused else None,
                 'tab': self.query_one(TabbedContent).active, 'follow': self.reading.follow,
                 'raw': self.reading.raw, 'anchor': output.anchor(),
                 'saved_anchor': self.reading.anchor,
                 'first_anchor': output.positions[0] if output.positions else None,
                 'render_width': output.render_width,
                 'output_width': output.scrollable_content_region.width,
                 'starts': [r.start for r in self.reading.page.refs] if self.reading.page else [],
                 'header': self.query_one('#item_header').render().plain,
                 'status': self.query_one('#run_status').render().plain,
                 'footer': self.query_one('#status').render().plain,
                 'screen': type(self.screen).__name__,
                 'modal': self.screen.query_one('#raw_details').render().plain
                          if self.screen.query('#raw_details') else '',
                 'visible': [strip.text.strip() for strip in self.screen._compositor.render_strips()
                             if strip.text.strip()],
                 'rows': tree.virtual_size.height,
                 'recent': recent.render().plain,
                 'upper_bottom': tree.region.bottom, 'recent_y': recent.region.y}
        return value
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof,
                          env={'NO_COLOR': None}) as terminal:
                transcript = terminal.transcript
                def check_resize(width, height):
                    terminal.resize(width, height)
                    return terminal.checkpoint(lambda value: value['size'] == [width, height]
                                      and value['narrow'] == (width < 110 or height < 32)
                                      and value['floor'] == (width < 60 or height < 16)
                                      and (value['floor'] and len(value['visible']) == 1
                                           or not value['floor'] and
                                           (not value['narrow'] or
                                            value['item_width' if value['item'] else 'work_width'] == width)))
                initial = terminal.checkpoint(lambda value: value['starts'] and value['cursor'] == value['selected'])
                self.assertEqual(initial['work_width'], 46)
                self.assertTrue(initial['work'] and initial['panes'])
                for size in ((109, 32), (110, 31), (60, 16), (80, 24)):
                    value = check_resize(*size)
                    self.assertTrue(value['narrow'])
                    self.assertFalse(value['floor'])
                    self.assertTrue(value['work'])
                    self.assertFalse(value['panes'])
                    self.assertEqual(value['work_width'], size[0])
                    self.assertEqual(value['rows'], 5)
                    self.assertEqual(value['upper_bottom'], value['recent_y'])
                    self.assertEqual(len(value['recent'].splitlines()), 2)
                    self.assertRegex(value['footer'], r'^v0\.1\.11 · poll \d+s')
                    self.assertTrue(value['footer'].endswith('↑↓ select ⏎ open ? keys q quit'))
                terminal.send(b'\r')
                opened = terminal.checkpoint(lambda value: value['item'] and value['focus'] == 'output'
                                    and 'no outcome reported' in value['status'])
                self.assertEqual(opened['item_width'], 80)
                self.assertFalse(opened['work'])
                self.assertIn('#114', opened['header'])
                self.assertIn('no outcome reported', opened['status'])
                self.assertTrue(opened['footer'].endswith('Esc back 1-3 tabs g reload ? keys q quit'))
                check_resize(80, 32)
                terminal.send(b'3')
                denied = terminal.checkpoint(lambda value: value['tab'] == 'runs'
                                    and any('2 denied' in line for line in value['visible']))
                self.assertNotIn('Bash:', '\n'.join(denied['visible']))
                self.assertNotIn('Write:', '\n'.join(denied['visible']))
                for size in ((110, 32), (60, 32)):
                    value = check_resize(*size)
                    if value['narrow'] and not value['item']:
                        terminal.send(b'\r')
                    runs = terminal.checkpoint(lambda value: value['tab'] == 'runs'
                               and any('2 denied' in line for line in value['visible']))
                    header = next(index for index, line in enumerate(runs['visible']) if 'outcome' in line)
                    # Filing and both runs occupy the next three lines, even at 60 columns.
                    history_rows = runs['visible'][header + 1:header + 4]
                    self.assertIn('filed', history_rows[0])
                    self.assertIn('2 denied', history_rows[1])
                    self.assertTrue(any(glyph in history_rows[2] for glyph in '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'))
                    self.assertNotIn('finalized', '\n'.join(history_rows))
                check_resize(80, 24)
                terminal.send(b'1')
                terminal.checkpoint(lambda value: value['tab'] == 'log')
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: not value['item'])
                terminal.send(b'\r')
                terminal.checkpoint(lambda value: value['item'] and value['focus'] == 'output')
                terminal.send(b'f')
                terminal.checkpoint(lambda value: not value['follow'])
                terminal.send(b'u')
                terminal.checkpoint(lambda value: value['raw'] and value['anchor'][0] == value['saved_anchor'][0])
                terminal.send(b'\x1b[H')
                top = terminal.checkpoint(lambda value: value['anchor'] == value['first_anchor'] == value['saved_anchor'])
                terminal.send(b'\x1b[6~')
                paused = terminal.checkpoint(lambda value: value['anchor'] == value['saved_anchor']
                                    and value['anchor'] != top['anchor'])
                for size in ((110, 32), (109, 32), (60, 16), (59, 16), (60, 15), (80, 24)):
                    value = check_resize(*size)
                    if not value['floor']:
                        value = terminal.checkpoint(lambda value: value['render_width'] == value['output_width']
                                           and value['anchor'][0] == paused['anchor'][0])
                    self.assertEqual(value['selected'], paused['selected'])
                    self.assertEqual(value['starts'], paused['starts'])
                    self.assertFalse(value['follow'])
                    self.assertTrue(value['raw'])
                    self.assertEqual(value['anchor'][0], paused['anchor'][0])
                    if value['floor']:
                        self.assertEqual(value['visible'], ['Please enlarge the terminal to at least 60×16.'])
                    else:
                        self.assertEqual(value['focus'], 'output')
                        self.assertTrue(value['panes'])
                        self.assertEqual(value['work'], not value['narrow'])
                terminal.send(b'2?')
                help_view = terminal.checkpoint(lambda value: value['screen'] == 'KeyHelp')
                self.assertIn('Enter below 110×32', help_view['modal'])
                self.assertIn('Esc below 110×32', help_view['modal'])
                self.assertEqual(check_resize(59, 16)['visible'], ['Please enlarge the terminal to at least 60×16.'])
                self.assertEqual(check_resize(80, 24)['screen'], 'KeyHelp')
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: value['screen'] != 'KeyHelp' and value['item'])
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: not value['item'] and value['focus'] == 'work')
                widened = check_resize(110, 32)
                self.assertEqual(widened['focus'], 'work')
                self.assertEqual(widened['tab'], 'issue')
                self.assertFalse(check_resize(80, 24)['item'])
                terminal.send(b'\r')
                terminal.checkpoint(lambda value: value['item'] and value['tab'] == 'issue')
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: not value['item'] and value['focus'] == 'work')
                terminal.send(b'\x1b[B')
                terminal.checkpoint(lambda value: value['cursor'] == 'plan:12' and value['focus'] == 'work')
                terminal.send(b'\x1b[B')
                terminal.checkpoint(lambda value: value['focus'] == 'recent')
                terminal.send(b'\r')
                terminal.checkpoint(lambda value: value['selected'] == 'outcome:previous-run' and value['item'])
                terminal.send(b'\x1b')
                terminal.checkpoint(lambda value: not value['item'] and value['focus'] == 'recent')
                check_resize(59, 16)
                terminal.send(quit_key)
                terminal.wait_exit()
                self.assertTrue(log.exists())

    def test_work_heading_separators_in_real_terminal_across_resize_refresh_and_scroll(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=1)
            attention = {'item': 20, 'agent': 'worker', 'state': 'blocked', 'title': 'Attention work'}
            eligible = {'item': 22, 'agent': 'worker', 'state': 'ready', 'title': 'Eligible work'}
            state['latest_pass'] = {'state': 'complete', 'rows': [attention, eligible]}
            path.write_text(json.dumps(state))
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from textual.binding import Binding
from textual.widgets import Tree
from ub_agents.view_ui import RecentActivity, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('s', 'scroll_work', priority=True)]
    def action_scroll_work(self):
        self.query_one(Tree).scroll_end(animate=False, immediate=True)
    def proof_values(self):
        tree, recent = self.query_one(Tree), self.query_one(RecentActivity)
        tree.get_node_at_line(0)
        strips = self.screen._compositor.render_strips()
        return {'narrow': self.narrow, 'selected': self.selected,
                'cursor': tree.cursor_node.data if tree.cursor_node else None,
                'focus': self.focused.id if self.focused else None,
                'groups': {name: node._line for name, node in self.groups.items()},
                'rows': tree.virtual_size.height, 'spacers': sorted(tree._spacer_lines),
                'scroll': tree.scroll_offset.y, 'upper_bottom': tree.region.bottom,
                'recent_y': recent.region.y, 'recent_scroll': recent.scroll_y,
                'live': [strip.crop(tree.region.x, tree.region.x + tree.scrollable_content_region.width).text
                         for strip in strips[tree.region.y:tree.region.bottom]]}
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof, size=(140, 44)) as terminal:
                for size in ((140, 44), (80, 32), (140, 44)):
                    terminal.resize(*size)
                    current = terminal.checkpoint(lambda value: value['narrow'] == (size[0] < 110)
                                     and len(value['groups']) == 3 and value['cursor'] == 'assignment:owned-run')
                    self.assertEqual(current['selected'], 'assignment:owned-run')
                    self.assertEqual(current['rows'], 8 if current['narrow'] else 11)
                    self.assertEqual(current['groups']['Running'], 0)
                    self.assertEqual(current['spacers'], sorted(current['groups'][name] - 1
                                                for name in ('Needs attention', 'Eligible')))
                    self.assertEqual(current['upper_bottom'], current['recent_y'])
                    for name in ('Needs attention', 'Eligible'):
                        line = current['groups'][name] - current['scroll']
                        self.assertEqual(current['live'][line - 1].strip(), '')
                        self.assertTrue(current['live'][line - 2].strip())
                        self.assertTrue(current['live'][line].startswith(name + ' · 1'))
                        self.assertTrue(current['live'][line + 1].strip())
                    # Arrow input crosses each blank row and returns without selecting a heading.
                    terminal.send(b'\x1b[B' * 2)
                    terminal.checkpoint(lambda value: value['cursor'] == 'plan:22')
                    terminal.send(b'\x1b[B')
                    terminal.checkpoint(lambda value: value['focus'] == 'recent')
                    terminal.send(b'\x1b[A' * 3)
                    terminal.checkpoint(lambda value: value['focus'] == 'work'
                                       and value['cursor'] == 'assignment:owned-run')
                boundary = current['recent_y']
                state['latest_pass']['rows'] = [eligible]
                path.write_text(json.dumps(state))
                hidden = terminal.checkpoint(lambda value: 'Needs attention' not in value['groups'])
                self.assertEqual(hidden['rows'], 7)
                self.assertEqual(hidden['spacers'], [3])
                self.assertEqual(hidden['recent_y'], boundary)
                self.assertEqual(hidden['selected'], 'assignment:owned-run')
                terminal.send(b'\x1b[B\r')
                terminal.checkpoint(lambda value: value['selected'] == 'plan:22')
                state['assignment'] = None
                path.write_text(json.dumps(state))
                idle = terminal.checkpoint(lambda value: value['rows'] == 6)
                self.assertEqual(idle['spacers'], [2])
                self.assertEqual(idle['groups']['Eligible'], 3)
                self.assertIn('Idle', idle['live'][1])
                self.assertEqual(idle['live'][2].strip(), '')
                self.assertEqual(idle['selected'], 'plan:22')
                state['latest_pass']['rows'] = [attention, eligible] + [
                    {'item': item, 'agent': 'worker', 'state': 'ready'} for item in range(30, 60)]
                path.write_text(json.dumps(state))
                # Eligible shows ten of its 31 items, still enough to scroll the upper half.
                capped = terminal.checkpoint(lambda value: value['rows'] == 37)
                self.assertTrue(capped['live'][capped['groups']['Eligible'] - capped['scroll']]
                                .startswith('Eligible · 31 · showing 10'))
                terminal.send(b's')
                scrolled = terminal.checkpoint(lambda value: value['scroll'] > 0)
                self.assertEqual(scrolled['recent_y'], boundary)
                self.assertEqual(scrolled['upper_bottom'], boundary)
                self.assertEqual(scrolled['recent_scroll'], 0)
                self.assertEqual(scrolled['selected'], 'plan:22')
                terminal.send(b'q')
                terminal.wait_exit()

    def test_update_banners_in_real_terminal_with_live_replay_and_resize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=100)
            replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream: stream.write(b'owned update replay\\n')
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from textual.binding import Binding
from textual.widgets import TabbedContent
from ub_agents.view_ui import LogPane, UpdateBanner, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('i', 'item_focus', priority=True)]
    def action_item_focus(self):
        self.query_one(LogPane).focus()
    def proof_values(self):
        banner = self.query_one(UpdateBanner)
        header = self.query_one('#item_header')
        output = self.query_one(LogPane)
        footer = self.query_one('#status')
        run_status = self.query_one('#run_status')
        value = {'display': banner.display, 'height': banner.size.height,
                 'width': banner.content_size.width, 'line': banner.render().plain,
                 'text': banner.banner.get('text'), 'terminal_width': self.size.width,
                 'cells': banner.render().cell_len, 'yellow': str(banner.styles.background),
                 'banner_y': banner.region.y, 'body_y': self.query_one('#body').region.y,
                 'work_y': self.query_one('#work_pane').region.y,
                 'header_y': header.region.y, 'header_height': header.size.height,
                 'header': header.render().plain, 'output_y': output.region.y,
                 'footer': footer.render().plain, 'footer_height': footer.size.height,
                 'footer_y': footer.region.y,
                 'run_status_bottom': run_status.region.bottom,
                 'active': self.query_one(TabbedContent).active,
                 'selected': self.selected, 'focus': self.focused.id if self.focused else None,
                 'follow': self.reading.follow, 'anchor': output.anchor(),
                 'saved_anchor': self.reading.anchor,
                 'first_anchor': output.positions[0] if output.positions else None,
                 'starts': [r.start for r in self.reading.page.refs] if self.reading.page else []}
        return value
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            try:
                with Terminal(script, root, path, proof, proof=proof,
                              env={'NO_COLOR': None}) as terminal:
                    transcript = terminal.transcript
                    initial = terminal.checkpoint(lambda value: value['starts'] and value['focus'] is not None
                                    and value['footer'].endswith('↑↓ select ⏎ open 1-3 tabs g reload ? keys q quit'))
                    window_title = 'ub-agents launch — example/repo'.encode()
                    self.assertTrue(b'\x1b]0;' + window_title + b'\x07' in transcript,
                                    'Terminal output is missing the window-title OSC sequence')
                    self.assertNotIn(b'FOLLOW', transcript)
                    self.assertFalse(initial['display'])
                    terminal.send(b'f')
                    terminal.checkpoint(lambda value: not value['follow'])
                    terminal.send(b'\x1b[Hi')
                    paused = terminal.checkpoint(lambda value: not value['follow'] and value['anchor'] is not None
                                   and value['focus'] == 'output'
                                   and value['anchor'] == value['first_anchor'] == value['saved_anchor'])
                    self.assertFalse(paused['follow'])
                    from tests.test_updates import release
                    from ub_agents.updates import release_banner
                    cases = [release_banner(release(), 'brew'), release_banner(release(), 'pip'),
                             {'text': '⬆ This launcher runs code 2 commits behind origin/main · restart the launcher'}]
                    for banner in cases:
                        if 'released_at' in banner:
                            banner['released_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                        state['update'] = banner
                        # Publish a fresh launcher snapshot even after a slow PTY startup.
                        state['published_at'] = datetime.now(timezone.utc).isoformat()
                        path.write_text(json.dumps(state))
                        for width in (110, 70, 170):
                            terminal.resize(width, 32)
                            keys = ('f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'
                                    if width >= 110 else 'f follow h older u raw PgUp/Dn ? keys q quit')
                            current = terminal.checkpoint(lambda value: value['text'] == banner['text']
                                            and value['display'] and value['height'] == 1
                                            and value['terminal_width'] == width
                                            and value['width'] == width - 2 and value['body_y'] == 1
                                            and value['anchor'] == paused['anchor']
                                            and value['footer'].endswith(keys))
                            self.assertTrue(current['display'])
                            self.assertEqual(current['height'], 1)
                            self.assertEqual(current['banner_y'], 0)
                            self.assertEqual(current['body_y'], 1)
                            if width >= 110:
                                self.assertEqual(current['work_y'], 1)
                            self.assertGreater(current['header_y'], current['banner_y'])
                            self.assertEqual(current['header_height'], 3)
                            self.assertLessEqual(current['header_y'] + current['header_height'], current['output_y'])
                            self.assertIn('#114', current['header'])
                            self.assertEqual(current['yellow'], 'Color(255, 139, 127)')
                            self.assertLessEqual(current['cells'], width - 2)
                            self.assertEqual(current['selected'], paused['selected'])
                            self.assertEqual(current['focus'], paused['focus'])
                            self.assertEqual(current['starts'], paused['starts'])
                            self.assertEqual(current['anchor'], paused['anchor'])
                            self.assertEqual(current['saved_anchor'], paused['saved_anchor'])
                            self.assertFalse(current['follow'])
                            self.assertEqual(current['footer_height'], 1)
                            self.assertEqual(current['footer_y'], 31)
                            self.assertLessEqual(current['run_status_bottom'], current['footer_y'])
                            self.assertTrue(current['footer'].endswith(keys))
                            if 'released_at' in banner:
                                self.assertTrue(current['line'].endswith('released 2 days ago'))
                                self.assertEqual(current['cells'], width - 2)
                            if width == 170:
                                self.assertIn(banner['text'], current['line'])
                    self.assertIn(b'released 2 days ago', transcript)
                    self.assertIn(b'restart the launcher', transcript)
                    state['update'] = None
                    path.write_text(json.dumps(state))
                    cleared = terminal.checkpoint(lambda value: not value['display'] and value['body_y'] == 0
                                    and value['anchor'] == paused['anchor'])
                    self.assertFalse(cleared['display'])
                    self.assertEqual(cleared['body_y'], 0)
                    self.assertEqual(cleared['anchor'], paused['anchor'])
                    # Repeated snapshot reads do not re-emit an unchanged title.
                    self.assertEqual(transcript.count(b'\x1b]0;' + window_title + b'\x07'), 1)
                    self.assertNotIn(b'\x1b]0;\x07', transcript)
                    # Repository controls must remain inert inside the OSC payload.
                    state['repository'] = 'other/repo\x07\x1b]0;injected\x1b\\\n'
                    path.write_text(json.dumps(state))
                    changed_title = ('ub-agents launch — ' + r'other/repo\x07\x1b]0;injected\x1b\\n').encode()
                    terminal.checkpoint(lambda _: b'\x1b]0;' + changed_title + b'\x07' in transcript)
                    self.assertNotIn(b'\x1b]0;injected', transcript)
                    before = log.stat().st_size
                    terminal.send(b'q')
                    terminal.wait_exit(0)
                    terminal.wait_for(lambda: log.stat().st_size > before)
                    self.assertEqual(transcript.count(b'\x1b]0;' + changed_title + b'\x07'), 1)
                    self.assertEqual(transcript.count(b'\x1b]0;\x07'), 1)
                    self.assertIsNone(replay.poll())
                    self.assertGreater(log.stat().st_size, before)
            finally:
                replay.communicate(b'stop\n', timeout=3)
                self.assertEqual(replay.returncode, 0)

    # One test per quit key, so a parallel run spreads them over cores.
    def test_real_terminal_explicit_load_cache_and_q_during_hung_request(self):
        self.check_load_cache_and_quit_during_hung_request(b'q')

    def test_real_terminal_explicit_load_cache_and_interrupt_during_hung_request(self):
        self.check_load_cache_and_quit_during_hung_request(b'\x03')

    def check_load_cache_and_quit_during_hung_request(self, quit_key):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, count=100)
            (log.parent / 'context.json').unlink()
            calls, release, mode = root / 'calls.jsonl', root / 'release', root / 'mode'
            mode.write_text('success')
            shim = root / 'gh'
            body = ('## Terminal Markdown\n\nTerminal loaded body\nSecond line\n\n'
                    '- **strong** and *emphasis* with `code`\n'
                    '- [bold]literal[/bold] \x1b[31m\n\n'
                    '```text\ncode\tline\n```\n\n'
                    '[web](https://example.invalid)\n'
                    '![pic](https://example.invalid/pic) <b>HTML</b>')
            shim.write_text(f'#!{sys.executable}\nbody = {body!r}\n' + '''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while (root / 'mode').read_text() == 'hang' or not (root / 'release').exists():
    time.sleep(0.02)
print('HTTP/2.0 200 OK\\nX-Ratelimit-Remaining: 500\\n')
print(json.dumps({'data': {'repository': {'issueOrPullRequest': {
    'title': 'Terminal loaded title', 'body': body}}}}))
''')
            shim.chmod(0o700)
            replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream:
        stream.write(b'owned replay output\\n')
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def recorded():
                return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            proof = root / 'proof.json'
            script = f'''
import ub_agents.view_ui
from ub_agents.view import main
class ProofView(checkpoint_view(ub_agents.view_ui.View, {str(proof)!r})):
    def proof_values(self):
        return {{'tab': self.query_one('#panes').active}}
ub_agents.view_ui.View = ProofView
raise SystemExit(main())
'''
            try:
                with Terminal(script, root, proof=proof,
                              env={'NO_COLOR': None, 'PATH': str(root) + os.pathsep + os.environ['PATH']}) as terminal:
                    transcript = terminal.transcript
                    terminal.expect(b'running assignment', b'owned replay output')
                    terminal.send(b'2')
                    terminal.expect(b'Press g on Issue')
                    self.assertEqual(recorded(), [])
                    terminal.send(b'g')
                    terminal.expect(b'Loading title/body', lambda _: len(recorded()) == 1)
                    terminal.send(b'g3g1g2g')
                    terminal.checkpoint()
                    self.assertEqual(len(recorded()), 1)
                    release.touch()
                    loaded = terminal.expect(b'Terminal loaded body', b'Terminal Markdown', b'Second line',
                                   b'[bold]literal[/bold]', br'\x1b[31m', b'Source: GitHub')
                    self.assertNotIn(b'\x1b[31m', loaded)
                    self.assertNotIn(b'**strong**', loaded)
                    self.assertNotIn(b'## Terminal Markdown', loaded)
                    terminal.send(b'3g1g2g')
                    terminal.checkpoint()
                    self.assertEqual(len(recorded()), 1)
                    state['latest_pass']['rows'][0]['description'] = {
                        'available': True, 'text': '## Snapshot Markdown\n\nSnapshot body\nSecond line\n\n- **strong**'}
                    path.write_text(json.dumps(state))
                    snapshot = terminal.expect(b'Snapshot Markdown', b'Source: snapshot')
                    self.assertNotIn(b'## Snapshot Markdown', snapshot)
                    state['latest_pass']['rows'][0]['description'] = {
                        'available': True, 'text': '```text\n' + 'x' * 3000,
                        'omitted_characters': 1000}
                    path.write_text(json.dumps(state))
                    terminal.expect(b'Description shortened')
                    self.assertEqual(len(recorded()), 1)
                    # A new selected item has no local or in-memory description.
                    state['assignment']['item'] = 116
                    path.write_text(json.dumps(state))
                    terminal.expect(b'Press g on Issue')
                    mode.write_text('hang')
                    terminal.send(b'g')
                    terminal.expect(b'Loading title/body', lambda _: len(recorded()) == 2)
                    owned_pid = recorded()[-1]['pid']
                    os.kill(owned_pid, 0)
                    terminal.resize(120, 36)
                    terminal.send(b'g1f2g')
                    terminal.checkpoint()
                    self.assertEqual(len(recorded()), 2)
                    before = log.stat().st_size
                    started = time.monotonic()
                    terminal.send(quit_key)
                    terminal.wait_exit(timeout=2)
                    self.assertLess(time.monotonic() - started, 2)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned_pid, 0)
                    self.assertIsNone(replay.poll())
                    self.assertGreater(log.stat().st_size, before)
            finally:
                # Clean only processes recorded by this owned acceptance check,
                # even if an assertion exposed a request cleanup regression.
                for record in recorded():
                    try:
                        os.kill(record['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                replay.communicate(b'stop\n', timeout=5)
                self.assertEqual(replay.returncode, 0)

    def test_real_terminal_controls_restoration_and_replay_isolation_on_q(self):
        self.check_controls_restoration_and_replay_isolation(b'q')

    def test_real_terminal_controls_restoration_and_replay_isolation_on_interrupt(self):
        self.check_controls_restoration_and_replay_isolation(b'\x03')

    def test_real_terminal_controls_restoration_and_replay_isolation_on_crash(self):
        self.check_controls_restoration_and_replay_isolation(b'x')

    def check_controls_restoration_and_replay_isolation(self, quit_key):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, _ = fixture(root, count=900)
            (log.parent / 'context.json').write_text(json.dumps({
                'title': 'Cached title [bold]literal[/bold]',
                'body': '## Cached Markdown\n\nCached description\r\nSecond line\rThird line\n\n'
                        '- **strong** and *emphasis* with `code`\n\n```text\ncode\tline\n```'}))
            # This owned process stands in for a launcher publishing live output.
            replay = subprocess.Popen([sys.executable, '-c', '''
import pathlib, select, sys
log = pathlib.Path(sys.argv[1])
index = 900
while not select.select([sys.stdin], [], [], 0.01)[0]:
    with log.open('ab') as stream:
        stream.write((f'replay output {index} ' + 'x' * 500 + '\\n').encode())
    index += 1
''', str(log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                script = 'from ub_agents.view import main; raise SystemExit(main())'
                arguments = (root, '--session', 'launcher')
                if quit_key == b'x':
                    script = '''
from pathlib import Path
import sys
from textual.binding import Binding
from ub_agents.view_ui import LogPane, View
class BrokenView(View):
    BINDINGS = [*View.BINDINGS, Binding('x', 'break_render', priority=True)]
    def action_break_render(self):
        def broken(y):
            raise RuntimeError('Intentional rendering failure')
        pane = self.query_one(LogPane)
        pane.render_line = broken
        pane.refresh()
app = BrokenView(Path(sys.argv[1]), Path(sys.argv[2]))
app.run()
sys.exit(app.return_code or 1)
'''
                    arguments = (root, path)
                with Terminal(script, *arguments, env={'NO_COLOR': None}) as terminal:
                    transcript = terminal.transcript
                    # The page is loaded once live replay output is on screen.
                    terminal.expect(b'running assignment', b'? keys q quit', b'Running', b'partial',
                          b'\x1b[?1049h', b'replay output')
                    self.assertNotIn(b'FOLLOW', transcript)
                    self.assertNotIn(b'FORMATTED', transcript)
                    self.assertIsNone(terminal.process.poll(), bytes(transcript[-1000:]))
                    terminal.send(b'f')
                    terminal.expect(b'PAUSED')
                    terminal.send(b'\x1b[5~')  # Page Up
                    terminal.send(b'h')
                    # Older page or split-record boundary.
                    terminal.expect(lambda out: b'Older page' in out or b'Page byte boundary' in out)
                    terminal.send(b'u')
                    terminal.expect(b'RAW')
                    terminal.send(b'2')
                    cached = terminal.expect(b'Cached description', b'Cached Markdown', b'Second line', b'Third line')
                    self.assertNotIn(b'## Cached Markdown', cached)
                    self.assertNotIn(b'**strong**', cached)
                    # Give the Runs table room to show the blocker prefix.
                    terminal.resize(150, 32)
                    terminal.send(b'3')
                    terminal.expect(b'filed by bk-one', b'build-01', b'BLOCKED:')
                    terminal.resize(110, 32)
                    terminal.send(b'1p')
                    terminal.expect(b'process.log', b'Displayed bytes', b'evicted', b'Rendered limit 400')
                    terminal.send(b'\x1b')
                    terminal.send(b'f')
                    terminal.expect('↑↓ select'.encode())
                    self.assertNotIn(b'FOLLOW', transcript)
                    before_size = log.stat().st_size
                    terminal.send(quit_key)
                    # Keep draining until exit. A rich crash traceback can fill
                    # a small CI PTY buffer and block if wait() stops reading.
                    terminal.wait_exit(1 if quit_key == b'x' else 0)
                    if quit_key == b'x':
                        self.assertIn(b'Intentional rendering failure', transcript)
                    self.assertTrue(b'\x1b]0;' + 'ub-agents launch — example/repo'.encode() + b'\x07' in transcript,
                                    'Terminal output is missing the window-title OSC sequence')
                    self.assertEqual(transcript.count(b'\x1b]0;\x07'), 1)
                    self.assertIsNone(replay.poll(), 'View stopped replay process')
                    self.assertGreater(log.stat().st_size, before_size)
            finally:
                replay.communicate(b'stop\n', timeout=5)
                self.assertEqual(replay.returncode, 0)


if __name__ == '__main__':
    unittest.main()

class TerminalRetentionTests(unittest.TestCase):
    def test_compact_claude_replay_hidden_and_failed_result_toggles_in_real_terminal(self):
        self.compact_runtime_replay('claude')

    def test_compact_codex_replay_hidden_and_failed_result_toggles_in_real_terminal(self):
        self.compact_runtime_replay('codex')

    def compact_runtime_replay(self, runtime):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root, runtime=f'{runtime}:synthetic-model:high')
            tail_count = 4 if runtime == 'claude' else 40
            state['assignment'].update(kind='issue', attempt=2)
            state['outcomes'].append({'item': 114, 'run': 'earlier-run', 'handoff': 185})
            path.write_text(json.dumps(state))
            capture = Path(__file__).parent / f'fixtures/runtime_logs/{runtime}.log'
            proof = root / 'proof.json'
            script = '''
import pathlib, sys
from textual.binding import Binding
from ub_agents.view_ui import LogPane, View
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('y', 'seek_failure', priority=True)]
    def action_seek_failure(self):
        self.reading.follow = False
        self.reading.anchor = (next(ref.start for ref in self.reading.page.refs
                                   if ref.value.kind == 'tool ERROR'), 0)
        self.query_one(LogPane).reflow()
    def proof_values(self):
        pane = self.query_one(LogPane)
        r = self.reading
        refs = r.page.refs if r.page else []
        value = {'ready': r.page is not None, 'raw': r.raw, 'anchor': pane.anchor(), 'entries': len(refs),
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'notice': self.query_one('#log_note').render().plain,
                 'raw_details': self.raw_details(),
                 'modal': self.screen.query_one('#raw_details').render().plain
                          if self.screen.query('#raw_details') else '',
                 'lines': [line.text for line in pane.lines],
                 'refs': [(ref.start, ref.value.kind) for ref in refs],
                 'positions': pane.positions}
        return value
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            with Terminal(script, root, path, proof, proof=proof,
                          env={'NO_COLOR': None}) as terminal:
                transcript = terminal.transcript
                terminal.checkpoint(lambda value: value['ready'])
                # Appending after attachment replays every recorded input from
                # byte zero, preserving capture times as well as producer times.
                from tests.test_view_data import event
                with log.open('ab') as stream:
                    stream.write(capture.read_bytes())
                    if runtime == 'claude':
                        # Keep the first hidden record within the narrower raw render budget.
                        stream.write(b''.join(event(i, 20) for i in range(tail_count)))
                    else:
                        from tests.test_codex_logs import encoded
                        stream.write(b''.join(encoded({'type': 'item.completed', 'item': {
                            'id': f'after_{i}', 'type': 'agent_message', 'text': f'after {i}'}}) for i in range(tail_count)))
                formatted = terminal.checkpoint(lambda value: value['entries'] == len(capture.read_bytes().splitlines()) + tail_count)
                self.assertEqual(formatted['entries'], len(capture.read_bytes().splitlines()) + tail_count)
                text = '\n'.join(formatted['lines'])
                if runtime == 'claude':
                    self.assertIn('· thinking', text)
                    self.assertIn('▸ Read <fixture>/fixture_output.py', text)
                    self.assertIn('▸ Bash cat missing-owned.txt', text)
                    self.assertIn('✗ Exit code 1', text)
                else:
                    self.assertIn('▸ Bash /bin/zsh', text)
                    self.assertIn('▸ file add <fixture>/greeting.txt', text)
                    self.assertIn('▸ fixture.echo', text)
                    self.assertIn('✗ Exit code 7: owned failure', text)
                    self.assertTrue(any(line.startswith('~') for line in formatted['lines']))
                self.assertIn('✓ run finished', text)
                self.assertNotIn('thinking_tokens', text)
                self.assertNotIn('producer=', text)
                self.assertEqual(formatted['header'].splitlines()[:2],
                                 ['#114 Cached title',
                                  ('implementer · claude synthetic-model high · attempt 2 · …'
                                   if runtime == 'claude' else
                                   'implementer · codex synthetic-model high · attempt 2 · ⌥…')])
                # A runtime's success line does not establish a workflow report.
                self.assertIn('implementer running · no outcome reported', formatted['run_status'])
                self.assertTrue(formatted['run_status'].endswith('1 earlier run'))
                self.assertEqual(formatted['notice'], '')
                self.assertIn('bytes ', formatted['raw_details'])
                self.assertIn('Rendered limit 400', formatted['raw_details'])
                for tab in (b'2', b'3', b'1'):
                    terminal.send(tab)
                    self.assertEqual(terminal.checkpoint()['header'], formatted['header'])
                self.assertEqual(terminal.checkpoint()['lines'], formatted['lines'])
                terminal.send(b'f')
                terminal.checkpoint()
                terminal.send(b'u')
                terminal.checkpoint()
                terminal.send(b'\x1b[H')  # Home on a hidden raw record.
                hidden = terminal.checkpoint()
                self.assertTrue(hidden['raw'])
                self.assertEqual(hidden['anchor'][0], 0)
                self.assertIn('task_started' if runtime == 'claude' else 'thread.started', '\n'.join(hidden['lines']))
                terminal.send(b'u')
                self.assertEqual(terminal.checkpoint()['anchor'], hidden['anchor'])
                terminal.resize(120, 36)
                self.assertEqual(terminal.checkpoint()['anchor'], hidden['anchor'])
                terminal.send(b'u')
                self.assertEqual(terminal.checkpoint()['anchor'][0], hidden['anchor'][0])
                terminal.send(b'uy')  # Format and seek the recorded failure.
                failed = terminal.checkpoint()
                failed_start = next(start for start, kind in failed['refs'] if kind == 'tool ERROR')
                self.assertEqual(failed['anchor'][0], failed_start)
                terminal.send(b'u')
                raw_failed = terminal.checkpoint()
                self.assertEqual(raw_failed['anchor'][0], failed_start)
                # Raw JSON wraps at the pane width, including within error text.
                self.assertIn('No such file' if runtime == 'claude' else 'owned failure',
                              ' '.join(' '.join(raw_failed['lines']).split()))
                terminal.send(b'u')
                self.assertEqual(terminal.checkpoint()['anchor'][0], failed_start)
                if runtime == 'claude':
                    # Synthetic #192 replay: attach mid-init, then stream several
                    # progress records and verify the actual terminal projection.
                    from tests.test_log_reader import record, progress, result, tool
                    init = json.dumps({'type': 'system', 'subtype': 'init',
                                       'tools': ['private-tool'] * 5000}).encode() + b'\n'
                    call = record(content=[{**tool(name='Edit'), 'input': {
                        'file_path': 'long/' * 80, 'new_string': 'one\ntwo\n', 'old_string': 'old'}}])
                    terminal.send(b'f')
                    terminal.checkpoint()
                    # Replace the file in one step: a reader that sees a half-written
                    # generation stamps later records with live capture times.
                    replacement = log.with_name(log.name + '.new')
                    replacement.write_bytes(init + record(content=[{'type': 'thinking'}], timestamp='2026-10-03T12:00:00Z') +
                                            record(content=[{'type': 'text', 'text': 'unknown\ncontinuation'}]) + call)
                    os.replace(replacement, log)
                    skipped = terminal.checkpoint(lambda value: any('earlier output skipped' in line for line in value['lines']))
                    self.assertIn('          · earlier output skipped · h older', skipped['lines'])
                    self.assertNotIn('private-tool', '\n'.join(skipped['lines']))
                    thinking = next(line for line in skipped['lines'] if '· thinking' in line)
                    self.assertEqual(thinking[0], ' ')
                    self.assertEqual(thinking[9:], ' · thinking')
                    self.assertIn('          unknown', skipped['lines'])
                    self.assertIn('          continuation', skipped['lines'])
                    with log.open('ab') as stream:
                        stream.write(progress(45))
                    elapsed = terminal.checkpoint(lambda value: any(line.endswith(' · 45s') for line in value['lines']))
                    self.assertTrue(next(line for line in elapsed['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 45s'))
                    self.assertNotIn('tool_progress', '\n'.join(elapsed['lines']))
                    terminal.send(b'f')
                    terminal.checkpoint()
                    with log.open('ab') as stream:
                        stream.write(progress(60) + progress(119) + record('user', [result()]) +
                                     record(content=[{'type': 'text', 'text': 'captured'}]))
                    self.assertEqual(terminal.checkpoint()['lines'], elapsed['lines'])
                    terminal.send(b'u')
                    raw_progress = terminal.checkpoint()
                    self.assertIn('tool_progress', '\n'.join(raw_progress['lines']))
                    self.assertIn('private-tool', '\n'.join(raw_progress['lines']))
                    self.assertNotIn('earlier output skipped', '\n'.join(raw_progress['lines']))
                    terminal.send(b'uf')
                    finished = terminal.checkpoint(lambda value: any(line.endswith(' · 1m') for line in value['lines']))
                    self.assertTrue(next(line for line in finished['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 1m'))
                    captured = next(line for line in finished['lines'] if 'captured' in line)
                    self.assertEqual(captured[0], '~')
                    self.assertEqual(captured[9:], ' captured')
                    terminal.resize(110, 32)
                    self.assertTrue(next(line for line in terminal.checkpoint()['lines'] if '▸ Edit' in line).endswith('… +2 -1 · 1m'))
                    terminal.send(b'fh')
                    older = terminal.checkpoint(lambda value: bool(value['refs']) and value['refs'][0][0] < finished['refs'][0][0])
                    self.assertEqual(older['lines'], ['          · earlier output skipped · h older'])
                    terminal.send(b'u')
                    self.assertIn('private-tool', '\n'.join(terminal.checkpoint()['lines']))
                terminal.send(b'p')
                details = terminal.checkpoint(lambda value: bool(value['modal']))['modal']
                self.assertIn('bytes ', details)
                self.assertIn('Rendered limit 400:', details)
                self.assertIn('process.log', details)
                terminal.send(b'\x1b')
                terminal.checkpoint()
                # A standalone observer restores its terminal and leaves its
                # owned replay file unchanged on quit.
                before = log.read_bytes()
                terminal.send(b'q')
                terminal.wait_exit()
                self.assertEqual(log.read_bytes(), before)

    def test_captured_log_pause_retention_and_generation_in_real_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, log, state = fixture(root)
            state['assignment'].update(kind='issue', attempt=1)
            state['base_version'] = '9.8.7'
            capture = Path(__file__).parent / 'fixtures/runtime_logs/claude.log'
            log.write_bytes(capture.read_bytes() * 20)
            state['latest_pass']['rows'].extend([
                {'item': 20, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'},
                {'item': 21, 'agent': 'worker', 'state': 'recover', 'reason': 'Pending outcome'},
                {'item': 22, 'agent': 'worker', 'state': 'blocked', 'reason': 'Cleanup unconfirmed'},
                {'item': 23, 'agent': 'worker', 'state': 'parked', 'reason': 'Stop label needs-human is present'},
                {'item': 24, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required'},
                {'item': 25, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for blockers #31'},
                {'item': 26, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for active milestone #10'},
                {'item': 27, 'agent': 'worker', 'state': 'backoff', 'reason': 'Retry backoff'},
                {'item': 28, 'agent': 'worker', 'state': 'waiting', 'reason': 'Runtime paused'},
            ])
            now = datetime.now(timezone.utc)
            state['outcomes'][0]['time'] = now.isoformat()
            state['outcomes'].insert(0, dict(state['outcomes'][0], run='older-run',
                                             time=(now - timedelta(days=2)).isoformat()))
            path.write_text(json.dumps(state))
            previous = root / '.ub-agents' / 'runs' / 'previous-run'
            previous.mkdir()
            outcome_log = previous / 'process.log'
            outcome_log.write_bytes(capture.read_bytes() * 20)
            script = '''
import pathlib, sys
from textual.binding import Binding
from textual.widgets import Static, Tree
from ub_agents.view_ui import LogPane, View, RecentActivity
class ProofView(checkpoint_view(View, sys.argv[3])):
    BINDINGS = [Binding('r', 'recent_cursor', priority=True), Binding('s', 'plan_cursor', priority=True),
                Binding('a', 'assignment_cursor', priority=True)]
    def cursor(self, node):
        tree = self.query_one(Tree)
        tree.focus()
        tree.move_cursor(node)
    def action_recent_cursor(self):
        recent = self.query_one(RecentActivity)
        recent.cursor = recent.visible_rows[0].key
        recent.focus()
        recent.refresh()
    def action_plan_cursor(self):
        self.cursor(self.nodes['plan:21'])
    def action_assignment_cursor(self):
        self.cursor(self.nodes['assignment:owned-run'])
    def proof_values(self):
        pane = self.query_one(LogPane)
        r = self.reading
        tree = self.query_one(Tree)
        tree.get_node_at_line(0)
        recent = self.query_one(RecentActivity)
        row = self.rows.get(self.selected)
        value = {'follow': r.follow, 'raw': r.raw, 'generation': r.page.generation if r.page else None,
                 'starts': [ref.start for ref in r.page.refs] if r.page else [], 'anchor': pane.anchor(),
                 'saved_anchor': r.anchor,
                 'first_anchor': pane.positions[0] if pane.positions else None,
                 'entries': r.log.total_entries if r.log else 0, 'lag': r.log.unread_bytes if r.log else 0,
                 'notice': self.query_one('#log_note').render().plain,
                 'header': self.query_one('#item_header').render().plain,
                 'run_status': self.query_one('#run_status').render().plain,
                 'raw_details': self.raw_details(),
                 'footer': self.query_one('#status', Static).render().plain,
                 'footer_height': self.query_one('#status').size.height,
                 'terminal_size': [self.size.width, self.size.height],
                 'pill': self.query_one('#log_state', Static).render().plain,
                 'pill_visible': self.query_one('#log_state').display,
                 'screen': type(self.screen).__name__,
                 'modal': self.screen.query_one('#raw_details', Static).render().plain
                          if self.screen.query('#raw_details') else '',
                 'selected': self.selected, 'group': row.group if row else None,
                 'state': row.state if row else None,
                 'cursor': tree.cursor_node.data if tree.cursor_node else None,
                 'focus': self.focused.id if self.focused else None,
                 'title': self.query_one('#work_pane').border_title,
                 'sections': [node.label.plain for node in tree.root.children],
                 'nodes': list(self.nodes),
                 'work_lines': {key: tree.render_line(node._line - tree.scroll_offset.y).text
                                for key, node in self.nodes.items()},
                 'work_details': {key: tree.render_line(node._line + 1 - tree.scroll_offset.y).text
                                  for key, node in self.nodes.items()},
                 'work_width': tree.scrollable_content_region.width,
                 'idle': self.idle_node.label.plain if self.idle_node else None,
                 'idle_dim': self.idle_node.label.style == 'dim' if self.idle_node else False,
                 'eligible': [node.data for node in self.groups.get('Eligible', tree.root).children],
                 'recent': recent.render().plain,
                 'recent_rows': [row.key for row in recent.visible_rows],
                 'recent_bounds': [recent.region.y, recent.size.height],
                 'upper_bounds': [tree.region.y, tree.size.height],
                 'upper_scroll': tree.scroll_y, 'recent_scroll': recent.scroll_y}
        return value
ProofView(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])).run()
'''
            proof = root / 'proof.json'
            assignment, latest_pass, outcomes = state['assignment'], state['latest_pass'], state['outcomes']
            state.update(assignment=None, latest_pass={'state': 'partial', 'rows': [latest_pass['rows'][2]]},
                         outcomes=[])
            path.write_text(json.dumps(state))
            with Terminal(script, root, path, proof, proof=proof,
                          env={'NO_COLOR': None}) as terminal:
                transcript = terminal.transcript
                def pause_at_top():
                    terminal.send(b'f')
                    paused = terminal.checkpoint(lambda value: not value['follow'])
                    self.assertFalse(paused['follow'])
                    terminal.send(b'\x1b[H')
                    scrolled = terminal.checkpoint(lambda value: value['anchor'] is not None
                                          and value['anchor'] == value['first_anchor'] == value['saved_anchor'])
                    self.assertFalse(scrolled['follow'])
                    self.assertIsNotNone(scrolled['anchor'])
                    self.assertEqual(scrolled['anchor'], scrolled['first_anchor'])
                    self.assertEqual(scrolled['anchor'], scrolled['saved_anchor'])
                    return scrolled
                idle = terminal.checkpoint(lambda value: value['sections'] == ['Running · 0'] and value['idle'])
                self.assertIsNone(idle['selected'])
                self.assertEqual(idle['nodes'], [])
                self.assertEqual(idle['idle'], '    Idle · nothing eligible for this launcher')
                self.assertTrue(idle['idle_dim'])
                self.assertIn('○ Idle · waiting for the next poll', idle['run_status'])
                self.assertIn(b'Idle', transcript)
                terminal.send(b'\x1b[B\x1b[B\r')
                self.assertIsNone(terminal.checkpoint()['selected'])
                state.update(latest_pass={'state': 'complete', 'rows': []}, outcomes=outcomes)
                path.write_text(json.dumps(state))
                newest = terminal.checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                    and value['anchor'] is not None)
                self.assertEqual(newest['sections'], ['Running · 0'])
                self.assertEqual(newest['focus'], 'recent')
                self.assertEqual(newest['recent_rows'], ['outcome:previous-run', 'outcome:older-run'])
                state.update(assignment=assignment, latest_pass=latest_pass)
                path.write_text(json.dumps(state))
                initial = terminal.checkpoint(lambda value: len(value['sections']) == 3 and value['anchor'] is not None
                                     and value['selected'] == 'assignment:owned-run'
                                     and '#114 Cached title' in value['header'] and not value['pill_visible'])
                self.assertNotIn(b'FORMATTED', transcript)
                self.assertNotIn(b'FOLLOW', transcript)
                self.assertIn('ub-agents v9.8.7 · running assignment', initial['footer'])
                self.assertTrue(initial['footer'].endswith('↑↓ select ⏎ open 1-3 tabs g reload ? keys q quit'))
                self.assertEqual(initial['footer_height'], 1)
                self.assertFalse(initial['pill_visible'])
                self.assertEqual(initial['notice'], '')
                self.assertEqual(initial['sections'], ['Running · 1', 'Needs attention · 3',
                                                       'Eligible · 5'])
                self.assertEqual(initial['eligible'], ['plan:12', 'plan:20', 'plan:21',
                                                      'plan:27', 'plan:28'])
                for item in (25, 26):
                    self.assertNotIn(f'plan:{item}', initial['nodes'])
                self.assertTrue(initial['work_lines']['plan:12'].endswith('next'))
                for item, status in ((27, 'backoff'), (28, 'waiting')):
                    line = initial['work_lines'][f'plan:{item}']
                    self.assertTrue(line.startswith(f'◷ #{item}'), line)
                    self.assertTrue(line.endswith(status), line)
                    self.assertNotIn('next', line)
                self.assertNotIn('plan:13:reviewer', initial['nodes'])
                self.assertIsNone(initial['idle'])
                self.assertIn('partial', initial['title'])
                self.assertTrue(initial['recent'].splitlines()[0].startswith('Recent activity · 1 today'))
                self.assertEqual(initial['recent_rows'], ['outcome:previous-run', 'outcome:older-run'])
                self.assertLessEqual(abs(initial['upper_bounds'][1] - initial['recent_bounds'][1]), 1)
                self.assertEqual(sum(initial['upper_bounds']), initial['recent_bounds'][0])
                self.assertEqual(initial['header'].splitlines()[:2],
                                 ['#114 Cached title', 'implementer · claude synthetic-model high · attempt 1'])
                self.assertIn('implementer running · no outcome reported', initial['run_status'])
                self.assertNotIn('bytes ', initial['notice'])
                for tab in (b'2', b'3', b'1'):
                    terminal.send(tab)
                    self.assertEqual(terminal.checkpoint()['header'], initial['header'])
                state['activity'] = {'state': 'waiting', 'until': (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
                path.write_text(json.dumps(state))
                waiting = terminal.checkpoint(lambda value: re.search(r'next poll (\d+)s', value['footer']))
                remaining = int(re.search(r'next poll (\d+)s', waiting['footer']).group(1))
                counted = terminal.checkpoint(lambda value: re.search(r'next poll (\d+)s', value['footer'])
                                     and int(re.search(r'next poll (\d+)s', value['footer']).group(1)) < remaining)
                self.assertLess(int(re.search(r'next poll (\d+)s', counted['footer']).group(1)), remaining)
                state['activity'] = {'state': 'stopping'}
                path.write_text(json.dumps(state))
                stopping = terminal.checkpoint(lambda value: '· stopping' in value['footer']
                                      and 'Stopping after this run' in value['run_status'])
                self.assertEqual(stopping['sections'], ['Running · 1', 'Needs attention · 3',
                                                        'Eligible · 5 · not claimed while stopping'])
                self.assertTrue(stopping['work_lines']['assignment:owned-run'].startswith('■ #114'))
                self.assertTrue(stopping['work_lines']['assignment:owned-run'].endswith('stopping'))
                from ub_agents.view_ui import pane_line
                self.assertEqual(stopping['work_details']['assignment:owned-run'].rstrip(),
                                 pane_line('  implementer · this launcher · finishing run',
                                           stopping['work_width']).plain)
                self.assertNotIn('attempt', stopping['work_details']['assignment:owned-run'])
                self.assertEqual(stopping['run_status'].splitlines()[1],
                                 '■ Stopping after this run (SIGTERM) · no new claims')
                for key in ('plan:12', 'plan:20', 'plan:21'):
                    self.assertTrue(stopping['work_lines'][key].endswith('held'))
                for item, status in ((27, 'backoff'), (28, 'waiting')):
                    self.assertTrue(stopping['work_lines'][f'plan:{item}'].endswith(status))
                terminal.send(b's\r')
                other = terminal.checkpoint(lambda value: value['selected'] == 'plan:21'
                                   and 'worker recover · no outcome reported' in value['run_status'])
                self.assertNotIn('Stopping after this run', other['run_status'])
                terminal.send(b'a\r')
                terminal.checkpoint(lambda value: value['selected'] == 'assignment:owned-run'
                           and 'Stopping after this run' in value['run_status'])
                state['published_at'] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
                path.write_text(json.dumps(state))
                self.assertIn('· stale', terminal.checkpoint(lambda value: '· stale' in value['footer'])['footer'])
                state['ended'] = True
                path.write_text(json.dumps(state))
                self.assertIn('· ended', terminal.checkpoint(lambda value: '· ended' in value['footer'])['footer'])
                path.write_text('{broken')
                malformed = terminal.checkpoint(lambda value: 'malformed: Expecting property' in value['footer'])
                self.assertIn('malformed: Expecting property', malformed['footer'])
                terminal.resize(80, 24)
                smaller = terminal.checkpoint(lambda value: value['terminal_size'] == [80, 24]
                                     and value['footer'].endswith('↑↓ select ⏎ open ? keys q quit'))
                self.assertNotIn('minimum', smaller['footer'])
                terminal.resize(110, 32)
                state['ended'] = False
                state['published_at'] = datetime.now(timezone.utc).isoformat()
                state['activity'] = {'state': 'running assignment'}
                path.write_text(json.dumps(state))
                terminal.checkpoint(lambda value: value['terminal_size'] == [110, 32]
                           and '· running assignment' in value['footer']
                           and 'malformed' not in value['footer'])
                terminal.send(b'?')
                help_view = terminal.checkpoint(lambda value: value['screen'] == 'KeyHelp')
                self.assertEqual(help_view['screen'], 'KeyHelp')
                for key in ('Tab', 'arrows', 'Enter', '1 / 2 / 3', 'g on Log', 'g on Issue', 'f   ', 'h   ',
                            'u   ', 'p   ', 'Page Up', 'Page Down', 'Home', 'End', 'Escape', 'q   ', 'Ctrl-C'):
                    self.assertIn(key, help_view['modal'])
                terminal.send(b'?')
                self.assertNotEqual(terminal.checkpoint(lambda value: value['screen'] != 'KeyHelp')['screen'], 'KeyHelp')
                terminal.send(b'?')
                terminal.checkpoint(lambda value: value['screen'] == 'KeyHelp')
                terminal.send(b'\x1b')
                self.assertNotEqual(terminal.checkpoint(lambda value: value['screen'] != 'KeyHelp')['screen'], 'KeyHelp')
                paused = pause_at_top()
                self.assertFalse(paused['follow'])
                self.assertTrue(paused['pill_visible'])
                self.assertIn('⏸ PAUSED', paused['pill'])
                self.assertTrue(paused['footer'].endswith('f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'))
                # More than both ingestion retention (200 entries) and renderer
                # retention (400 wrapped rows) arrive while the page is paused.
                from tests.test_view_data import event
                with log.open('ab') as stream:
                    stream.write(b''.join(event(i, size=800) for i in range(1200)))
                terminal.checkpoint(lambda value: value['entries'] - paused['entries'] > 200)
                terminal.send(b'231')
                retained = terminal.checkpoint(lambda value: value['anchor'] is not None
                                      and value['anchor'][0] == paused['anchor'][0]
                                      and abs(value['anchor'][1] - paused['anchor'][1]) <= 0.05)
                self.assertEqual(retained['starts'], paused['starts'])
                self.assertEqual(retained['anchor'][0], paused['anchor'][0])
                # Tab relayout may settle the scrollbar and change wrapping;
                # the same entry and proportional reading position survive.
                self.assertAlmostEqual(retained['anchor'][1], paused['anchor'][1], delta=0.05)
                self.assertGreater(retained['entries'] - paused['entries'], 200)
                self.assertIn('new ↓', retained['pill'])
                self.assertIn('B lag', retained['pill'])
                self.assertNotIn('evicted', retained['notice'])
                terminal.send(b'p')
                raw = terminal.checkpoint()
                self.assertEqual(raw['screen'], 'RawAccess')
                for diagnostic in (str(log), 'Displayed bytes', 'Page bytes', 'evicted', 'skipped',
                                   'shortened', 'Rendered limit 400', 'entries hidden'):
                    self.assertIn(diagnostic, raw['modal'])
                terminal.send(b'\x1b')
                terminal.checkpoint()
                terminal.send(b'r\r')  # Focus the newest outcome, then real Enter.
                self.assertEqual(terminal.checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                            and value['anchor'] is not None)['selected'], 'outcome:previous-run')
                outcome_paused = pause_at_top()
                self.assertIsNotNone(outcome_paused['anchor'])
                with outcome_log.open('ab') as stream:
                    stream.write(b''.join(event(i, size=800) for i in range(600)))
                outcome_retained = terminal.checkpoint(lambda value: value['entries'] - outcome_paused['entries'] > 200)
                self.assertGreater(outcome_retained['entries'] - outcome_paused['entries'], 200)
                self.assertEqual(outcome_retained['recent_rows'], initial['recent_rows'])
                self.assertEqual(outcome_retained['starts'], outcome_paused['starts'])
                self.assertEqual(outcome_retained['anchor'], outcome_paused['anchor'])
                terminal.send(b'r\x1b[A\r')  # Up crosses to the last live row.
                upper = terminal.checkpoint(lambda value: value['selected'].startswith('plan:') and value['focus'] == 'work')
                self.assertGreater(upper['upper_scroll'], 0)
                self.assertEqual(upper['recent_bounds'], initial['recent_bounds'])
                terminal.send(b'\x1b[B\r')  # Down crosses back to the newest outcome.
                revisited = terminal.checkpoint(lambda value: value['selected'] == 'outcome:previous-run'
                                       and value['starts'] == outcome_paused['starts'] and value['anchor'] is not None
                                       and value['anchor'][0] == outcome_paused['anchor'][0]
                                       and abs(value['anchor'][1] - outcome_paused['anchor'][1]) <= 0.05)
                self.assertEqual(revisited['starts'], outcome_paused['starts'])
                self.assertEqual(revisited['anchor'][0], outcome_paused['anchor'][0])
                self.assertAlmostEqual(revisited['anchor'][1], outcome_paused['anchor'][1], delta=0.05)
                # New outcomes clip the selected row without changing its pane or paused page.
                original_outcomes = list(state['outcomes'])
                state['outcomes'] += [dict(state['outcomes'][-1], run=f'new-{n}', item=40 + n)
                                      for n in range(18)]
                path.write_text(json.dumps(state))
                clipped = terminal.checkpoint(lambda value: value['recent_rows'][:1] == ['outcome:new-17'])
                self.assertNotIn('outcome:previous-run', clipped['recent_rows'])
                self.assertEqual(clipped['selected'], 'outcome:previous-run')
                self.assertEqual(clipped['header'], revisited['header'])
                self.assertEqual(clipped['starts'], outcome_paused['starts'])
                self.assertEqual(clipped['anchor'], revisited['anchor'])
                self.assertEqual(clipped['recent_scroll'], 0)
                self.assertEqual(len(clipped['recent'].splitlines()), 3 * len(clipped['recent_rows']))
                state['outcomes'] = original_outcomes
                path.write_text(json.dumps(state))
                terminal.checkpoint(lambda value: value['recent_rows'] == initial['recent_rows'])
                terminal.send(b's\r')
                plan = terminal.checkpoint()
                state['latest_pass']['state'] = 'complete'
                state['latest_pass']['rows'] = [
                    {'item': 21, 'agent': 'worker', 'state': 'parked', 'reason': 'Approval required'}]
                path.write_text(json.dumps(state))
                moved = terminal.checkpoint(lambda value: value['group'] == 'Needs attention' and value['cursor'] == 'plan:21:worker')
                self.assertEqual(moved['group'], 'Needs attention')
                self.assertEqual(moved['selected'], 'plan:21:worker')
                self.assertEqual(moved['cursor'], moved['selected'])
                self.assertEqual(moved['focus'], plan['focus'])
                self.assertEqual(moved['sections'], ['Running · 1', 'Needs attention · 1'])
                self.assertNotIn('partial', moved['title'])
                state['latest_pass']['rows'][0]['reason'] = 'Waiting for blockers #31'
                path.write_text(json.dumps(state))
                hidden = terminal.checkpoint(lambda value: value['sections'] == ['Running · 1']
                                    and value['state'] == 'earlier observation')
                self.assertNotIn(moved['selected'], hidden['nodes'])
                self.assertEqual(hidden['selected'], moved['selected'])
                self.assertEqual(hidden['focus'], plan['focus'])
                state['latest_pass']['rows'] = []
                path.write_text(json.dumps(state))
                self.assertEqual(terminal.checkpoint(lambda value: value['state'] == 'earlier observation')['state'],
                                 'earlier observation')
                terminal.send(b'a\r')
                restored = terminal.checkpoint(lambda value: value['starts'] == paused['starts'] and value['anchor'] is not None
                                      and value['anchor'][0] == paused['anchor'][0]
                                      and abs(value['anchor'][1] - paused['anchor'][1]) <= 0.05)
                self.assertEqual(restored['starts'], paused['starts'])
                self.assertEqual(restored['anchor'][0], paused['anchor'][0])
                self.assertAlmostEqual(restored['anchor'][1], paused['anchor'][1], delta=0.05)
                terminal.send(b'h')
                older = terminal.checkpoint(lambda value: value['starts'] and value['starts'][0] < paused['starts'][0])
                self.assertLess(older['starts'][0], paused['starts'][0])
                terminal.send(b'p')
                raw = terminal.checkpoint()['raw_details']
                self.assertIn('bytes ', raw)
                self.assertIn('evicted ', raw)
                self.assertIn('skipped ', raw)
                self.assertIn('shortened ', raw)
                self.assertIn('Rendered limit 400:', raw)
                self.assertIn('process.log', raw)
                self.assertIn(b'Rendered limit 400:', transcript)
                terminal.send(b'\x1b')
                terminal.checkpoint()
                terminal.send(b'u')
                raw_mode = terminal.checkpoint(lambda value: value['raw'])
                self.assertTrue(raw_mode['raw'])
                self.assertIn('RAW', raw_mode['pill'])
                terminal.resize(120, 36)
                terminal.checkpoint()
                # Replacement preserves a paused earlier generation until follow.
                replacement = log.with_suffix('.next')
                replacement.write_bytes(capture.read_bytes())
                replacement.replace(log)
                changed = terminal.checkpoint(lambda value: 'FILE CHANGED' in value['notice'])
                self.assertEqual(changed['generation'], older['generation'])
                self.assertIn('FILE CHANGED', changed['notice'])
                terminal.send(b'h')
                self.assertIn('File changed', terminal.checkpoint(lambda value: 'File changed' in value['notice'])['notice'])
                terminal.send(b'f')
                resumed = terminal.checkpoint(lambda value: value['follow'] and value['generation'] > changed['generation'])
                self.assertTrue(resumed['follow'])
                self.assertFalse(resumed['pill_visible'])
                self.assertGreater(resumed['generation'], changed['generation'])
                log.write_bytes(capture.read_bytes().splitlines(keepends=True)[0])
                self.assertGreater(terminal.checkpoint(lambda value: value['generation'] > resumed['generation'])['generation'],
                                   resumed['generation'])
                # Exercise the fixed split in both real terminal sizes, including
                # an idle upper viewport and zero cached outcomes.
                terminal.send(b'r\r')
                terminal.checkpoint(lambda value: value['selected'] == 'outcome:previous-run')
                state['assignment'] = None
                state['latest_pass'] = {'state': 'complete', 'rows': []}
                path.write_text(json.dumps(state))
                empty = terminal.checkpoint(lambda value: value['sections'] == ['Running · 0'])
                self.assertLessEqual(abs(empty['upper_bounds'][1] - empty['recent_bounds'][1]), 1)
                self.assertEqual(sum(empty['upper_bounds']), empty['recent_bounds'][0])
                state['outcomes'] = []
                path.write_text(json.dumps(state))
                zero = terminal.checkpoint(lambda value: value['recent'].startswith('Recent activity · 0 today'))
                self.assertEqual(zero['recent_bounds'], empty['recent_bounds'])
                state['latest_pass']['rows'] = [
                    {'item': n, 'agent': 'worker', 'state': 'ready', 'reason': 'Trigger matched'}
                    for n in range(1, 50)]
                path.write_text(json.dumps(state))
                overflow = terminal.checkpoint(lambda value: value['sections'] == ['Running · 0', 'Eligible · 49 · showing 10'])
                self.assertEqual(len(overflow['eligible']), 10)
                self.assertEqual(overflow['recent_bounds'], empty['recent_bounds'])
                terminal.resize(110, 32)
                minimum = terminal.checkpoint(lambda value: value['terminal_size'] == [110, 32]
                                     and value['recent_bounds'] == initial['recent_bounds'])
                self.assertEqual(minimum['recent_bounds'], initial['recent_bounds'])
                state['latest_pass']['rows'] = []
                path.write_text(json.dumps(state))
                minimum_empty = terminal.checkpoint(lambda value: value['sections'] == ['Running · 0'])
                self.assertEqual(minimum_empty['recent_bounds'], initial['recent_bounds'])
                terminal.send(b'q')
                terminal.wait_exit()
