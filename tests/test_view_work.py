from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from rich.console import Console
from rich.text import Text
from textual.geometry import Region

from ub_agents.view_data import Pane, Section, WorkRow
from ub_agents.view_work import WorkTree, work_lines
from ub_agents.view_theme import VIEW_THEME, theme_style


class WorkTreeTests(unittest.TestCase):
    """Reconcile real Tree nodes without starting a Textual app or workers."""

    def setUp(self):
        self.now = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)
        self.app = SimpleNamespace(
            narrow=False, session=SimpleNamespace(data={}),
            descriptions=SimpleNamespace(clock=lambda: self.now.timestamp()),
            idle_label=Mock(return_value=Text('    Idle · polling', style='dim')),
            theme_variables={**VIEW_THEME.to_color_system().generate(), **VIEW_THEME.variables})
        self.tree = WorkTree('Work')
        self.enterContext(patch.object(WorkTree, 'app', new_callable=PropertyMock, return_value=self.app))
        self.enterContext(patch.object(WorkTree, 'scrollable_content_region', new_callable=PropertyMock,
                                      return_value=Region(0, 0, 80, 20)))

    def row(self, item, group='Eligible', **data):
        return WorkRow(f'plan:{item}', group, item, 'worker', 'ready', '',
                       {'title': f'Item {item}', **data})

    def pane(self, *work, recent=(), hidden=(), selected=None):
        sections = []
        for name in ('Running', 'Needs attention', 'Eligible'):
            grouped = tuple(row for row in work if row.group == name)
            if grouped or name == 'Running':
                sections.append(Section(name, f'{name} · {len(grouped)}', grouped,
                                        idle=name == 'Running' and not grouped,
                                        next=grouped[0].key if name == 'Eligible' and grouped else None))
        return Pane(tuple(sections), recent, (*work, *recent, *hidden), selected, 'Work')

    def test_sections_rows_and_idle_reconcile_without_selecting_a_row(self):
        first, second = self.row(1), self.row(2)
        pane = self.pane(first, second, recent=(self.row(3, 'Recent activity'),),
                         hidden=(replace(self.row(4), hidden=True),), selected=second.key)
        self.assertIs(self.tree.populate(pane), self.tree.nodes)
        self.assertEqual(list(self.tree.nodes), [first.key, second.key])
        running, eligible = self.tree.root.children
        idle = self.tree.idle_node
        node = self.tree.nodes[second.key]
        self.assertEqual([group.label.plain for group in self.tree.root.children],
                         ['Running · 0', 'Eligible · 2'])
        self.assertEqual(idle.label.plain, '    Idle · polling')
        self.assertIsNone(self.tree.cursor_node)

        attention = self.row(5, 'Needs attention')
        eligible.collapse()
        self.tree.root.collapse()
        self.tree.populate(self.pane(attention, second))
        self.assertEqual([group.label.plain for group in self.tree.root.children],
                         ['Running · 0', 'Needs attention · 1', 'Eligible · 1'])
        self.assertIs(self.tree.groups['Running'], running)
        self.assertIs(self.tree.groups['Eligible'], eligible)
        self.assertIs(self.tree.idle_node, idle)
        self.assertIs(self.tree.nodes[second.key], node)
        self.assertNotIn(first.key, self.tree.nodes)
        self.assertTrue(eligible.is_expanded)
        self.assertTrue(self.tree.root.is_expanded)

        moved = replace(second, group='Needs attention')
        self.tree.populate(self.pane(moved, attention))
        self.assertNotIn('Eligible', self.tree.groups)
        self.assertIsNot(self.tree.nodes[second.key], node)
        self.assertEqual([child.data for child in self.tree.groups['Needs attention'].children],
                         [second.key, attention.key])
        self.assertEqual(list(node.parent.children), [])

        own = replace(self.row(6, 'Running'), key='assignment:own', run='own')
        self.tree.populate(self.pane(own))
        self.assertIsNone(self.tree.idle_node)
        self.assertEqual(list(self.tree.groups), ['Running'])
        self.assertEqual([child.data for child in running.children], [own.key])
        self.tree.populate(self.pane())
        self.assertEqual(self.tree.nodes, {})
        self.assertIsNotNone(self.tree.idle_node)
        self.assertEqual(self.tree._work_values, {})

    def test_render_cache_detects_mutation_and_ignores_invisible_changes(self):
        row = self.row(1)
        pane = self.pane(row)
        self.tree.populate(pane)
        node = self.tree.nodes[row.key]
        updates = node._updates
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates)

        row.data['process_reason'] = 'An invisible diagnostic'
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates)
        row.data['title'] = 'Changed title'
        self.tree.populate(pane)
        self.assertIs(self.tree.nodes[row.key], node)
        self.assertEqual(node._updates, updates + 1)
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates + 1)

        self.app.narrow = True
        row.data['priority'] = 'high'
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates + 1)
        self.app.narrow = False
        row.data['priority'] = 'urgent'
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates + 2)
        self.app.session.data['activity'] = {'state': 'stopping'}
        self.tree.populate(pane)
        self.assertEqual(node._updates, updates + 3)

    def test_claim_time_survives_a_report_and_is_pruned_when_row_leaves(self):
        own = replace(self.row(1, 'Running', claimed_at=self.now.isoformat()),
                      key='assignment:own', run='own')
        self.tree.populate(self.pane(own))
        self.assertEqual(self.tree.claim_times, {own.key: self.now})
        reported = replace(own, data={'history': {'runs': [
            {'agent': 'worker', 'time': (self.now + timedelta(minutes=3)).isoformat(),
             'acceptance': 'unaccepted'}]}})
        self.tree.populate(self.pane(reported))
        self.assertEqual(self.tree.claim_times, {own.key: self.now})
        self.tree.populate(self.pane())
        self.assertEqual(self.tree.claim_times, {})

    def test_idle_refresh_reuses_the_leaf_and_only_relabels_on_change(self):
        pane = self.pane()
        self.tree.populate(pane)
        idle = self.tree.idle_node
        updates = idle._updates
        self.tree.refresh_idle(pane)
        self.assertEqual(idle._updates, updates)
        self.app.idle_label.return_value = Text('    Idle · nothing eligible for this launcher', style='dim')
        self.tree.refresh_idle(pane)
        self.assertIs(self.tree.idle_node, idle)
        self.assertEqual(idle.label.plain, '    Idle · nothing eligible for this launcher')
        self.assertEqual(idle._updates, updates + 1)
        self.tree.populate(self.pane(self.row(1, 'Running')))
        self.app.idle_label.reset_mock()
        self.tree.refresh_idle(pane)
        self.app.idle_label.assert_not_called()


class WorkLineTests(unittest.TestCase):
    def test_pause_cause_names_clis_uses_first_expiry_and_leaves_other_waits_alone(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        pauses = {'claude': now + timedelta(hours=28), 'codex': now + timedelta(minutes=35)}
        for clis, label in ((['claude'], 'claude paused for 1d 4h'),
                            (['codex'], 'codex paused for 35m'),
                            (['claude', 'codex'], 'claude, codex paused for 35m')):
            row = self.row('Eligible', 'waiting', paused_clis=clis)
            # WorkRow state and its structured plan carry the same observed state.
            for width in (42, 80):
                line = work_lines(row, width, now=now, pauses=pauses)[0]
                self.assertTrue(line.plain.endswith(label), line.plain)
                self.assertEqual(line.cell_len, width)
            later = now + timedelta(minutes=1)
            if 'codex' in clis:
                self.assertTrue(work_lines(row, 80, now=later, pauses=pauses)[0].plain.endswith('34m'))
        row = self.row('Eligible', 'waiting', paused_clis=['claude', 'codex'])
        self.assertTrue(work_lines(row, 80, now=now, pauses={'claude': pauses['claude']})[0].plain.endswith('waiting'))
        for state in ('waiting', 'backoff', 'ready'):
            row = self.row('Eligible', state, reason='health check failed')
            self.assertTrue(work_lines(row, 80, now=now, pauses=pauses)[0].plain.endswith(state))

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
        data = {'agent': 'implementer', 'title': 'Compact work rows', 'kind': 'issue', 'state': state, **data}
        return WorkRow('plan:160:attention', group, 160, data['agent'], state,
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

    def test_attention_merges_agent_states_distinct_reasons_and_longest_wait(self):
        now = datetime(2026, 10, 5, 12, 36, tzinfo=timezone.utc)
        first = self.row('Needs attention', 'parked', agent='issue-reviewer',
                         stop_labels=['needs-human'], attention_reason='Sonner conflicts',
                         waiting_since='2026-10-05T12:12:00Z')
        second = self.row('Needs attention', 'blocked', attention_reason='Sonner conflicts',
                          waiting_since='2026-10-05T11:36:00Z')
        row = replace(first, attention_rows=(first, second))
        line, detail = work_lines(row, 110, now=now)
        self.assertTrue(line.plain.startswith('? #160'))
        self.assertTrue(line.plain.endswith('1h'))
        self.assertEqual(detail.plain, '  issue-reviewer needs-human, implementer blocked · Sonner conflicts')
        second = replace(second, data={**second.data, 'attention_reason': 'CI failed'})
        row = replace(first, attention_rows=(first, second))
        self.assertEqual(work_lines(row, 110, now=now)[1].plain,
                         '  issue-reviewer needs-human, implementer blocked · Sonner conflicts, CI failed')
        second = replace(second, data={**second.data, 'waiting_since': 'invalid'})
        row = replace(first, attention_rows=(first, second))
        self.assertTrue(work_lines(row, 110, now=now)[0].plain.endswith('24m'))
        for width in (0, 1, 15, 40):
            line, detail = work_lines(row, width, now=now)
            self.assertEqual(line.cell_len, width)
            self.assertLessEqual(detail.cell_len, width)

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
