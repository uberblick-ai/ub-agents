from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from rich.console import Console

from ub_agents.coordination import Coordinator, Plan
from ub_agents.observations import MAX_BYTES, MAX_OUTCOMES, MAX_TEXT, Observations
from ub_agents.records import iso
from ub_agents.run_history import merge_record
from ub_agents.view_data import Session, work_rows
from ub_agents.view_runs import relative_time, run_host, run_status, runs_view
from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue, pr


def claim(run='run', created=1000, **fields):
    return {'kind': 'lease', 'assignment': 1, 'agent': 'implementer', 'run': run,
            'created': iso(created), 'expires': iso(created + 1800), 'state': 'running',
            'host': 'build-01.tail9c.ts.net', **fields}


def outcome(run='run', created=1100, **fields):
    return {'kind': 'outcome', 'assignment': 1, 'agent': 'implementer', 'run': run,
            'created': iso(created), 'status': 'success', 'outcome': 'handed-off',
            'summary': 'Ready for review', 'accepted': True, 'transition_complete': True,
            'handoff': 2, **fields}


class RunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = MemoryPublisher()
        self.observer = Observations(config(self.root), 'operator', None, self.memory)
        self.observer.begin_pass()
        self.now = datetime(2026, 10, 4, 12, tzinfo=timezone(timedelta(hours=2)))

    def plan(self, item=None, history=(), filing=None):
        plan = Plan(item or issue(), agent(self.root, name='implementer'), None, 'ready', 'Ready', 1, history=tuple(history))
        self.observer.plan(plan, filing)
        return plan

    def display(self, item=1, width=110):
        session = Session(self.root / 'session.json', self.memory.snapshots[-1])
        row = next(row for row in work_rows(session, self.root) if row.item == item)
        view = runs_view(row, session, now=self.now)
        stream = io.StringIO()
        Console(file=stream, width=width, color_system=None).print(view)
        return stream.getvalue(), view

    def test_relative_time_steps_and_invalid_or_future_times(self):
        for age, expected in ((0, 'just now'), (59, 'just now'), (60, '1 min ago'),
                              (3599, '59 min ago'), (3600, '1 h ago'), (86399, '23 h ago'),
                              (86400, 'yesterday'), (2 * 86400, '2 days ago'),
                              (6 * 86400, '6 days ago'), (7 * 86400, '2026-09-27')):
            with self.subTest(age=age):
                stamp = (self.now - timedelta(seconds=age)).astimezone(timezone.utc).isoformat()
                self.assertEqual(relative_time(stamp, self.now), expected)
        self.assertEqual(relative_time((self.now + timedelta(days=1)).isoformat(), self.now), 'just now')
        for stamp in (None, 'bad', '2026-10-04T00:00:00'):
            self.assertEqual(relative_time(stamp, self.now), 'unknown')

    def test_relative_days_and_date_use_the_viewers_timezone(self):
        now = datetime(2026, 10, 4, 1, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(relative_time('2026-10-02T21:30:00Z', now), '2 days ago')
        self.assertEqual(relative_time('2026-09-26T23:30:00Z', now), '2026-09-27')

    def test_local_date_applies_the_recorded_dates_daylight_saving_offset(self):
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, {'TZ': 'America/New_York'}):
            time.tzset()
            with patch('ub_agents.view_runs.datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 3, 1, tzinfo=timezone(timedelta(hours=-5)))
                self.assertEqual(relative_time('2026-10-27T04:30:00Z'), '2026-10-27')

    def test_host_domain_shortening_fixed_slot_and_local_machine(self):
        for host, expected in (('build-01.tail9c.ts.net', 'build-01'),
                               ('a-very-long-build-host.domain', 'a-very-long-b…'),
                               (None, 'unknown')):
            with self.subTest(host=host):
                rendered = run_host(host, 'local-host')
                self.assertEqual(rendered.plain, expected)
                self.assertEqual(rendered.style, 'dim')
                self.assertLessEqual(rendered.cell_len, 14)
        self.assertEqual(run_host('LOCAL-HOST', 'local-host').plain, 'this machine')
        self.assertFalse(run_host('local-host', 'local-host').style)

    def test_filing_row_header_and_unknown_filing_empty_state(self):
        filed = replace(issue(), author='bk-one', created_at='2026-10-02T10:00:00Z')
        self.plan(filed)
        value, view = self.display()
        self.assertIn('#1 Requirements', value)
        self.assertIn('filed by bk-one · 0 runs', value)
        self.assertIn('2 days ago', value)
        self.assertIn('GitHub', value)
        self.assertIn('filed', value)
        self.assertEqual(list(view.renderables)[0].style, 'bold')
        self.assertEqual(list(view.renderables)[1].style, 'dim')
        for item, filing, closing in ((pr(), filed, True), (pr(), None, True),
                                      (pr(body='No closing reference'), filed, False),
                                      (issue(), None, False)):
            # A PR without a closing issue must not use its own filing data.
            self.plan(item, filing=filing)
            value, _ = self.display(item.number)
            self.assertEqual('closes #1' in value, closing)
            self.assertEqual('filed by' in value, bool(filing and closing))
            if not filing or not closing:
                self.assertIn('No item history cached.', value)

    def test_claim_and_handoff_copies_deduplicate_and_prefer_claim_host(self):
        source = outcome(host='outcome-host', id=2)
        copy = source | {'id': 3, 'actor': 'other-launcher'}
        self.plan(history=(claim(id=1), source, copy))
        history = self.memory.snapshots[-1]['histories']['1']
        self.assertEqual(len(history['runs']), 1)
        self.assertEqual(history['runs'][0]['host'], 'build-01.tail9c.ts.net')
        self.assertEqual(history['runs'][0]['time'], source['created'])
        self.plan(pr(), (copy,))
        self.assertEqual(self.memory.snapshots[-1]['histories']['2']['runs'][0]['host'], 'outcome-host')
        self.plan(pr(), (outcome(),))
        value, _ = self.display(2)
        self.assertIn('unknown', value)

    def test_all_launchers_oldest_first_and_live_record_updates(self):
        other = claim('other', 800, actor='other-launcher', agent='reviewer', host='other-host')
        plan = self.plan(history=(claim('own', 1000), other))
        runs = self.memory.snapshots[-1]['histories']['1']['runs']
        self.assertEqual([row['run'] for row in runs], ['other', 'own'])
        self.assertEqual(runs[0]['time'], other['created'])
        self.observer.assignment(plan)
        self.observer.record(claim('own', 1000, runtime='direct'))
        self.observer.record(outcome('own', accepted=False, transition_complete=False))
        self.observer.record(claim('own', 1000, state='released', result='blocked', runtime='direct'))
        runs = self.memory.snapshots[-1]['histories']['1']['runs']
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[-1]['result'], 'blocked')
        self.assertEqual(runs[-1]['acceptance'], 'unaccepted')

    def test_omitted_count_under_run_and_shared_byte_limits(self):
        all_runs = [outcome(str(n), 1000 + n, summary='😀' * (MAX_TEXT + 20))
                    for n in range(MAX_OUTCOMES + 7)]
        self.plan(history=all_runs)
        state = self.memory.snapshots[-1]
        history = state['histories']['1']
        self.assertEqual(len(history['runs']) + history['omitted_runs'], len(all_runs))
        self.assertGreater(history['omitted_runs'], 7)  # UTF-8 bytes also require trimming.
        self.assertTrue(history['runs'])
        self.assertEqual(history['runs'][-1]['run'], str(len(all_runs) - 1))
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode()), MAX_BYTES)
        value, _ = self.display()
        self.assertIn(f'{len(all_runs)} runs', value)
        self.assertIn(f'{history["omitted_runs"]} earlier runs omitted.', value)

    def test_result_colors_spinner_summary_shortening_acceptance_and_blockers(self):
        filed = replace(issue(labels=('needs-human',)), author='bk-one')
        stamp = self.now.timestamp()
        self.plan(filed, (outcome(created=stamp - 600, handoff=None),
                          claim('failed', stamp - 500, state='released', result='retry'),
                          claim('abandoned', stamp - 400, expires=iso(stamp - 1)),
                          claim('live', stamp - 300, summary='x' * 200)))
        value, view = self.display(width=72)
        self.assertIn('finalized', value)
        self.assertIn('BLOCKED:', value)
        self.assertIn('needs-human', value)
        self.assertIn('…', value)
        table = list(view.renderables)[3]
        self.assertEqual(table.columns[1]._cells[0].style, 'green')  # Filing.
        self.assertEqual(table.columns[1]._cells[1].style, 'green')
        self.assertEqual(table.columns[1]._cells[2].style, 'red')
        self.assertEqual(table.columns[1]._cells[3].style, 'red')
        self.assertIn(table.columns[1]._cells[4].plain, '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
        self.assertEqual(table.columns[3].width, 14)

    def test_report_and_handoff_copy_keep_the_source_launcher_host(self):
        github = FakeGitHub(issue(), pr())
        co = Coordinator(github, 'operator', lambda: 1000)
        role = agent(self.root)
        lease = co.claim(co.plan(github.item(1), role, ()))
        co.update(lease, state='running', started=True, host='source-host')
        reported = co.report(lease, 'success', 'Ready', handoff=2, outcome='done')
        self.assertEqual(reported['host'], 'source-host')
        co.accept(lease, reported)
        self.assertEqual(co.history(2)[0]['host'], 'source-host')
        # Unknown hosts remain backward compatible with old outcomes.
        lease.pop('host')
        rows = []
        merge_record(rows, outcome(), ())
        self.assertIsNone(rows[0]['host'])

    def test_rejected_success_and_released_or_withdrawn_claims_are_failures(self):
        for row in ({'result': 'success', 'rejection': 'Changed candidate'},
                    {'state': 'released'}, {'state': 'withdrawn'}):
            self.assertEqual(run_status(row, self.now)[0], 'failed')


if __name__ == '__main__':
    unittest.main()
