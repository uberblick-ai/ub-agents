from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from rich.console import Console

from ub_agents.view_data import WorkRow
from ub_agents.view_work import work_lines
from ub_agents.view_theme import theme_style


class WorkLineTests(unittest.TestCase):
    def row(self, group, state, **data):
        data = {'agent': 'implementer', 'title': 'Compact work rows', 'kind': 'issue', **data}
        return WorkRow('plan:160:implementer', group, 160, data['agent'], state,
                       data.pop('reason', ''), data)

    def test_assignment_claim_elapsed_stopping_and_details(self):
        now = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
        stamp = (now - timedelta(minutes=4, seconds=12)).isoformat()
        row = WorkRow('assignment:own', 'Running', 160, 'implementer', 'running', '',
                      {'kind': 'issue', 'title': 'Compact work rows', 'agent': 'implementer',
                       'attempt': 1, 'lease_expires': 'expiry', 'history': {'runs': [
                           {'agent': 'implementer', 'expires': 'expiry', 'time': stamp},
                           {'agent': 'reviewer', 'time': now.isoformat()}]}}, run='own')
        first, second = work_lines(row, 50, now=now)
        self.assertEqual(first.plain, '⠋ #160 Compact work rows' + ' ' * 21 + '04:12')
        self.assertEqual(second.plain, '  implementer · this launcher · attempt 1')
        self.assertTrue(work_lines(row, 50, now=now + timedelta(hours=1))[0].plain.endswith('1:04:12'))
        self.assertTrue(work_lines(row, 50, stopping=True)[0].plain.startswith('■ #160'))
        self.assertTrue(work_lines(row, 50, stopping=True)[0].plain.endswith('stopping'))
        for data in ({}, {'history': {'runs': []}},
                     {'history': {'runs': [{'agent': 'implementer', 'time': 'invalid'}]}},
                     {'history': {'runs': [{'agent': 'implementer', 'time': '2026-10-04T20:00:00'}]}}):
            claiming = WorkRow('assignment:own', 'Running', 160, 'implementer', 'starting', '', data, run='own')
            self.assertTrue(work_lines(claiming, 50, now=now)[0].plain.endswith('claiming'))
        before = WorkRow('assignment:claiming', 'Running', 160, 'implementer', 'claiming', '', row.data)
        self.assertTrue(work_lines(before, 50, now=now)[0].plain.endswith('claiming'))
        # A report timestamp cannot substitute for an unavailable claim time.
        reported = WorkRow('assignment:own', 'Running', 160, 'implementer', 'running', '',
                           {'history': {'runs': [{'agent': 'implementer', 'time': now.isoformat(),
                                                 'acceptance': 'unaccepted'}]}}, run='own')
        self.assertTrue(work_lines(reported, 50, now=now)[0].plain.endswith('claiming'))

    def test_assignment_spinner_cycles_in_tenths_and_other_glyphs_are_static(self):
        now = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
        own = WorkRow('assignment:own', 'Running', 160, 'implementer', 'running', '',
                      {'title': 'Compact work rows', 'agent': 'implementer'}, run='own')
        static = [self.row('Eligible', 'ready'), self.row('Eligible', 'backoff'),
                  self.row('Eligible', 'waiting'), self.row('Needs attention', 'parked'),
                  self.row('Needs attention', 'blocked'),
                  self.row('Needs attention', 'blocked', failures=3, max_attempts=3,
                           reason='Attempt limit exhausted'),
                  self.row('Eligible', 'earlier observation'),
                  replace(own, state='earlier observation')]
        baseline = work_lines(own, 50, now=now, claimed_at=now - timedelta(seconds=12))
        for tick, glyph in enumerate('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠋'):
            with self.subTest(tick=tick):
                later = now + timedelta(milliseconds=100 * tick)
                first, second = work_lines(own, 50, now=later, claimed_at=now - timedelta(seconds=12))
                self.assertEqual(first.plain[0], glyph)
                self.assertEqual(first.plain[1:-5], baseline[0].plain[1:-5])
                self.assertTrue(first.plain.endswith('00:13' if tick == 10 else '00:12'))
                self.assertEqual(second.plain, baseline[1].plain)
                for row in static:
                    self.assertEqual(work_lines(row, 50, now=later), work_lines(row, 50, now=now))
                self.assertEqual(work_lines(own, 50, now=later, stopping=True),
                                 work_lines(own, 50, now=now, stopping=True))

    def test_blocked_attempt_limit_and_eligible(self):
        cases = [
            (self.row('Eligible', 'ready', kind='pr'),
             '● ⌥160 Compact work rows', 'ready', '  implementer'),
            (self.row('Needs attention', 'blocked', failures=3, max_attempts=3, reason='Last run blocked'),
             '! #160 Compact work rows', 'blocked', '  implementer · 3/3 failures'),
            (self.row('Needs attention', 'blocked', failures=3, max_attempts=3,
                      reason='Attempt limit exhausted; inspect failures'),
             '✗ #160 Compact work rows', 'failed 3/3', '  implementer · 3/3 failures'),
            (self.row('Eligible', 'ready', failures=0, max_attempts=3),
             '● #160 Compact work rows', 'ready', '  implementer'),
            (self.row('Eligible', 'recover', failures=1, max_attempts=3),
             '● #160 Compact work rows', 'recover', '  implementer · 1/3 failures'),
            (self.row('Needs attention', 'parked'), '? #160 Compact work rows', 'parked', '  implementer'),
            (self.row('Eligible', 'backoff'), '◷ #160 Compact work rows', 'backoff', '  implementer'),
            (self.row('Eligible', 'waiting'), '◷ #160 Compact work rows', 'waiting', '  implementer'),
            (self.row('Eligible', 'earlier observation'), '○ #160 Compact work rows',
             'earlier observation', '  implementer'),
        ]
        for row, prefix, state, detail in cases:
            with self.subTest(group=row.group, state=row.state, detail=detail):
                first, second = work_lines(row, 50)
                self.assertTrue(first.plain.startswith(prefix))
                self.assertTrue(first.plain.endswith(state))
                self.assertEqual(first.cell_len, 50)
                self.assertEqual(second.plain, detail)
        self.assertTrue(work_lines(cases[3][0], 50, next_row=True)[0].plain.endswith('next'))
        self.assertTrue(work_lines(cases[4][0], 50, next_row=True)[0].plain.endswith('next'))
        for state in ('backoff', 'waiting'):
            self.assertTrue(work_lines(self.row('Eligible', state), 50, next_row=True)[0].plain.endswith(state))

    def test_cell_clipping_and_missing_metadata(self):
        row = self.row('Eligible', 'ready', title='Wide 界 titles and long queue descriptions ' * 5,
                       agent='long-agent-name' * 5)
        for width in (0, 1, 15, 32, 50):
            with self.subTest(width=width):
                first, second = work_lines(row, width)
                self.assertEqual(first.cell_len, width)
                self.assertLessEqual(second.cell_len, width)
                self.assertNotIn('\n', first.plain + second.plain)
        first, second = work_lines(row, 32)
        self.assertIn('…', first.plain)
        self.assertTrue(first.plain.endswith('ready'))
        self.assertTrue(second.plain.endswith('…'))
        row = self.row('Eligible', 'ready', agent='', title='', failures=0, max_attempts=3)
        self.assertEqual(work_lines(row, 32)[1].plain.strip(), '')

    def test_pr_marker_accent_is_bounded_and_does_not_restyle_title(self):
        row = self.row('Eligible', 'ready', kind='pr', title='Verbatim ⌥99 #98')
        accent = theme_style(None, 'view-accent').color
        console = Console()
        for width in (0, 1, 6, 15, 50):
            with self.subTest(width=width):
                first, _ = work_lines(row, width)
                if first.plain[2:3] == '⌥':
                    self.assertEqual(first.get_style_at_offset(console, 2).color, accent)
                    self.assertNotEqual(first.get_style_at_offset(console, 3).color, accent)
                self.assertTrue(all(first.plain[span.start:span.end] == '⌥' for span in first.spans))
        first, _ = work_lines(row, 50)
        self.assertIn('Verbatim ⌥99 #98', first.plain)
        self.assertNotEqual(first.get_style_at_offset(console, first.plain.index('⌥99')).color, accent)
