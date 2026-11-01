from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rich.console import Console
from rich.style import Style
from rich.text import Text

from ub_agents.view_data import Description, Session, WorkRow
from ub_agents.view_status import (StatusStyles, empty_log_message, footer, header, key_hints, log_mode,
                                   log_notices, pane_line, paused_pill, run_status_line)
from ub_agents.view_unblock import ActionComment

NOW = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)
STYLES = StatusStyles(Style(color='grey50'), Style(color='magenta'), Style(color='red'), Style(color='yellow'))


def reading(**values):
    return SimpleNamespace(**({'page': None, 'log': None, 'latest': None, 'follow': True,
                               'raw': False, 'notice': '', 'runtime': 'claude'} | values))


class FooterTests(unittest.TestCase):
    def setUp(self):
        self.data = {'base_version': '9.8.7', 'repository': 'example/repo', 'published_at': NOW.isoformat(),
                     'activity': {'state': 'waiting', 'until': (NOW + timedelta(seconds=30)).isoformat()}}
        self.session = Session(Path('session.json'), self.data)

    def render(self, width=160, keys='1-3 tabs g reload ? keys q quit', **values):
        return footer(self.session, width, keys, **({'now': NOW, 'monotonic_now': 100} | values))

    def test_full_footer_and_countdown_use_supplied_wall_clock(self):
        with patch('ub_agents.view_data.datetime', wraps=datetime) as clock:
            clock.now.side_effect = AssertionError('unexpected clock read')
            line = self.render(now=NOW + timedelta(seconds=4))
        self.assertTrue(line.plain.startswith('ub-agents v9.8.7 · example/repo · next poll 26s'))
        self.assertTrue(line.plain.endswith('1-3 tabs g reload ? keys q quit'))
        self.assertEqual(line.cell_len, 160)
        self.assertNotIn('stale', line.plain)
        self.assertIn('stale · next poll 0s', self.render(now=NOW + timedelta(seconds=40)).plain)

    def test_missing_session_and_snapshot_fields(self):
        line = footer(None, 80, '? keys q quit', now=NOW, monotonic_now=0)
        self.assertTrue(line.plain.startswith('ub-agents · reading session'))
        self.data.pop('repository')
        self.data.pop('base_version')
        self.assertTrue(self.render().plain.startswith('ub-agents · next poll 30s'))
        self.assertTrue(self.render(narrow=True).plain.startswith('poll 30s'))
        self.assertNotIn(' ·  · ', self.render().plain)

    def test_state_queue_and_activity_order(self):
        self.data.update(queue_agent='worker', ended=True, activity={'state': 'polling'})
        self.assertTrue(self.render().plain.startswith('ub-agents v9.8.7 · example/repo · agent worker · ended · polling'))
        self.session = replace(self.session, error='bad snapshot')
        self.assertIn('agent worker · malformed: bad snapshot · polling', self.render().plain)

    def test_notices_choose_latest_expiry_and_expire_at_boundary(self):
        values = {'copy_notice': 'copied 12 characters', 'copy_notice_until': 101,
                  'awake_notice': 'keep awake unavailable', 'awake_notice_until': 102}
        self.assertEqual(self.render(**values).plain.strip(), 'keep awake unavailable')
        self.assertEqual(self.render(**(values | {'copy_notice_until': 102})).plain.strip(), 'copied 12 characters')
        self.assertEqual(self.render(width=9, **values).plain, 'keep awa…')
        line = self.render(monotonic_now=102, **values)
        self.assertTrue(line.plain.startswith('ub-agents v9.8.7'))
        self.assertEqual(line.cell_len, 160)

    def test_pause_and_repository_tokens_survive_narrow_widths_without_mutation(self):
        self.data.update(repository='owner/' + 'long-repository-name' * 4,
                         runtime_pauses={'claude': (NOW + timedelta(hours=28)).isoformat(),
                                         'codex': (NOW + timedelta(minutes=35)).isoformat()})
        original = deepcopy(self.data)
        for width in (60, 80, 109, 110, 160, 220):
            with self.subTest(width=width):
                line = self.render(width, '? keys q quit', narrow=width < 110,
                                   launcher_attached=True, awake_enabled=True)
                for token in ('v9.8.7', 'claude', '1d 4h', 'codex', '35m', '☕'):
                    self.assertIn(token, line.plain)
                self.assertTrue(line.plain.endswith('q quit'))
                self.assertEqual(line.cell_len, width)
                self.assertEqual(line.cell_len, len(line.plain) + 1)
        self.assertEqual(self.data, original)

    def test_poll_feedback_rate_limit_refresh_and_cooldown_precedence(self):
        self.data['activity'] = {'state': 'running assignment'}
        self.data['poll_now'] = {'cooldown_until': (NOW + timedelta(seconds=5)).isoformat()}
        self.assertIn('running assignment · poll now available in 5s', self.render(launcher_attached=True).plain)
        self.assertIn('running assignment · polling', self.render(poll_feedback={'refreshing': True}).plain)
        feedback = {'refreshing': True, 'rate_limit_until': (NOW + timedelta(minutes=35)).isoformat()}
        original = feedback.copy()
        line = self.render(launcher_attached=True, poll_feedback=feedback)
        self.assertIn('running assignment · rate limited for 35m · r unavailable', line.plain)
        self.assertNotIn(' · polling', line.plain)
        self.assertNotIn('poll now available', line.plain)
        self.assertEqual(feedback, original)
        self.data['activity'] = {'state': 'waiting', 'reason': 'rate-limit reset',
                                 'until': (NOW + timedelta(minutes=35)).isoformat()}
        self.assertIn('rate limited for 35m · r unavailable', self.render(launcher_attached=True).plain)
        self.assertIn('next poll 2100s', self.render().plain)

    def test_waiting_bad_or_expired_deadlines(self):
        for until in (None, 123, '', 'bad', '2026-10-05T20:00:00', '999999999999999999999'):
            with self.subTest(until=until):
                self.data['activity']['until'] = until
                self.assertIn(' · waiting', self.render().plain)
        self.data['activity'].pop('until')
        self.assertIn(' · waiting', self.render().plain)
        self.data['activity']['until'] = '2026-10-05T19:59:59Z'
        self.assertIn('next poll 0s', self.render().plain)

    def test_awake_hint_is_first_to_drop_but_token_uses_attached_enabled_flag(self):
        line = self.render(launcher_attached=True, awake_enabled=True, awake_supported=True)
        self.assertIn('r poll now c Stay awake ? keys', line.plain)
        self.assertIn(' · ☕ · ', line.plain)
        line = self.render(80, narrow=True, launcher_attached=True, awake_enabled=True, awake_supported=True)
        self.assertNotIn('c Stay awake', line.plain)
        self.assertIn('r poll now', line.plain)
        self.assertIn('g reload', line.plain)
        self.assertNotIn('☕', self.render(awake_enabled=True).plain)

    def test_paused_keys_shorten_at_narrow_widths(self):
        keys = 'f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'
        self.assertTrue(self.render(80, keys, narrow=True).plain.endswith('f follow h older u raw PgUp/Dn ? keys q quit'))
        self.assertTrue(self.render(60, keys, narrow=True).plain.endswith('f follow h older u raw ? keys q quit'))
        keys = '1-4 tabs f follow h older u raw PgUp/PgDn scroll g load ? keys q quit'
        self.assertTrue(self.render(60, keys, narrow=True).plain.endswith('1-4 tabs g load f follow ? keys q quit'))


class StatusTextTests(unittest.TestCase):
    def setUp(self):
        self.row = WorkRow('assignment:114', 'Running', 114, 'implementer', 'running', '',
                           {'item': 114, 'kind': 'pr', 'agent': 'implementer', 'runtime': 'codex:model:high',
                            'attempt': 2, 'handoff': 200}, run='run', log=Path('process.log'))
        self.session = Session(Path('session.json'), {'assignment': {'item': 114, 'agent': 'implementer',
                                                                   'run': 'run', 'process': 'running'}})

    def render_header(self, **values):
        return header(self.row, Description(title='A title'), self.session, 100, STYLES,
                      **({'clickable': True, 'unblock': False, 'action': None, 'now': NOW.timestamp()} | values))

    def test_contextual_key_hints(self):
        cases = [
            (True, False, False, 'log', True, '? keys q quit'),
            (True, True, False, 'log', True, 'Esc back 1-3 tabs g reload ? keys q quit'),
            (False, False, False, 'issue', True, '1-3 tabs ? keys q quit'),
            (False, False, True, 'unblock', True, '1-4 tabs g load ? keys q quit'),
            (False, False, True, 'unblock', False,
             '1-4 tabs f follow h older u raw PgUp/PgDn scroll g load ? keys q quit'),
            (True, True, True, 'log', False, 'f follow h older u raw PgUp/PgDn scroll g reload ? keys q quit'),
        ]
        for narrow, item_view, unblock, tab, follow, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(key_hints(reading(follow=follow), narrow=narrow, item_view=item_view,
                                           unblock_visible=unblock, active_tab=tab), expected)

    def test_mode_width_and_selected_styles(self):
        console = Console()
        for raw in (False, True):
            for width, expected in ((20, '│ Formatted  Raw (u)'), (18, '│ Formatted  Raw u'),
                                    (16, '│ Formatted Raw u')):
                with self.subTest(raw=raw, width=width):
                    line = log_mode(reading(raw=raw), width, STYLES)
                    self.assertEqual(line.plain, expected)
                    formatted = line.get_style_at_offset(console, 2)
                    raw_style = line.get_style_at_offset(console, line.plain.index('Raw'))
                    self.assertEqual(formatted.underline, not raw)
                    self.assertEqual(raw_style.reverse, raw)
                    self.assertEqual((raw_style if raw else formatted).color, STYLES.accent.color)

    def test_paused_and_behind_pills_hide_without_content(self):
        self.assertFalse(paused_pill(reading(follow=False), 0, 0)[0])
        self.assertFalse(paused_pill(reading(page=object()), 0, 0)[0])
        visible, line = paused_pill(reading(page=object(), follow=False, raw=True), 3, 12)
        self.assertTrue(visible)
        self.assertEqual(line.plain, ' ⏸ PAUSED · 3 new ↓ · 12B lag · RAW · f follow ')
        self.assertEqual(line.style, 'reverse')
        visible, line = paused_pill(reading(log=object()), 2, 0)
        self.assertTrue(visible)
        self.assertEqual(line.plain, ' ↓ BEHIND · 2 new ↓ · f follow ')

    def test_header_identity_handoff_styles_and_click_metadata(self):
        line = self.render_header()
        self.assertEqual(line.plain, '⌥114 A title\nimplementer · codex model high · attempt 2 · ⌥200\n' + '┄' * 100)
        console = Console()
        marker = line.get_style_at_offset(console, 0)
        self.assertEqual(marker.color, STYLES.accent.color)
        self.assertTrue(marker.bold)
        self.assertEqual(marker.meta, {'@click': 'app.open_reference'})
        self.assertEqual(line.get_style_at_offset(console, 4).meta, {})
        handoff = line.get_style_at_offset(console, line.plain.index('⌥200'))
        self.assertEqual(handoff.color, STYLES.accent.color)
        self.assertTrue(handoff.dim)
        self.assertEqual(self.render_header(clickable=False).get_style_at_offset(console, 0).meta, {})

    def test_unblock_header_waiting_uses_supplied_time_and_error_style(self):
        self.row = replace(self.row, state='parked', data=self.row.data | {
            'waiting_since': (NOW - timedelta(minutes=30)).isoformat()})
        with patch('time.time', side_effect=AssertionError('unexpected clock read')):
            line = self.render_header(unblock=True, action=ActionComment())
        self.assertIn('implementer · parked · waiting 30m · since ', line.plain)
        self.assertNotIn('⌥200', line.plain)
        waiting = line.get_style_at_offset(Console(), line.plain.index('waiting 30m'))
        self.assertEqual(waiting.color, STYLES.error.color)
        self.assertTrue(waiting.dim)

    def test_header_and_pane_line_truncate_by_cells_and_preserve_input(self):
        value = Text('界界界', style='bold')
        self.assertEqual(pane_line(value, 5).plain, '界界…')
        self.assertEqual(value.plain, '界界界')
        for width in (0, 1, 5, 20):
            with self.subTest(width=width):
                line = header(None, None, None, width, STYLES, clickable=False,
                              unblock=False, action=None, now=NOW.timestamp())
                self.assertEqual(len(line.plain.split('\n')), 3)
                self.assertTrue(all(Text(part).cell_len <= width for part in line.plain.split('\n')))

    def test_pane_line_is_empty_at_nonpositive_widths(self):
        for value in ('abc', Text('abc', style='bold')):
            for width in (0, -1):
                with self.subTest(value=value, width=width):
                    line = pane_line(value, width, STYLES.muted)
                    self.assertEqual(line.plain, '')
                    self.assertEqual(line.cell_len, 0)

    def test_log_notices_preserve_order_and_only_show_for_local_logs(self):
        state = reading(page=SimpleNamespace(generation=1), latest=SimpleNamespace(generation=2),
                        log=SimpleNamespace(error='failed', pending_bytes=28), notice='Older page.', runtime='custom')
        visible, line = log_notices(self.row, state, 250, STYLES)
        self.assertTrue(visible)
        self.assertEqual(line.plain, 'FILE CHANGED; paused earlier generation; f latest. · Read error: failed · '
                         'Unfinished: 28B (raw preview) · Older page. · custom: plain/raw fallback')
        style = line.get_style_at_offset(Console(), 0)
        self.assertEqual(style.color, STYLES.warning.color)
        self.assertTrue(style.bold)
        self.assertFalse(log_notices(replace(self.row, log=None), state, 250, STYLES)[0])
        self.assertFalse(log_notices(None, state, 250, STYLES)[0])
        state.latest = state.page
        self.assertTrue(log_notices(self.row, state, 250, STYLES)[1].plain.startswith('File replaced or changed generation.'))
        self.assertEqual(log_notices(self.row, state, 10, STYLES)[1].cell_len, 10)
        self.assertFalse(log_notices(self.row, reading(), 100, STYLES)[0])

    def test_empty_log_messages_distinguish_owner_and_local_output(self):
        self.assertEqual(empty_log_message(None), 'No local log cached for this row.')
        self.assertEqual(empty_log_message(replace(self.row, log=None)), 'No local log cached for this row.')
        self.assertEqual(empty_log_message(self.row), 'No log output yet.')
        self.assertEqual(empty_log_message(replace(self.row, reason='Owned elsewhere', data={'owner': {'run': 'other'}})),
                         'Owned elsewhere')

    def test_run_spinner_uses_supplied_monotonic_time_and_retains_earlier_count(self):
        self.session.data['outcomes'] = [{'item': 114, 'run': 'earlier'}, {'item': 114, 'run': 'other'}]
        lines = [run_status_line(self.row, self.session, 80, STYLES, seconds) for seconds in (0, 0.1)]
        first, second = [line.plain.split('\n')[1] for line in lines]
        self.assertTrue(first.startswith('⠋ implementer running · no outcome reported'))
        self.assertTrue(first.endswith('2 earlier runs'))
        self.assertEqual(Text(first).cell_len, 80)
        self.assertEqual(second[0], '⠙')
        self.assertEqual(first[1:], second[1:])
        self.assertEqual(lines[0].get_style_at_offset(Console(), 0).color, STYLES.muted.color)
        line = run_status_line(self.row, self.session, 30, STYLES, 0)
        self.assertEqual(line.plain.split('\n')[1], '⠋ implementer … 2 earlier runs')

    def test_run_status_hides_left_column_when_earlier_count_fills_width(self):
        self.session.data['outcomes'] = [{'item': 114, 'run': 'earlier'}, {'item': 114, 'run': 'other'}]
        for width, expected in ((0, ''), (2, ' …'), (14, ' 2 earlier ru…'), (15, ' 2 earlier runs')):
            with self.subTest(width=width):
                line = run_status_line(self.row, self.session, width, STYLES, 0)
                parts = line.plain.split('\n')
                self.assertEqual(parts[1], expected)
                self.assertTrue(all(Text(part).cell_len <= width for part in parts))

    def test_idle_and_stopping_run_status_do_not_spin(self):
        line = run_status_line(None, Session(Path('session.json'), {}), 80, STYLES, 0.1)
        self.assertEqual(line.plain.split('\n')[1], '○ Idle · waiting for the next poll')
        self.session.data['activity'] = {'state': 'stopping'}
        line = run_status_line(self.row, self.session, 100, STYLES, 0.1)
        self.assertEqual(line.plain.split('\n')[1], '■ Stopping after this run (SIGTERM) · no new claims')

    def test_status_module_imports_without_textual_or_view_ui(self):
        script = """
import sys
from ub_agents import view_status
assert not any(name == 'textual' or name.startswith('textual.') for name in sys.modules)
assert 'ub_agents.view_ui' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
