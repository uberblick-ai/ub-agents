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
                                 outcome_text, outcomes_today, read_json, text, work_rows)
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
                 {'item': 12, 'agent': 'reviewer', 'state': 'owned', 'reason': 'occupied',
                  'owner': {'actor': 'other-launcher', 'host': 'other-host', 'run': 'foreign-run'},
                  'process_log': str(log)}]},
             'outcomes': [{'item': 10, 'agent': 'preparer', 'run': 'previous-run', 'completed': True,
                           'runtime': 'claude:synthetic-model:high',
                           'acceptance': 'finalized', 'result': 'prepared', 'summary': 'Waiting for decision',
                           'human_blocker': ['needs-human'], 'time': '2026-10-04T00:00:00Z'}]}
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

    def test_section_mapping_deduplicates_assignment_and_preserves_planned_order(self):
        plans = [
            ('ready', 'Trigger matched', 'Eligible'),
            ('blocked', 'Last run blocked', 'Needs attention'),
            ('recover', 'Pending outcome', 'Eligible'),
            ('parked', 'Stop label needs-human is present', 'Needs attention'),
            ('parked', 'Approval required', 'Needs attention'),
            ('parked', 'Waiting for blockers #31', 'Waiting'),
            ('parked', 'Waiting for active milestone #10', 'Waiting'),
            ('backoff', 'Retry backoff', 'Waiting'),
            ('waiting', 'Runtime paused', 'Waiting'),
        ]
        self.state['latest_pass']['rows'][0]['state'] = 'ready'
        self.state['latest_pass']['rows'].extend(
            {'item': n, 'agent': 'worker', 'state': state, 'reason': reason}
            for n, (state, reason, _) in enumerate(plans, 20))
        work = work_rows(Session(self.path, self.state), self.root)
        self.assertEqual([row.group for row in work if row.item == 114], ['Running'])
        self.assertEqual(next(row for row in work if row.item == 12).group, 'Running')
        for n, (_, _, expected) in enumerate(plans, 20):
            self.assertEqual(next(row for row in work if row.item == n).group, expected)
        self.assertEqual([row.item for row in work if row.group == 'Eligible'], [20, 22])
        keys = {row.item: row.key for row in work}
        self.state['latest_pass']['rows'].reverse()
        reordered = work_rows(Session(self.path, self.state), self.root)
        self.assertEqual([row.item for row in reordered if row.group == 'Eligible'], [22, 20])
        self.assertEqual({row.item: row.key for row in reordered}, keys)

    def test_eligibility_wait_wording_classifies_as_waiting(self):
        worker = agent(self.root)
        item = issue(milestone=20)
        matches = AgentMatches.for_item(item, (worker,))
        for gate, blockers in ((False, ('#31',)), (True, ()), (True, ('#31',))):
            with self.subTest(gate=gate, blockers=blockers):
                check = check_start(item, worker, matches, (),
                                    Queue(milestones='gate' if gate else 'ignore'), 10, blockers)
                self.assertFalse(check.allowed)
                snapshot = Session(self.path, {'latest_pass': {'rows': [
                    {'item': item.number, 'agent': worker.name, 'state': 'parked', 'reason': check.reason}]}})
                self.assertEqual(work_rows(snapshot, self.root)[0].group, 'Waiting')

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
                      {'version': 1, 'latest_pass': {'rows': [{'owner': []}]}}):
            self.path.write_text(json.dumps(value))
            self.assertEqual(load_session(self.path).state(), 'malformed')
        self.assertEqual(Session(self.path, dict(self.state, ended=True)).state(), 'ended')
        old = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
        self.assertEqual(Session(self.path, dict(self.state, published_at=old)).state(), 'stale')

    def test_context_is_cached_local_and_foreign_owner_has_no_log(self):
        session = load_session(self.path)
        work = work_rows(session, self.root)
        self.assertEqual(work[0].runtime, 'claude')
        self.assertIn('Cached description', item_context(work[0], session))
        foreign = next(row for row in work if row.item == 12)
        self.assertIsNone(foreign.log)
        self.assertIsNone(foreign.context)
        self.assertIn('@other-launcher', foreign.reason)
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

    def test_long_page_fragments_are_raw_and_marked_never_events(self):
        data = b'prefix' + event(1, size=PAGE_BYTES * 6)
        self.log.write_bytes(data)
        reader = ViewReader(self.log, 'claude')
        self.drain(reader)
        page = reader.older(reader.page().start, reader.resets)
        self.assertIn('boundary', page.notice)
        self.assertTrue(all('partial raw record' in ref.value.kind for ref in page.refs))
        self.assertLess(page.start, page.end)
        self.assertEqual(page.refs[0].value.raw, page.refs[0].value.text)

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
