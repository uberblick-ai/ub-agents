from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import unittest

from rich.console import Console

from ub_agents.view_data import WorkRow
from ub_agents.view_work import work_lines
from ub_agents.view_theme import theme_style


class WorkLineTests(unittest.TestCase):
    def test_priority_words_colors_and_missing_priority(self):
        expected = {'urgent': '#e0524a', 'high': '#c98a86', 'low': '#86a891'}
        for priority in ('urgent', 'high', 'medium', 'low', 'custom', None):
            with self.subTest(priority=priority):
                row = self.row('Eligible', 'ready', priority=priority)
                line = work_lines(row, 80)[1]
                self.assertEqual(line.plain, '  implementer' + (f' · {priority}' if priority else ''))
                if priority in expected:
                    console = Console()
                    self.assertEqual(line.get_style_at_offset(console, len(line.plain) - 1).color,
                                     theme_style(None, 'view-priority-' + priority).color)
                    self.assertEqual(line.get_style_at_offset(console, len(line.plain) - 1).color.name,
                                     expected[priority])
                stream = io.StringIO()
                Console(file=stream, force_terminal=True, no_color=True).print(line)
                self.assertNotIn('38;2', stream.getvalue())
        plans = ({'agent': 'reviewer', 'failures': 1, 'max_attempts': 3}, {'agent': 'integrator'})
        row = replace(self.row('Eligible', 'ready', priority='high'), eligible_plans=plans)
        self.assertEqual(work_lines(row, 80)[1].plain, '  reviewer 1/3 failures, integrator · high')

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
        self.assertEqual(work_lines(row, 50, stopping=True)[1].plain,
                         '  implementer · this launcher · finishing run')
        self.assertEqual(work_lines(replace(row, data={**row.data, 'attempt': None}), 50,
                                    stopping=True)[1].plain,
                         '  implementer · this launcher · finishing run')
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
             '! #160 Compact work rows', '', '  implementer · blocked'),
            (self.row('Needs attention', 'blocked', failures=3, max_attempts=3,
                      reason='Attempt limit exhausted; inspect failures'),
             '✗ #160 Compact work rows', '', '  implementer · failed 3/3'),
            (self.row('Eligible', 'ready', failures=0, max_attempts=3),
             '● #160 Compact work rows', 'ready', '  implementer'),
            (self.row('Eligible', 'recover', failures=1, max_attempts=3),
             '● #160 Compact work rows', 'recover', '  implementer · 1/3 failures'),
            (self.row('Needs attention', 'parked'), '? #160 Compact work rows', '', '  implementer · parked'),
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

    def test_merged_agents_keep_the_first_status_and_individual_failure_counts(self):
        for state, glyph in (('ready', '●'), ('recover', '●'), ('backoff', '◷'), ('waiting', '◷')):
            with self.subTest(state=state):
                row = self.row('Eligible', state, agent='reviewer', failures=1, max_attempts=3)
                row = replace(row, eligible_plans=(row.data,
                              {'agent': 'integrator', 'failures': 2, 'max_attempts': 5},
                              {'agent': 'worker', 'failures': 0, 'max_attempts': 3}))
                first, detail = work_lines(row, 80)
                self.assertTrue(first.plain.startswith(f'{glyph} #160'))
                self.assertTrue(first.plain.endswith(state))
                self.assertEqual(detail.plain, '  reviewer 1/3 failures, integrator 2/5 failures, worker')
                expected = 'next' if state in {'ready', 'recover'} else state
                self.assertTrue(work_lines(row, 80, next_row=True)[0].plain.endswith(expected))
                expected = 'held' if state in {'ready', 'recover'} else state
                self.assertTrue(work_lines(row, 80, stopping=True)[0].plain.endswith(expected))
                for width in (0, 1, 15, 40):
                    first, detail = work_lines(row, width)
                    self.assertEqual(first.cell_len, width)
                    self.assertLessEqual(detail.cell_len, width)

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

    def test_stopping_holds_ready_recovery_but_keeps_delays_and_attention(self):
        for state in ('ready', 'recover'):
            for next_row in (False, True):
                with self.subTest(state=state, next_row=next_row):
                    row = self.row('Eligible', state)
                    first, detail = work_lines(row, 44, stopping=True, next_row=next_row)
                    self.assertTrue(first.plain.startswith('● #160'))
                    self.assertTrue(first.plain.endswith('held'))
                    self.assertEqual(detail, work_lines(row, 44)[1])
        for group, state in (('Eligible', 'backoff'), ('Eligible', 'waiting'),
                             ('Needs attention', 'blocked'), ('Needs attention', 'parked'),
                             ('Eligible', 'earlier observation')):
            with self.subTest(group=group, state=state):
                row = self.row(group, state)
                self.assertEqual(work_lines(row, 44, stopping=True, next_row=True),
                                 work_lines(row, 44, next_row=True))

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
