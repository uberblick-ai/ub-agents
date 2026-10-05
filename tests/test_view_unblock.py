import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Markdown, Static, TabbedContent

from tests.support import RecordingDescriptionTransport
from tests.test_view_data import fixture
from ub_agents.view_data import Description, Session, WorkRow
from ub_agents.view_github import (CACHE_ITEMS, COMMENTS_QUERY, RESPONSE_BYTES, DescriptionLoads,
                                   GhTransport, Response, parse_response)
from ub_agents.view_ui import ItemTabs, View, description_parser
from ub_agents.view_unblock import (ACTION_MARKER, ActionComment, comment_body, local_action,
                                    needs_attention, stamp, unblock_metadata)

AUTHORS = {'operator': {'trusted': True, 'reason': None},
           'reader': {'trusted': False, 'reason': 'write or higher is required'}}
NOTICE = (ACTION_MARKER + 'run -->\n**Action needed**\n\n'
          '## Resolve the blocker\n**Needs:** a decision.\n\n'
          'Candidate: `abcdef1234567890`.\n\n'
          '[Claim](https://example.test/claim) · [Outcome](https://example.test/outcome)\n\n'
          'After resolving the blocker, run:\n\n```sh\nub-agents retry 178\n```\n')


def comment(body=NOTICE, author='operator', created='2026-10-05T12:12:00Z'):
    return {'body': body, 'author': {'login': author}, 'createdAt': created}


def comments_reply(*comments):
    return json.dumps({'data': {'repository': {'issueOrPullRequest': {
        'comments': {'nodes': list(comments)}}}}}).encode()


class UnblockDataTests(unittest.TestCase):
    def setUp(self):
        self.row = WorkRow('plan:178:worker', 'Needs attention', 178, 'worker', 'parked', '',
                           {'kind': 'pr', 'title': 'Item title', 'failures': 3, 'max_attempts': 3,
                            'waiting_since': '2026-10-05T12:12:00Z'})
        self.session = Session(Path('session.json'), {'coordination_authors': AUTHORS, 'action_needed': {
            '178': {'text': NOTICE, 'author': 'operator', 'created_at': '2026-10-05T12:12:00Z'}}})

    def test_tab_applies_only_to_current_attention_rows(self):
        self.assertTrue(needs_attention(self.row))
        for state in ('blocked', 'failed'):
            self.assertTrue(needs_attention(replace(self.row, state=state)))
        for state in ('ready', 'recover', 'backoff', 'waiting', 'owned', 'earlier observation'):
            self.assertFalse(needs_attention(replace(self.row, state=state)))
        self.assertFalse(needs_attention(replace(self.row, hidden=True)))
        self.assertFalse(needs_attention(replace(self.row, group='Recent activity')))
        self.assertFalse(needs_attention(None))

    def test_snapshot_removes_only_notice_lines_and_preserves_markdown_and_sha(self):
        result = local_action(self.row, self.session)
        self.assertTrue(result.available)
        self.assertIn('## Resolve the blocker\n**Needs:**', result.body)
        self.assertIn('`abcdef1234567890`', result.body)
        self.assertIn('```sh\nub-agents retry 178\n```', result.body)
        for removed in (ACTION_MARKER, '**Action needed**', '[Claim]', '[Outcome]'):
            self.assertNotIn(removed, result.body)
        self.assertTrue(result.details().endswith('snapshot'))
        no_outcome = NOTICE.replace('[Outcome](https://example.test/outcome)', 'No outcome was reported.')
        self.assertNotIn('[Claim]', comment_body(no_outcome))
        self.assertIn('[Claim](x) is useful context', comment_body(NOTICE + '\n[Claim](x) is useful context'))
        self.assertIn('**Action needed**', comment_body(NOTICE + '\n**Action needed**'))
        tokens = description_parser().parse('[link](https://example.test)\n![image](x)\n<b>HTML</b>')
        self.assertFalse(any(child.type in {'link_open', 'image', 'html_inline'}
                             for token in tokens for child in token.children or []))

    def test_controls_normalization_shortening_and_unknown_snapshot_author(self):
        notice = self.session.data['action_needed']['178']
        notice['text'] = NOTICE.replace('\n', '\r\n') + '\t[bold]\x1b[31m' + 'x' * 3000
        result = local_action(self.row, self.session)
        self.assertEqual(len(result.body), 2048)
        self.assertIn('shortened', result.notice)
        self.assertIn('\n', result.body)
        self.assertIn('\t[bold]', result.body)
        self.assertNotIn('\x1b', result.body)
        for author, error in (('reader', 'write or higher'), ('unknown', 'not verified')):
            notice['author'] = author
            result = local_action(self.row, self.session)
            self.assertFalse(result.available)
            self.assertEqual(result.body, '')
            self.assertIn(error, result.details())

    def test_waiting_uses_row_start_even_when_github_comment_or_history_differs(self):
        now = stamp('2026-10-05T12:36:00Z')
        result = local_action(self.row, self.session)
        metadata, waiting = unblock_metadata(self.row, result, self.session, now)
        self.assertEqual(waiting, 'waiting 24m')
        self.assertIn('worker · parked · waiting 24m · since ', metadata)
        self.assertIn('waiting 25m', unblock_metadata(self.row, result, self.session, now + 60)[0])
        self.session.data['histories'] = {'178': {'runs': [
            {'time': '2026-10-05T11:00:00Z'}, {'time': '2026-10-05T12:30:00Z'}]}}
        row = replace(self.row, state='blocked', reason='Attempt limit exhausted')
        self.assertIn('failed 3/3 · waiting 24m', unblock_metadata(row, ActionComment(), self.session, now)[0])
        self.assertIn('waiting 24m', unblock_metadata(row, replace(result, created_at='2026-10-05T12:30:00Z'),
                                                   self.session, now)[0])
        self.session.data['histories'] = {}
        row = replace(row, data={**row.data, 'waiting_since': None})
        self.assertEqual(unblock_metadata(row, ActionComment(), self.session, now), ('worker · failed 3/3', ''))
        row = replace(row, reason='Previous cleanup was unconfirmed')
        self.assertEqual(unblock_metadata(row, ActionComment(), self.session, now), ('worker · blocked', ''))

    def test_github_uses_latest_trusted_marker_including_approval_notices(self):
        result = parse_response(comments_reply(
            comment(), comment('normal comment'),
            comment(ACTION_MARKER + 'approval-gate-1 -->\n**Action needed**\n\nApprove feedback.',
                    author='OPERATOR', created='2026-10-05T12:13:00Z'),
            comment('prefix ' + NOTICE, created='2026-10-05T12:14:00Z'),
            comment(author='reader', created='2026-10-05T12:15:00Z'),
            comment(author='unknown', created='2026-10-05T12:16:00Z')),
            b'', 0, 1000, 'unblock', AUTHORS)
        self.assertEqual(result.action.body, 'Approve feedback.')
        self.assertEqual(result.action.created_at, '2026-10-05T12:13:00Z')
        self.assertEqual(result.action.author, 'OPERATOR')

    def test_no_trusted_comment_explains_unverified_author_and_no_paging(self):
        for comments, error in (([comment(author='reader')], 'write or higher'),
                                 ([comment(author='unknown')], 'not verified'),
                                 ([dict(comment(), author=None)], 'unreadable'),
                                 ([comment(body='normal')], 'No trusted'),
                                 ([comment()] + [comment(body='normal')] * 100, 'No trusted')):
            result = parse_response(comments_reply(*comments), b'', 0, 1000, 'unblock', AUTHORS)
            self.assertIsNone(result.action)
            self.assertIn(error, result.error)
        self.assertIn('comments(last:100)', COMMENTS_QUERY)
        self.assertNotIn('pageInfo', COMMENTS_QUERY)
        self.assertNotIn('permission', COMMENTS_QUERY)


class UnblockLoadTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.transport = RecordingDescriptionTransport()
        self.loads = DescriptionLoads(self.transport, clock=lambda: self.now)
        self.key = ('example/repo', 178)

    def test_reads_share_one_flight_cooldown_and_explicit_retry(self):
        self.loads.request(self.key)
        self.loads.request(self.key, 'unblock', AUTHORS)
        self.assertEqual(len(self.transport.calls), 1)
        self.transport.response = Response(error='rate limit', reset=1100)
        self.loads.poll()
        self.loads.request(self.key, 'unblock', AUTHORS)
        self.assertEqual(len(self.transport.calls), 1)
        self.now = 1100
        self.loads.poll()
        self.assertEqual(len(self.transport.calls), 1)
        self.loads.request(self.key, 'unblock', AUTHORS)
        self.loads.request(('example/repo', 179))
        self.assertEqual(len(self.transport.calls), 2)
        self.transport.response = Response(error='No trusted comment')
        self.loads.poll()
        for _ in range(10):
            self.loads.get(self.key, 'unblock')
            self.loads.poll()
        self.assertEqual(len(self.transport.calls), 2)
        self.loads.request(self.key, 'unblock', AUTHORS)
        self.transport.response = Response(action=ActionComment(body='Resolve it.', available=True, author='operator'))
        self.loads.poll()
        self.loads.request(self.key, 'unblock', AUTHORS)
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.loads.get(self.key, 'unblock').source, 'GitHub')
        self.assertIn('loaded 0s ago', self.loads.get(self.key, 'unblock').details(self.now))

    def test_both_sources_share_128_item_lru_with_independent_bodies(self):
        for number in range(CACHE_ITEMS + 1):
            key = ('example/repo', number + 1)
            self.loads.remember(key, Response('Title', 'Description'))
            self.loads.remember(key, Response(action=ActionComment(body='Action', available=True)), 'unblock')
        self.assertEqual(len(self.loads.cache), CACHE_ITEMS)
        self.assertIsNone(self.loads.get(('example/repo', 1)))
        self.assertEqual(self.loads.get(('example/repo', 2)).body, 'Description')
        self.assertEqual(self.loads.get(('example/repo', 2), 'unblock').body, 'Action')

    def test_comments_transport_makes_one_bounded_graphql_request(self):
        calls = []
        popen = subprocess.Popen
        def recording(command, **kwargs):
            calls.append(command)
            return popen([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(' +
                          repr(comments_reply(comment(body=NOTICE + 'x' * 9000))) + ')'], **kwargs)
        transport = GhTransport()
        with patch('ub_agents.view_github.subprocess.Popen', side_effect=recording):
            transport.start(*self.key, 'unblock', AUTHORS)
            try:
                deadline = time.monotonic() + 3
                response = None
                while response is None and time.monotonic() < deadline:
                    response = transport.poll()
                    time.sleep(0.01)
                self.assertTrue(response.action.available)
                self.assertEqual(len(response.action.body), 2048)
                self.assertIn('shortened', response.action.notice)
                self.assertEqual(len(calls), 1)
                self.assertIn('query=' + COMMENTS_QUERY, calls[0])
                self.assertIsNone(transport.process)
            finally:
                transport.close()

    def test_comments_transport_enforces_combined_response_limit_and_timeout(self):
        popen = subprocess.Popen
        for oversized in (False, True):
            code = ('import sys; sys.stdout.buffer.write(b"x" * ' + str(RESPONSE_BYTES + 1) + ')' if oversized
                    else 'import time; time.sleep(60)')
            def recording(command, **kwargs):
                return popen([sys.executable, '-c', code], **kwargs)
            now = [0]
            transport = GhTransport(clock=lambda: now[0])
            with patch('ub_agents.view_github.subprocess.Popen', side_effect=recording):
                transport.start(*self.key, 'unblock', AUTHORS)
                owned = transport.process
                try:
                    response = None
                    if not oversized:
                        now[0] = 10
                    deadline = time.monotonic() + 3
                    while response is None and time.monotonic() < deadline:
                        response = transport.poll()
                        time.sleep(0.01)
                    self.assertIn('limit' if oversized else 'timed out', response.error)
                    self.assertIsNotNone(owned.poll())
                finally:
                    transport.close()


class UnblockUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, _, self.state = fixture(self.root)
        self.state['latest_pass']['rows'] += [
            {'item': 178, 'agent': 'worker', 'kind': 'pr', 'title': 'Blocked candidate',
             'state': 'parked', 'reason': 'Approval needed', 'description': {'available': False},
             'waiting_since': '2026-10-05T12:12:00Z'},
            {'item': 179, 'agent': 'worker', 'state': 'blocked', 'reason': 'Attempt limit exhausted',
             'failures': 3, 'max_attempts': 3}]
        self.state['coordination_authors'] = AUTHORS
        self.state['action_needed'] = {'178': {'text': NOTICE, 'author': 'operator',
            'created_at': '2026-10-05T12:12:00Z'}}
        self.path.write_text(json.dumps(self.state))
        self.transport = RecordingDescriptionTransport()
        self.now = stamp('2026-10-05T12:36:00Z')
        self.app = View(self.root, self.path, descriptions=DescriptionLoads(self.transport, clock=lambda: self.now))

    async def ready(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(0.03)
            if condition():
                return
        self.fail('View did not become ready')

    async def test_tab_keys_snapshot_header_markdown_and_selection_refresh_fallback(self):
        app = self.app
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(pilot, lambda: app.local_description is not None)
            await pilot.press('4', 'g')
            self.assertEqual(app.query_one(ItemTabs).active, 'log')
            self.assertFalse(app.unblock_visible)
            app.select('plan:178:worker')
            await pilot.press('4', 'g')
            await self.ready(pilot, lambda: app.query_one(ItemTabs).active == 'unblock')
            self.assertEqual(self.transport.calls, [])
            self.assertIn('1-4 tabs', app.query_one('#status', Static).render().plain)
            self.assertIn('g load', app.query_one('#status', Static).render().plain)
            header = app.query_one('#item_header', Static).render()
            self.assertIn('⌥178 Blocked candidate\nworker · parked · waiting 24m · since ', header.plain)
            waiting = header.plain.index('waiting')
            self.assertEqual(header.get_style_at_offset(waiting).foreground.hex.lower(), '#ff8b7f')
            self.assertTrue(header.get_style_at_offset(waiting).dim)
            self.assertTrue(header.get_style_at_offset(0).bold)
            markdown = app.query_one('#unblock_body', Markdown)
            self.assertIn('**Needs:**', markdown.source)
            self.assertIn('snapshot', app.query_one('#unblock_note', Static).render().plain)
            self.now += 60
            await self.ready(pilot, lambda: 'waiting 25m' in app.query_one('#item_header', Static).render().plain)
            await pilot.press('?')
            self.assertIn('4 on Needs attention', app.screen.message)
            self.assertIn('g on Unblock', app.screen.message)
            await pilot.press('?')
            app.select('plan:179:worker')
            await pilot.press('4')
            self.assertIn('failed 3/3', app.query_one('#item_header', Static).render().plain)
            app.select('plan:12')
            self.assertEqual(app.query_one(ItemTabs).active, 'log')
            self.assertFalse(app.unblock_visible)
            await pilot.press('?')
            self.assertNotIn('g on Unblock', app.screen.message)
            await pilot.press('?')
            app.select('plan:178:worker')
            await pilot.press('4')
            self.state['latest_pass']['rows'][-2]['state'] = 'ready'
            self.path.write_text(json.dumps(self.state))
            await self.ready(pilot, lambda: not app.unblock_visible)
            self.assertEqual(app.query_one(ItemTabs).active, 'log')
            await pilot.press('q')
        app.worker.thread.join(2)
        self.assertTrue(self.transport.closed)

    async def test_github_explicit_read_shared_flight_cache_cooldown_and_trust_revocation(self):
        app = self.app
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(pilot, lambda: app.local_description is not None)
            app.select('plan:179:worker')
            await pilot.press('4')
            await pilot.resize_terminal(120, 36)
            await pilot.pause(0.3)
            self.assertEqual(self.transport.calls, [])
            self.assertIn('press g', app.query_one('#unblock_note', Static).render().plain)
            await pilot.press('g', 'g', '2', 'g')
            self.assertEqual(len(self.transport.calls), 1)
            self.assertEqual(self.transport.calls[0][:3], ('example/repo', 179, 'unblock'))
            self.transport.response = Response(error='rate limit', reset=self.now + 60)
            await self.ready(pilot, lambda: app.descriptions.pending is None)
            await pilot.press('g', '4', 'g')
            self.assertEqual(len(self.transport.calls), 1)
            self.now += 60
            await pilot.pause(0.3)
            self.assertEqual(len(self.transport.calls), 1)
            await pilot.press('g')
            self.transport.response = parse_response(comments_reply(comment()), b'', 0, self.now, 'unblock', AUTHORS)
            await self.ready(pilot, lambda: app.current_action().available)
            self.assertIn('GitHub · loaded 0s ago', app.query_one('#unblock_note', Static).render().plain)
            await pilot.press('1', '4', 'g')
            self.assertEqual(len(self.transport.calls), 2)
            self.state['coordination_authors'] = {}
            self.path.write_text(json.dumps(self.state))
            await self.ready(pilot, lambda: not app.current_action().available and
                             'not verified' in app.query_one('#unblock_note', Static).render().plain)
            self.assertEqual(app.query_one('#unblock_body', Markdown).source, '')
            self.assertIn('not verified', app.query_one('#unblock_note', Static).render().plain)
            await pilot.press('g', 'q')
        app.worker.thread.join(2)
        self.assertTrue(self.transport.closed)
