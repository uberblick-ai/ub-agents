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
                                 item_header, outcome_text, outcomes_today, read_json, run_status, text, work_rows)
from ub_agents.view_logs import FileChanged, PAGE_BYTES, ViewReader
from ub_agents.config import Queue
from ub_agents.eligibility import AgentMatches, check_start
from tests.support import agent, issue


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
    path.write_text(json.dumps(state))
    if count:
        log.write_bytes(b''.join(event(i) for i in range(count)))
    return path, log, state


def event(i, size=400):
    return (json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'text', 'text': f'event {i:05d} ' + 'x' * size}]}}) + '\n').encode()


class ViewDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root)

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

    def test_eligibility_dependency_and_milestone_waits_are_omitted(self):
        worker = agent(self.root)
        item = issue(milestone=20)
        matches = AgentMatches.for_item(item, (worker,))
        for gate, blockers in ((False, ('#31',)), (True, ()), (True, ('#31',))):
            with self.subTest(gate=gate, blockers=blockers):
                check = check_start(item, worker, matches, (),
                                    Queue(milestones='gate' if gate else 'ignore'), 10, blockers)
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
