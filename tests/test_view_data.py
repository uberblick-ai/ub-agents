import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.view import main
from ub_agents.view_data import (Description, Session, choose_session, item_context, load_session,
                                 item_header, outcome_text, outcomes_today, read_json, run_status, text, work_pane, work_rows)
from ub_agents.view_logs import FileChanged, PAGE_BYTES, ViewReader
from ub_agents.view_worker import LocalWorker, Request
from ub_agents.config import Queue
from ub_agents.eligibility import AgentMatches, check_start
from tests.support import agent, issue


def publish_snapshot(path, state):
    # Live view readers must see complete snapshots, as with the launcher writer.
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def fixture(root, *, runtime='claude:synthetic-model:high', count=0):
    run = root / '.ub-agents' / 'runs' / 'owned-run'
    run.mkdir(parents=True, exist_ok=True)
    log = run / 'process.log'
    log.write_bytes(b'')
    (run / 'context.json').write_text(json.dumps({'title': 'Cached title', 'body': 'Cached description'}))
    directory = root / '.ub-agents' / 'sessions'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'launcher.json'
    state = {'version': 1, 'session': 'launcher', 'actor': 'synthetic', 'repository': 'example/repo',
             'published_at': datetime.now(timezone.utc).isoformat(), 'ended': False,
             'activity': {'state': 'running assignment'},
             'assignment': {'item': 114, 'agent': 'implementer', 'run': 'owned-run', 'runtime': runtime,
                            'process': 'running', 'process_reason': 'Supervisor observed running'},
             'latest_pass': {'state': 'partial', 'rows': [
                 {'item': 114, 'agent': 'implementer', 'state': 'running', 'reason': 'selected',
                  'description': {'available': False}},
                 {'item': 12, 'agent': 'reviewer', 'state': 'ready', 'reason': 'Trigger matched'},
                 {'item': 13, 'agent': 'reviewer', 'state': 'owned', 'reason': 'occupied',
                  'owner': {'actor': 'other-launcher', 'host': 'other-host', 'run': 'foreign-run'},
                  'process_log': str(log)}]},
             'outcomes': [{'item': 10, 'agent': 'preparer', 'run': 'previous-run', 'completed': True,
                           'runtime': 'claude:synthetic-model:high',
                           'acceptance': 'finalized', 'result': 'prepared', 'summary': 'Waiting for decision',
                           'human_blocker': ['needs-human'], 'time': '2026-10-04T00:00:00Z'}]}
    state['histories'] = {
        '114': {'item': 114, 'kind': 'issue', 'title': 'Cached title', 'omitted_runs': 0,
                'filing': {'author': 'bk-one', 'time': '2026-10-02T00:00:00Z'},
                'runs': [{'agent': 'preparer', 'run': 'filed-run', 'result': 'success',
                          'outcome': 'prepared', 'summary': 'Waiting for decision',
                          'acceptance': 'finalized', 'human_blocker': ['needs-human'],
                          'host': 'build-01.tail9c.ts.net', 'time': '2026-10-03T00:00:00Z'},
                         {'agent': 'implementer', 'run': 'owned-run', 'state': 'running',
                          'host': 'local-test-host', 'time': state['published_at']}]},
        '10': {'item': 10, 'kind': 'issue', 'title': 'Earlier item', 'omitted_runs': 0,
               'filing': None, 'runs': [dict(state['outcomes'][0], result='success', outcome='prepared')]},
        '12': {'item': 12, 'kind': 'pr', 'title': 'Foreign candidate', 'closes': 11,
               'omitted_runs': 3, 'filing': None,
               'runs': [{'agent': 'reviewer', 'run': 'foreign-run', 'state': 'running',
                         'host': 'other-host', 'time': state['published_at']}]},
    }
    publish_snapshot(path, state)
    if count:
        log.write_bytes(b''.join(event(i) for i in range(count)))
    return path, log, state


def event(i, size=400):
    return (json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'text', 'text': f'event {i:05d} ' + 'x' * size}]}}) + '\n').encode()


class WorkPaneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('unused')
        self.data = {'assignment': {'item': 114, 'agent': 'implementer', 'run': 'owned-run', 'process': 'running'},
                     'latest_pass': {'state': 'partial', 'rows': [
                         {'item': 114, 'agent': 'implementer', 'state': 'ready'},
                         {'item': 12, 'agent': 'reviewer', 'state': 'ready', 'reason': 'Trigger matched'}]},
                     'outcomes': [{'item': 10, 'agent': 'preparer', 'run': 'previous-run', 'result': 'prepared'}]}

    def pane(self, previous=None, selected=None, chosen=False):
        return work_pane(Session(Path('launcher.json'), self.data), self.root, previous, selected, chosen)

    def test_sections_counts_omitted_plans_title_stopping_and_idle(self):
        self.data['latest_pass']['rows'].extend([
            {'item': 20, 'agent': 'worker', 'state': 'ready'},
            {'item': 21, 'agent': 'worker', 'state': 'blocked'},
            {'item': 22, 'agent': 'worker', 'state': 'waiting'},
            {'item': 23, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for blockers #31'},
            {'item': 24, 'agent': 'worker', 'state': 'parked', 'reason': 'Waiting for active milestone #10'},
            {'item': 25, 'agent': 'worker', 'state': 'backoff'},
            {'item': 26, 'agent': 'worker', 'state': 'owned'}])
        self.data['omitted'] = {'plans': 3}
        pane = self.pane()
        self.assertEqual([section.name for section in pane.sections], ['Running', 'Needs attention', 'Eligible'])
        self.assertEqual([section.label for section in pane.sections],
                         ['Running · 1', 'Needs attention · 1', 'Eligible · 4'])
        self.assertEqual([row.key for row in pane.sections[-1].rows], ['plan:12', 'plan:20', 'plan:22', 'plan:25'])
        self.assertEqual([row.item for row in pane.rows], [114, 21, 12, 20, 22, 25, 10])
        self.assertEqual(pane.title, 'Work · pass partial · omitted 3')
        self.assertFalse(pane.sections[0].idle)
        self.data['activity'] = {'state': 'stopping'}
        self.assertEqual(self.pane().sections[-1].label, 'Eligible · 4 · not claimed while stopping')
        self.data['assignment'] = None
        self.data['latest_pass'] = {'state': 'complete', 'rows': []}
        self.data['outcomes'] = []
        self.data.pop('omitted')
        pane = self.pane()
        self.assertEqual([section.label for section in pane.sections], ['Running · 0'])
        self.assertTrue(pane.sections[0].idle)
        self.assertEqual(pane.sections[0].rows, ())
        self.assertEqual(pane.rows, ())
        self.assertEqual(pane.recent, ())
        self.assertIsNone(pane.selected)
        self.assertIsNone(pane.next)
        self.assertEqual(pane.title, 'Work · pass complete')
        self.data['latest_pass'] = {}
        self.assertEqual(self.pane().title, 'Work')

    def test_eligible_total_survives_byte_trim_and_older_snapshots_need_no_total(self):
        latest = self.data['latest_pass']
        latest['rows'].extend({'item': number, 'agent': 'worker', 'state': 'ready'}
                              for number in range(20, 29))
        self.assertEqual(self.pane().sections[-1].label, 'Eligible · 10')
        latest['eligible_count'] = 23
        section = self.pane().sections[-1]
        self.assertEqual(section.label, 'Eligible · 23 · showing 10')
        self.assertEqual(len(section.rows), 10)
        for invalid in (True, -1, 4, '23', None):
            with self.subTest(total=invalid):
                latest['eligible_count'] = invalid
                self.assertEqual(self.pane().sections[-1].label, 'Eligible · 10')

    def test_eligible_limit_keeps_all_details_rows_and_counts_before_capping(self):
        self.data['assignment'] = None
        order = [30, 18, 42, 15, 9, 31, 22, 13, 37, 5] + list(range(100, 113))
        self.data['latest_pass']['rows'] = [
            {'item': n, 'agent': 'worker', 'state': 'backoff' if n == 18 else 'ready'} for n in order]
        pane = self.pane(selected='plan:112', chosen=True)
        eligible = pane.sections[-1]
        self.assertEqual(eligible.label, 'Eligible · 23 · showing 10')
        self.assertEqual([row.item for row in eligible.rows], [n for n in order if n != 18][:10])
        self.assertEqual(len([row for row in pane.rows if row.group == 'Eligible']), 23)
        self.assertEqual(pane.selected, 'plan:112')
        self.assertEqual(eligible.next, 'plan:30')
        self.assertEqual(pane.next, 'plan:30')
        self.data['activity'] = {'state': 'stopping'}
        self.assertEqual(self.pane().sections[-1].label,
                         'Eligible · 23 · showing 10 · not claimed while stopping')

    def test_next_never_marks_delayed_or_retained_rows(self):
        self.data['assignment'] = None
        self.data['latest_pass']['rows'] = [
            {'item': 20, 'agent': 'worker', 'state': 'backoff'},
            {'item': 21, 'agent': 'worker', 'state': 'waiting'}]
        pane = self.pane()
        self.assertIsNone(pane.next)
        self.data['latest_pass']['rows'].append({'item': 22, 'agent': 'worker', 'state': 'recover'})
        pane = self.pane(pane, 'plan:22', True)
        self.assertEqual([row.item for row in pane.sections[-1].rows], [22, 20, 21])
        self.assertEqual(pane.next, 'plan:22')
        self.data['latest_pass']['rows'].pop()
        pane = self.pane(pane, 'plan:22', True)
        self.assertEqual(pane.selected, 'plan:22')
        self.assertIsNone(pane.next)
        self.assertEqual(pane.sections[-1].label, 'Eligible · 3')
        self.assertEqual(pane.sections[-1].rows[-1].state, 'earlier observation')

    def test_eligible_agents_merge_before_counting_and_next_selection(self):
        self.data['latest_pass']['rows'] = [
            {'item': 12, 'agent': 'reviewer', 'state': 'backoff'},
            {'item': 12, 'agent': 'integrator', 'state': 'recover'},
            {'item': 12, 'agent': 'worker', 'state': 'owned'},
            {'item': 20, 'agent': 'worker', 'state': 'ready'},
            {'item': 21, 'agent': 'reviewer', 'state': 'blocked'},
            {'item': 21, 'agent': 'integrator', 'state': 'parked'}]
        pane = self.pane()
        self.assertEqual([section.label for section in pane.sections],
                         ['Running · 1', 'Needs attention · 1', 'Eligible · 2'])
        row = pane.sections[-1].rows[0]
        self.assertEqual((row.key, row.agent, row.state), ('plan:12', 'integrator', 'recover'))
        self.assertEqual([plan['agent'] for plan in row.eligible_plans], ['integrator', 'reviewer'])
        self.assertEqual(pane.next, row.key)
        self.data['latest_pass']['rows'][0]['state'] = 'ready'
        refreshed = self.pane(pane, row.key, True)
        self.assertEqual(refreshed.selected, row.key)
        self.assertEqual(refreshed.sections[-1].rows[0].agent, 'reviewer')

    def test_attention_merges_items_and_keeps_other_agents_in_their_sections(self):
        first = {'item': 114, 'agent': 'reviewer', 'state': 'parked', 'reason': 'Review needed'}
        self.data['latest_pass']['rows'] = [first, dict(first),
            {'item': 114, 'agent': 'integrator', 'state': 'blocked', 'reason': 'CI failed'},
            {'item': 114, 'agent': 'preparer', 'state': 'ready'},
            {'item': 20, 'agent': 'reviewer', 'state': 'blocked'}]
        pane = self.pane(selected='plan:114:attention', chosen=True)
        self.assertEqual([section.label for section in pane.sections],
                         ['Running · 1', 'Needs attention · 2', 'Eligible · 1'])
        attention = pane.sections[1].rows
        self.assertEqual([row.item for row in attention], [114, 20])
        self.assertEqual([row.agent for row in attention[0].attention_rows], ['reviewer', 'integrator'])
        self.assertEqual(pane.selected, attention[0].key)
        self.assertEqual(pane.sections[0].rows[0].agent, 'implementer')
        self.assertEqual(pane.sections[2].rows[0].agent, 'preparer')

    def test_attention_refresh_replaces_reasons_and_drops_departing_agents(self):
        self.data['latest_pass']['rows'] = [
            {'item': 12, 'agent': 'reviewer', 'state': 'parked', 'reason': 'Old decision'},
            {'item': 12, 'agent': 'integrator', 'state': 'blocked', 'reason': 'Old failure'}]
        pane = self.pane(selected='plan:12:attention', chosen=True)
        for state in ('partial', 'complete'):
            with self.subTest(pass_state=state):
                self.data['latest_pass'] = {'state': state, 'rows': [
                    {'item': 12, 'agent': 'reviewer', 'state': 'ready'},
                    {'item': 12, 'agent': 'integrator', 'state': 'blocked', 'reason': 'New failure'}]}
                pane = self.pane(pane, pane.selected, True)
                self.assertEqual(pane.selected, 'plan:12:attention')
                attention = pane.sections[1].rows
                self.assertEqual(len(attention), 1)
                self.assertEqual([(row.agent, row.reason) for row in attention[0].attention_rows],
                                 [('integrator', 'New failure')])
                self.assertEqual(pane.sections[-1].rows[0].agent, 'reviewer')
        self.data['latest_pass']['rows'] = [{'item': 12, 'agent': 'integrator', 'state': 'ready'}]
        pane = self.pane(pane, pane.selected, True)
        self.assertEqual(pane.selected, 'plan:12')
        self.assertNotIn('Needs attention', [section.name for section in pane.sections])

    def test_vanished_eligible_is_listed_counted_and_retained_only_while_selected(self):
        pane = self.pane(selected='plan:12', chosen=True)
        observed = next(row for row in pane.rows if row.key == pane.selected)
        self.data['latest_pass']['rows'] = []
        pane = self.pane(pane, 'plan:12', True)
        kept = pane.sections[-1].rows[0]
        self.assertEqual(pane.selected, 'plan:12')
        self.assertEqual(kept.state, 'earlier observation')
        self.assertEqual(kept.reason, 'Last observed state: ready. Trigger matched')
        self.assertIs(kept.data, observed.data)
        self.assertFalse(kept.hidden)
        self.assertEqual(pane.sections[-1].label, 'Eligible · 1')
        repeated = self.pane(pane, pane.selected, True)
        self.assertEqual(repeated, pane)
        other = self.pane(pane, 'assignment:owned-run', True)
        self.assertNotIn('plan:12', [row.key for row in other.rows])
        self.data['latest_pass']['rows'] = [{'item': 12, 'agent': 'reviewer', 'state': 'ready'}]
        returned = self.pane(pane, 'plan:12', True)
        self.assertEqual(returned.sections[-1].rows[0].state, 'ready')

    def test_other_vanished_selections_are_details_only_and_never_counted(self):
        cases = [
            ('assignment:owned-run', None),
            ('outcome:previous-run', None),
            ('plan:12:attention', {'item': 12, 'agent': 'reviewer', 'state': 'blocked'}),
            ('plan:12', {'item': 12, 'agent': 'reviewer', 'state': 'owned'}),
            ('plan:12', {'item': 12, 'agent': 'reviewer', 'state': 'parked', 'reason': 'Waiting for blockers #31'}),
            ('plan:12', {'item': 12, 'agent': 'reviewer', 'state': 'parked',
                         'reason': 'Waiting for active milestone #10'})]
        for key, plan in cases:
            with self.subTest(key=key, plan=plan):
                self.setUp()
                if key == 'plan:12:attention':
                    self.data['latest_pass']['rows'][1] = plan
                pane = self.pane(selected=key, chosen=True)
                self.data['assignment'] = None
                self.data['outcomes'] = []
                self.data['latest_pass']['rows'] = [plan] if plan and key == 'plan:12' else []
                pane = self.pane(pane, key, True)
                self.assertEqual(pane.selected, key)
                self.assertEqual([row.key for row in pane.rows], [key])
                kept = pane.rows[0]
                self.assertEqual(kept.state, 'earlier observation')
                self.assertTrue(kept.hidden)
                self.assertEqual([section.label for section in pane.sections], ['Running · 0'])
                self.assertTrue(pane.sections[0].idle)
                self.assertEqual(pane.recent, ())
                self.assertIsNone(pane.next)
                # A later sparse pass must not make a hidden selection live again.
                self.data['latest_pass']['rows'] = []
                self.assertEqual(self.pane(pane, key, True).sections, pane.sections)

    def test_omitted_selected_agent_uses_the_same_sanitized_identity_as_work_rows(self):
        self.data['latest_pass']['rows'][1]['agent'] = 'reviewer\x1b'
        pane = self.pane(selected='plan:12', chosen=True)
        self.data['latest_pass']['rows'][1]['state'] = 'owned'
        kept = self.pane(pane, pane.selected, True)
        self.assertEqual([section.label for section in kept.sections], ['Running · 1'])
        self.assertTrue(kept.rows[-1].hidden)

    def test_follows_own_runs_until_a_person_picks_a_row(self):
        assignment = self.data['assignment']
        self.data['assignment'] = None
        pane = self.pane()
        self.assertEqual(pane.selected, 'plan:114')
        self.data['assignment'] = assignment
        pane = self.pane(pane, pane.selected)
        self.assertEqual(pane.selected, 'assignment:owned-run')
        self.assertNotIn('plan:114', [row.key for row in pane.rows])
        self.data['assignment'] = dict(assignment, run='next-run')
        followed = self.pane(pane, pane.selected)
        self.assertEqual(followed.selected, 'assignment:next-run')
        self.assertNotIn('assignment:owned-run', [row.key for row in followed.rows])
        picked = self.pane(pane, 'plan:12', True)
        self.assertEqual(picked.selected, 'plan:12')
        kept = self.pane(pane, pane.selected, True)
        self.assertEqual(kept.selected, 'assignment:owned-run')
        self.assertEqual([row.key for row in kept.sections[0].rows], ['assignment:next-run'])

    def test_selection_moves_to_related_plan_including_a_merged_secondary_agent(self):
        pane = self.pane(selected='plan:12', chosen=True)
        self.data['latest_pass']['rows'] = [{'item': 12, 'agent': 'reviewer', 'state': 'blocked'}]
        pane = self.pane(pane, pane.selected, True)
        self.assertEqual(pane.selected, 'plan:12:attention')
        self.assertEqual([section.label for section in pane.sections], ['Running · 1', 'Needs attention · 1'])
        self.data['latest_pass']['rows'] = [
            {'item': 12, 'agent': 'integrator', 'state': 'ready'},
            {'item': 12, 'agent': 'reviewer', 'state': 'ready'}]
        pane = self.pane(pane, pane.selected, True)
        self.assertEqual(pane.selected, 'plan:12')
        self.assertNotIn('plan:12:attention', [row.key for row in pane.rows])
        self.assertEqual([plan['agent'] for plan in pane.sections[-1].rows[0].eligible_plans],
                         ['integrator', 'reviewer'])

    def test_selected_claiming_assignment_follows_its_named_run(self):
        self.data['assignment'].pop('run')
        claiming = self.pane()
        self.assertEqual(claiming.selected, 'assignment:claiming')
        self.assertIsNone(claiming.rows[0].log)
        self.data['assignment']['run'] = 'owned-run'
        for chosen in (False, True):
            with self.subTest(chosen=chosen):
                named = self.pane(claiming, claiming.selected, chosen)
                self.assertEqual(named.selected, 'assignment:owned-run')
                self.assertNotIn('assignment:claiming', [row.key for row in named.rows])
                self.assertEqual(named.sections[0].rows[0].log,
                                 self.root / '.ub-agents/runs/owned-run/process.log')

    def test_chosen_claiming_assignment_does_not_follow_a_different_item_or_agent(self):
        self.data['assignment'].pop('run')
        claiming = self.pane()
        for change in ({'item': 115}, {'agent': 'reviewer'}):
            with self.subTest(change=change):
                self.data['assignment'] = {'item': 114, 'agent': 'implementer', 'run': 'other-run'} | change
                named = self.pane(claiming, claiming.selected, True)
                self.assertEqual(named.selected, 'assignment:claiming')
                self.assertEqual(named.rows[-1].state, 'earlier observation')
                self.assertIsNone(named.rows[-1].log)

    def test_recent_is_bounded_and_a_rolled_out_selection_is_details_only(self):
        self.data['outcomes'] = [{'item': n, 'run': f'run-{n}', 'result': 'success'} for n in range(1, 21)]
        pane = self.pane(selected='outcome:run-1', chosen=True)
        self.assertEqual([row.item for row in pane.recent], list(range(20, 0, -1)))
        self.data['outcomes'] = [{'item': n, 'run': f'run-{n}', 'result': 'success'} for n in range(2, 22)]
        pane = self.pane(pane, pane.selected, True)
        self.assertEqual(len(pane.recent), 20)
        self.assertEqual(pane.selected, 'outcome:run-1')
        self.assertNotIn(pane.selected, [row.key for row in pane.recent])
        self.assertEqual(pane.rows[-1].state, 'earlier observation')

    def test_missing_selection_falls_back_to_first_row_and_import_needs_no_textual(self):
        self.data['assignment'] = None
        pane = self.pane(selected='missing', chosen=True)
        self.assertEqual(pane.selected, 'plan:114')
        self.data['latest_pass']['rows'] = []
        self.assertEqual(self.pane().selected, 'outcome:previous-run')
        code = 'import sys; import ub_agents.view_data; assert "textual" not in sys.modules'
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ViewDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root)

    def test_worker_retries_a_late_log_and_reload_replaces_the_cached_reader_at_tail(self):
        self.log.unlink()
        worker = LocalWorker(self.root, self.path)
        request = Request('assignment:owned-run', 1, chosen=True)
        missing = worker.read(request)
        self.assertEqual(missing.page.refs, ())
        reader = worker.readers[str(self.log)]
        self.log.write_bytes(b'first\n')
        picked_up = worker.read(request)
        self.assertEqual(picked_up.page.refs[-1].value.text, 'first')
        self.assertIs(worker.readers[str(self.log)], reader)
        # Reload skips the old backlog and reads a bounded tail of the file.
        self.log.write_bytes(b'first\n' + b'old output\n' * 10000 + b'latest output\n')
        reloaded = worker.read(Request(request.key, 2, chosen=True, reload=True))
        self.assertIsNot(worker.readers[str(self.log)], reader)
        self.assertGreater(reloaded.page.start, 0)
        self.assertLess(len(reloaded.page.refs), 201)
        self.assertLessEqual(reloaded.log.bytes_read, PAGE_BYTES)
        # The record budget may need a second update to finish the bounded tail.
        reloaded = worker.read(request)
        self.assertEqual(reloaded.log.unread_bytes, 0)
        self.assertEqual(reloaded.page.refs[-1].value.text, 'latest output')

    def test_worker_reload_resolves_a_claiming_row_from_the_latest_snapshot(self):
        self.state['assignment'].pop('run')
        publish_snapshot(self.path, self.state)
        worker = LocalWorker(self.root, self.path)
        claiming = worker.read(Request('assignment:claiming', 1, chosen=True))
        self.assertIsNone(claiming.log)
        self.state['assignment']['run'] = 'owned-run'
        self.log.write_bytes(event(1, 20))
        publish_snapshot(self.path, self.state)
        named = worker.read(Request(claiming.key, 2, chosen=True, previous=claiming.pane, reload=True))
        self.assertEqual(named.key, 'assignment:owned-run')
        self.assertIn('event 00001', named.page.refs[-1].value.text)

    def test_worker_retains_from_the_requested_pane_instead_of_its_last_result(self):
        worker = LocalWorker(self.root, self.path)
        drawn = worker.read(Request('plan:12', 1, chosen=True)).pane
        self.state['latest_pass']['rows'] = []
        publish_snapshot(self.path, self.state)
        other = worker.read(Request('assignment:owned-run', 2, chosen=True, previous=drawn))
        self.assertNotIn('plan:12', [row.key for row in other.pane.rows])
        result = worker.read(Request('plan:12', 3, chosen=True, previous=drawn))
        self.assertEqual(result.key, 'plan:12')
        self.assertEqual(result.pane.selected, result.key)
        self.assertEqual(result.pane.sections[-1].label, 'Eligible · 1')
        self.assertEqual(result.pane.rows[-1].state, 'earlier observation')
        # Without the last drawn pane, a separate request has no retained row.
        unrelated = worker.read(Request('plan:12', 4, chosen=True))
        self.assertNotIn('plan:12', [row.key for row in unrelated.pane.rows])

    def test_header_assignment_plan_pr_and_missing_fields(self):
        self.state['assignment'].update(kind='issue', title='Assignment title', attempt=3)
        self.state['latest_pass']['rows'][1].update(
            kind='pr', title='Planned PR', failures=2, max_attempts=5, runtime='codex:gpt-6.1-sol:xhigh')
        self.state['outcomes'].append({'item': 114, 'run': 'handoff', 'handoff': 1235})
        session = Session(self.path, self.state)
        work = work_rows(session, self.root)
        self.assertEqual(item_header(work[0], Description(title='Context title'), session),
                         ('#114 Context title', 'implementer · claude synthetic-model high · attempt 3 · ⌥1235'))
        planned = next(row for row in work if row.item == 12)
        self.assertEqual(item_header(planned, None, session),
                         ('⌥12 Planned PR', 'reviewer · codex gpt-6.1-sol xhigh · 2/5 failures'))
        self.state['assignment'] = {'item': 114}
        session = Session(self.path, self.state)
        self.assertEqual(item_header(work_rows(session, self.root)[0], None, session),
                         ('#114 Cached title', '⌥1235'))
        self.state.pop('histories')  # Missing fields in an older snapshot.
        session = Session(self.path, self.state)
        self.assertEqual(item_header(work_rows(session, self.root)[0], None, session), ('#114', '⌥1235'))
        # Older snapshots can still obtain PR identity from local run context.
        self.assertEqual(item_header(work_rows(session, self.root)[0], Description(title='PR', kind='pr'), session)[0],
                         '⌥114 PR')

    def test_stopping_status_applies_only_to_the_current_assignment(self):
        self.state['outcomes'].extend([
            {'item': 114, 'agent': 'implementer', 'run': 'before', 'result': 'success'},
            {'item': 114, 'agent': 'implementer', 'run': 'owned-run', 'result': 'success',
             'acceptance': 'unaccepted'}])
        session = Session(self.path, self.state)
        work = work_rows(session, self.root)
        normal = {row.key: run_status(row, session) for row in work}
        self.state['activity'] = {'state': 'stopping'}
        for row in work:
            with self.subTest(key=row.key):
                self.assertEqual(run_status(row, session),
                                 ('■ Stopping after this run (SIGTERM) · no new claims', '', False)
                                 if row.key == 'assignment:owned-run' else normal[row.key])
        self.state['assignment']['run'] = 'different-run'
        self.assertNotIn('Stopping after this run', run_status(work[0], session)[0])
        self.state['assignment'] = None
        self.assertEqual(run_status(None, session), ('○ Idle · waiting for the next poll', '', False))

    def test_status_matches_only_selected_run_and_counts_other_same_item_outcomes(self):
        current = {'item': 114, 'agent': 'implementer', 'run': 'owned-run',
                   'result': 'success', 'acceptance': 'unaccepted'}
        self.state['outcomes'].extend([
            {'item': 114, 'agent': 'preparer', 'run': 'before', 'handoff': 999}, current])
        session = Session(self.path, self.state)
        assignment = work_rows(session, self.root)[0]
        for acceptance in ('unaccepted', 'accepted', 'finalized', 'rejected'):
            current['acceptance'] = acceptance
            self.assertEqual(run_status(assignment, session),
                             (f'implementer running · reported success ({acceptance})', '1 earlier run', True))
        # A reported outcome can be selected before its process has exited.
        reported = next(row for row in work_rows(session, self.root) if row.key == 'outcome:owned-run')
        self.assertTrue(run_status(reported, session)[2])
        self.state['outcomes'].remove(current)
        self.assertEqual(run_status(assignment, session),
                         ('implementer running · no outcome reported', '1 earlier run', True))
        self.state['assignment'].update(process='recovery', recovered_run='before')
        recovered = work_rows(session, self.root)[0]
        self.assertEqual(run_status(recovered, session), ('implementer recovery · outcome reported', '', False))
        outcome = next(row for row in work_rows(session, self.root) if row.run == 'before')
        self.assertEqual(run_status(outcome, session), ('preparer exited · outcome reported', '', False))
        planned = next(row for row in work_rows(session, self.root) if row.item == 12)
        self.assertEqual(run_status(planned, session), ('reviewer ready · no outcome reported', '', False))
        self.state['assignment'] = None
        from dataclasses import replace
        earlier = replace(assignment, state='earlier observation')
        self.assertFalse(run_status(earlier, session)[2])

    def test_optional_header_counts_reject_invalid_snapshot_values(self):
        for field in ('attempt', 'failures', 'max_attempts', 'handoff'):
            for value in (-1, True, '3'):
                with self.subTest(field=field, value=value):
                    self.state['assignment'][field] = value
                    self.path.write_text(json.dumps(self.state))
                    self.assertEqual(load_session(self.path).state(), 'malformed')
            del self.state['assignment'][field]

    def test_section_mapping_deduplicates_assignment_and_preserves_planned_order(self):
        plans = [
            ('ready', 'Trigger matched', 'Eligible'),
            ('blocked', 'Last run blocked', 'Needs attention'),
            ('recover', 'Pending outcome', 'Eligible'),
            ('parked', 'Stop label needs-human is present', 'Needs attention'),
            ('parked', 'Approval required', 'Needs attention'),
            ('parked', 'Waiting for blockers #31', None),
            ('parked', 'Waiting for active milestone #10', None),
            ('backoff', 'Retry backoff', 'Eligible'),
            ('waiting', 'Runtime paused', 'Eligible'),
            ('recover', 'Pending outcome', 'Eligible'),
            ('ready', 'Trigger matched', 'Eligible'),
        ]
        self.state['latest_pass']['rows'][0]['state'] = 'ready'
        self.state['latest_pass']['rows'].extend(
            {'item': n, 'agent': 'worker', 'state': state, 'reason': reason}
            for n, (state, reason, _) in enumerate(plans, 20))
        work = work_rows(Session(self.path, self.state), self.root)
        self.assertEqual([row.group for row in work if row.item == 114], ['Running'])
        self.assertFalse(any(row.item == 13 for row in work))
        for n, (_, _, expected) in enumerate(plans, 20):
            observed = next((row for row in work if row.item == n), None)
            self.assertEqual(observed.group if observed else None, expected)
        self.assertEqual([row.item for row in work if row.group == 'Eligible'], [12, 20, 22, 29, 30, 27, 28])
        keys = {row.item: row.key for row in work}
        self.state['latest_pass']['rows'].reverse()
        reordered = work_rows(Session(self.path, self.state), self.root)
        self.assertEqual([row.item for row in reordered if row.group == 'Eligible'], [30, 29, 22, 20, 12, 28, 27])
        self.assertEqual({row.item: row.key for row in reordered}, keys)

    def test_plans_merge_by_section_after_ordering_and_running_stays_per_agent(self):
        self.state['latest_pass']['rows'] = [
            {'item': 20, 'agent': 'reviewer', 'state': 'backoff'},
            {'item': 12, 'agent': 'reviewer', 'state': 'recover'},
            {'item': 114, 'agent': 'implementer', 'state': 'ready'},
            {'item': 20, 'agent': 'integrator', 'state': 'ready'},
            {'item': 12, 'agent': 'integrator', 'state': 'waiting'},
            {'item': 12, 'agent': 'worker', 'state': 'owned'},
            {'item': 114, 'agent': 'preparer', 'state': 'ready'},
            {'item': 30, 'agent': 'reviewer', 'state': 'blocked'},
            {'item': 30, 'agent': 'integrator', 'state': 'parked'},
            {'item': 40, 'agent': 'reviewer', 'state': 'waiting'},
            {'item': 40, 'agent': 'integrator', 'state': 'backoff'},
        ]
        work = work_rows(Session(self.path, self.state), self.root)
        eligible = [row for row in work if row.group == 'Eligible']
        self.assertEqual([(row.key, row.agent, row.state) for row in eligible],
                         [('plan:12', 'reviewer', 'recover'), ('plan:20', 'integrator', 'ready'),
                          ('plan:114', 'preparer', 'ready'), ('plan:40', 'reviewer', 'waiting')])
        self.assertEqual([[plan['agent'] for plan in row.eligible_plans] for row in eligible],
                         [['reviewer', 'integrator'], ['integrator', 'reviewer'],
                          ['preparer'], ['reviewer', 'integrator']])
        self.assertEqual([row.agent for row in work if row.group == 'Running'], ['implementer'])
        self.assertEqual([row.key for row in work if row.group == 'Needs attention'],
                         ['plan:30:attention'])
        attention = next(row for row in work if row.group == 'Needs attention')
        self.assertEqual([row.agent for row in attention.attention_rows], ['reviewer', 'integrator'])

    def test_dependency_waits_are_omitted_under_each_milestone_policy(self):
        worker = agent(self.root)
        item = issue(milestone=20)
        matches = AgentMatches.for_item(item, (worker,))
        for policy in ('ignore', 'order', 'gate'):
            with self.subTest(policy=policy):
                check = check_start(item, worker, matches, (),
                                    Queue(milestones=policy), ('#31',))
                self.assertFalse(check.allowed)
                for state in ('partial', 'complete'):
                    snapshot = Session(self.path, {'latest_pass': {'state': state, 'rows': [
                        {'item': item.number, 'agent': worker.name, 'state': 'parked', 'reason': check.reason}]}})
                    self.assertEqual(work_rows(snapshot, self.root), [])

    def test_foreign_claims_are_omitted_from_partial_and_complete_passes(self):
        foreign = self.state['latest_pass']['rows'][2]
        for assignment in (self.state['assignment'], None):
            for state in ('partial', 'complete'):
                with self.subTest(assignment=bool(assignment), state=state):
                    session = Session(self.path, {'assignment': assignment,
                        'latest_pass': {'state': state, 'rows': [foreign]}})
                    work = work_rows(session, self.root)
                    self.assertEqual([row.key for row in work],
                                     ['assignment:owned-run'] if assignment else [])
                    self.assertEqual(run_status(None, session)[0], 'No item selected.' if assignment else
                                     '○ Idle · waiting for the next poll')

    def test_today_count_uses_local_dates_and_ignores_missing_invalid_times(self):
        local = timezone(timedelta(hours=2))
        now = datetime(2026, 10, 4, 12, tzinfo=local)
        times = ['2026-10-03T22:30:00Z', '2026-10-04T21:59:59+00:00',
                 '2026-10-04T23:30:00Z', '2026-10-03T21:59:59Z',
                 '2026-10-04T12:00:00', 'invalid', None]
        snapshot = Session(self.path, {'outcomes': [{'time': stamp} for stamp in times] + [{}]})
        self.assertEqual(outcomes_today(snapshot, now), 2)

    def test_selection_only_live_or_explicit_including_ended_malformed(self):
        self.assertEqual(choose_session(self.root)[0], self.path)
        second = self.path.with_name('idle.json')
        state = dict(self.state, assignment=None)
        second.write_text(json.dumps(state))
        selected, listing = choose_session(self.root)
        self.assertIsNone(selected)
        self.assertIn('idle', '\n'.join(listing))
        second.write_text(json.dumps(dict(state, ended=True)))
        self.assertEqual(choose_session(self.root)[0], self.path)
        self.assertEqual(choose_session(self.root, 'idle')[0], second)
        second.write_text('{broken')
        self.assertIn('malformed', load_session(second).freshness())
        with self.assertRaises(ValueError):
            choose_session(self.root, '../escape')

    def test_sparse_stale_and_bad_shapes_and_controls_are_safe(self):
        self.path.write_text('{"version":1}')
        session = load_session(self.path)
        self.assertEqual(session.state(), 'stale')
        self.assertEqual(work_rows(session, self.root), [])
        for value in ({'version': 2}, {'version': 1, 'outcomes': {}}, {'version': 1, 'assignment': []},
                      {'version': 1, 'latest_pass': {'rows': 5}}, {'version': 1, 'published_at': 3}):
            self.path.write_text(json.dumps(value))
            self.assertIn(load_session(self.path).state(), ('stale', 'malformed'))
        for value in ({'version': True}, {'version': 1, 'outcomes': [3]},
                      {'version': 1, 'assignment': {'item': 'bad'}},
                      {'version': 1, 'latest_pass': {'rows': [{'owner': []}]}},
                      {'version': 1, 'histories': []},
                      {'version': 1, 'histories': {'1': {'runs': [3]}}},
                      {'version': 1, 'histories': {'1': {'runs': [], 'filing': []}}},
                      {'version': 1, 'histories': {'1': {'runs': [], 'omitted_runs': -1}}}):
            self.path.write_text(json.dumps(value))
            self.assertEqual(load_session(self.path).state(), 'malformed')
        self.assertEqual(Session(self.path, dict(self.state, ended=True)).state(), 'ended')
        old = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
        self.assertEqual(Session(self.path, dict(self.state, published_at=old)).state(), 'stale')

    def test_context_is_cached_local_and_plan_has_no_log(self):
        session = load_session(self.path)
        work = work_rows(session, self.root)
        self.assertEqual(work[0].runtime, 'claude')
        self.assertIn('Cached description', item_context(work[0], session))
        foreign = next(row for row in work if row.item == 12)
        self.assertIsNone(foreign.log)
        self.assertIsNone(foreign.context)
        self.assertEqual(foreign.reason, 'Trigger matched')
        self.assertIn('Description unavailable', item_context(foreign, session))
        self.assertIn('BLOCKED: needs-human', work[-1].state)
        self.assertIn('BLOCKED: needs-human', outcome_text(session))
        self.state['latest_pass']['rows'][0]['description'] = {'available': True, 'text': 'snapshot body'}
        session = Session(self.path, self.state)
        with patch('ub_agents.view_data.read_json', side_effect=AssertionError('unneeded local context read')):
            self.assertIn('snapshot body', item_context(work[0], session))

    def test_description_whitespace_and_all_other_controls_are_inert(self):
        whitespace = 'LF\nCRLF\r\nCR\rTAB\tend'
        controls = ''.join(chr(code) for code in (*range(32), *range(127, 160))
                           if chr(code) not in '\r\n\t') + '\ud800'
        description = Description(whitespace + controls, whitespace + controls, available=True)
        expected = 'LF\nCRLF\nCR\nTAB\tend'
        for value in (description.title, description.body):
            self.assertTrue(value.startswith(expected))
            self.assertFalse(any(char in value for char in controls))
            for char in controls:
                self.assertIn(f'\\x{ord(char):02x}' if ord(char) < 256 else r'\ud800', value)
        # The shared projections used by Runs and logs retain their old escaping.
        self.assertEqual(text(whitespace), r'LF\nCRLF\r\nCR\rTAB\tend')

    def test_description_limit_has_separate_notices_for_title_and_body(self):
        description = Description('t' * 2049, '```\n' + 'x' * 2049, available=True)
        self.assertEqual(len(description.title), 2048)
        self.assertEqual(len(description.body), 2048)
        self.assertNotIn('shortened', description.body)
        self.assertIn('Title shortened', description.details())
        self.assertIn('Description shortened', description.details())
        exact = Description('t' * 2048, 'x' * 2048)
        self.assertEqual(exact.notice, '')

    def test_cli_lists_without_importing_textual_or_invoking_a_process(self):
        self.path.write_text(json.dumps(dict(self.state, ended=True)))
        with patch('subprocess.Popen', side_effect=AssertionError('process/network access')), contextlib.redirect_stdout(io.StringIO()) as output:
            main([str(self.root)])
        self.assertIn('launcher  ended', output.getvalue())
        completed = subprocess.run([sys.executable, '-c', 'import ub_agents.cli, ub_agents.view, sys; assert "textual" not in sys.modules'], capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_size_limit_and_fifo_are_rejected(self):
        self.path.write_bytes(b'x' * (64 * 1024 + 1))
        self.assertIn('exceeds', load_session(self.path).error)
        if hasattr(os, 'mkfifo'):
            self.path.unlink()
            os.mkfifo(self.path)
            self.assertIn('regular file', load_session(self.path).error)


class ViewLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, _ = fixture(self.root)

    def drain(self, reader):
        for _ in range(10000):
            snapshot = reader.update()
            if not snapshot.unread_bytes:
                return snapshot
        self.fail('Reader did not drain')

    def test_tail_refs_and_older_pages_reach_byte_zero_without_gaps(self):
        self.log.write_bytes(b''.join(event(i) for i in range(1800)))
        reader = ViewReader(self.log, 'claude')
        snapshot = self.drain(reader)
        page = reader.page()
        self.assertIn('event 01799', page.refs[-1].value.text)
        self.assertGreater(page.start, 0)
        prior = page.start
        visited = [prior]
        for _ in range(100):
            page = reader.older(prior, reader.resets)
            self.assertLess(page.start, prior)
            self.assertEqual(page.end, prior)
            self.assertLessEqual(prior - page.start, PAGE_BYTES)
            self.assertLessEqual(len(page.refs), 201)
            prior = page.refs[0].start if page.refs else page.start
            visited.append(prior)
            if not prior:
                break
        self.assertEqual(prior, 0)
        self.assertEqual(page.refs[0].value.kind, 'assistant')
        self.assertGreater(len(visited), 10)
        self.assertLessEqual(snapshot.bytes_read, 32768)

    def test_tiny_record_pages_preserve_paging_progress_and_mark_boundary(self):
        self.log.write_bytes(b'a\n' * 50000)
        reader = ViewReader(self.log, 'command')
        self.assertLess(reader.update().unread_bytes, 2048)
        self.drain(reader)
        page = reader.older(reader.page().start, reader.resets)
        self.assertIn('Entry limit boundary', page.notice)
        self.assertLessEqual(len(page.refs), 201)
        self.assertTrue(all(ref.value.text == 'a' for ref in page.refs))
        self.assertLess(page.refs[0].start, reader.page().start)

    def test_cut_off_first_page_fragment_is_skipped_and_keeps_raw_bytes(self):
        data = b'prefix' + event(1, size=PAGE_BYTES * 6)
        self.log.write_bytes(data)
        reader = ViewReader(self.log, 'claude')
        self.drain(reader)
        page = reader.older(reader.page().start, reader.resets)
        self.assertIn('boundary', page.notice)
        self.assertTrue(all('partial raw record' in ref.value.kind for ref in page.refs))
        self.assertLess(page.start, page.end)
        fragment = page.refs[0].value
        self.assertEqual(fragment.text, '          · earlier output skipped · h older')
        self.assertIn('partial raw record (page end)', fragment.display(raw=True))
        self.assertNotIn('earlier output skipped', fragment.display(raw=True))

    def test_older_page_skips_cut_off_init_and_folds_latest_tool_progress(self):
        from tests.test_log_reader import record, progress, result, tool
        init = json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['private-tool'] * 5000}).encode() + b'\n'
        page_bytes = init + record(content=[tool()]) + progress(45) + progress(60) + progress(119) + record('user', [result()])
        self.log.write_bytes(page_bytes + b'padding\n' * 5000)
        reader = ViewReader(self.log, 'claude')
        self.drain(reader)
        page = reader.older(len(page_bytes), reader.resets)
        self.assertEqual(page.refs[0].value.text, '          · earlier output skipped · h older')
        self.assertIn('private-tool', page.refs[0].value.display(raw=True))
        call = next(ref.value for ref in page.refs if ref.value.calls)
        self.assertTrue(call.text.endswith(' · 1m'))
        self.assertEqual([ref.value.display() for ref in page.refs[-4:]], ['', '', '', ''])

    def test_generation_does_not_mix_and_history_checks_file_before_and_after(self):
        self.log.write_bytes(b'old\n' * 10000)
        reader = ViewReader(self.log, 'command')
        self.drain(reader)
        original = reader.page()
        replacement = self.log.with_suffix('.next')
        replacement.write_bytes(b'new\n' * 10000)
        replacement.replace(self.log)
        with self.assertRaises(FileChanged):
            reader.older(original.start, original.generation)
        self.drain(reader)
        self.assertTrue(all('old' not in ref.value.text for ref in reader.page().refs))
        with self.assertRaises(FileChanged):
            reader.older(original.start, original.generation)
        self.log.write_bytes(b'regrown generation\n' * 3000)
        self.drain(reader)
        self.assertEqual(reader.resets, 2)
        self.log.write_bytes(b'truncated\n')
        self.drain(reader)
        self.assertEqual(reader.resets, 3)

    def test_replacement_during_older_read_discards_result(self):
        self.log.write_bytes(b'original\n' * 5000)
        reader = ViewReader(self.log, 'command')
        self.drain(reader)
        validate = reader._validate
        calls = 0
        def replace(stream, info):
            nonlocal calls
            calls += 1
            validate(stream, info)
            if calls == 2:
                other = self.log.with_suffix('.next')
                other.write_bytes(b'replaced\n')
                other.replace(self.log)
        with patch.object(reader, '_validate', side_effect=replace), self.assertRaises(FileChanged):
            reader.older(reader.page().start, reader.resets)


if __name__ == '__main__':
    unittest.main()
