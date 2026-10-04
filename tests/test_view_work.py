from datetime import datetime, timedelta, timezone
import unittest

from ub_agents.view_data import WorkRow
from ub_agents.view_work import work_lines


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
        self.assertEqual(first.plain, '⠹ #160 Compact work rows' + ' ' * 21 + '04:12')
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

    def test_foreign_owned_blocked_attempt_limit_and_eligible(self):
        cases = [
            (self.row('Running', 'owned', kind='pr', owner={'actor': 'worker', 'host': 'build-01'}),
             '◌ ⌥160 Compact work rows', 'owned', '  implementer · @worker on build-01'),
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
            (self.row('Waiting', 'backoff'), '◷ #160 Compact work rows', 'backoff', '  implementer'),
            (self.row('Waiting', 'parked'), '◷ #160 Compact work rows', 'waiting', '  implementer'),
            (self.row('Waiting', 'waiting'), '◷ #160 Compact work rows', 'waiting', '  implementer'),
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

    def test_cell_clipping_and_missing_metadata(self):
        row = self.row('Running', 'owned', title='Wide 界 titles and long queue descriptions ' * 5,
                       owner={'actor': 'long-actor-name', 'host': 'long-host-name'})
        for width in (0, 1, 15, 32, 50):
            with self.subTest(width=width):
                first, second = work_lines(row, width)
                self.assertEqual(first.cell_len, width)
                self.assertLessEqual(second.cell_len, width)
                self.assertNotIn('\n', first.plain + second.plain)
        first, second = work_lines(row, 32)
        self.assertIn('…', first.plain)
        self.assertTrue(first.plain.endswith('owned'))
        self.assertTrue(second.plain.endswith('…'))
        row = self.row('Eligible', 'ready', agent='', title='', failures=0, max_attempts=3)
        self.assertEqual(work_lines(row, 32)[1].plain.strip(), '')
