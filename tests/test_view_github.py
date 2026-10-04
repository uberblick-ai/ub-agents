import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests.support import RecordingDescriptionTransport
from ub_agents.view_github import (CACHE_ITEMS, QUERY, REQUEST_SECONDS, DescriptionLoads,
                                   GhTransport, Response, parse_response)


def reply(title='Loaded title', body='Loaded body'):
    return json.dumps({'data': {'repository': {'issueOrPullRequest': {'title': title, 'body': body}}}}).encode()


class DescriptionLoadTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.transport = RecordingDescriptionTransport()
        self.loads = DescriptionLoads(self.transport, clock=lambda: self.now)
        self.key = ('example/repo', 115)

    def test_success_cache_single_flight_and_no_session_load_cap(self):
        self.loads.request(self.key)
        self.loads.request(self.key)
        self.loads.request(('example/repo', 116))
        self.assertEqual(self.transport.calls, [self.key])
        for _ in range(30):
            self.loads.poll()
        self.assertEqual(self.transport.calls, [self.key])
        self.transport.response = Response('title', 'body')
        self.loads.poll()
        for _ in range(30):
            self.assertEqual(self.loads.get(self.key).body, 'body')
            self.loads.request(self.key)
        self.assertEqual(self.transport.calls, [self.key])
        for number in range(200, 200 + CACHE_ITEMS + 5):
            key = ('example/repo', number)
            self.loads.request(key)
            self.transport.response = Response('title', 'body')
            self.loads.poll()
        self.assertEqual(len(self.loads.cache), CACHE_ITEMS)
        self.assertGreater(len(self.transport.calls), 20)
        self.assertNotIn(self.key, self.loads.cache)

    def test_failures_are_cached_and_only_explicit_retry_calls(self):
        self.loads.request(self.key)
        self.transport.response = Response(error='Access denied')
        self.loads.poll()
        for _ in range(50):
            self.loads.get(self.key)
            self.loads.poll()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertIn('Access denied', self.loads.get(self.key).display())
        self.loads.request(self.key)
        self.transport.response = Response('title', 'body')
        self.loads.poll()
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(self.loads.get(self.key).available)

    def test_rate_limit_prevents_all_loads_and_retries_until_reset(self):
        self.loads.request(self.key)
        self.transport.response = Response(error='rate limit', reset=1100)
        self.loads.poll()
        for key in (self.key, ('example/repo', 116)):
            self.loads.request(key)
        self.now = 1099
        self.loads.request(self.key)
        self.assertEqual(len(self.transport.calls), 1)
        self.now = 1100
        self.loads.poll()
        self.assertEqual(len(self.transport.calls), 1)
        self.loads.request(self.key)
        self.assertEqual(len(self.transport.calls), 2)

    def test_invalid_targets_and_missing_gh_are_local_failures(self):
        for repo, item in (('../repo', 1), ('owner/repo/extra', 1), ('example/repo', True),
                           ('example/repo', '?'), (None, 1)):
            self.assertIsNone(self.loads.key(repo, item))
        self.loads.request(None)
        self.assertEqual(self.transport.calls, [])
        with patch.object(self.transport, 'start', side_effect=FileNotFoundError('gh missing')):
            self.loads.request(self.key)
        self.assertIn('gh missing', self.loads.get(self.key).error)


class GhTransportTests(unittest.TestCase):
    def test_query_only_selected_title_body_and_response_shortening(self):
        transport = GhTransport()
        process = subprocess.Popen
        calls = []
        def recording(command, **kwargs):
            calls.append(command)
            return process([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(' + repr(reply(body='x' * 9000)) + ')'], **kwargs)
        with patch('ub_agents.view_github.subprocess.Popen', side_effect=recording):
            transport.start('example/repo', 115)
            try:
                deadline = time.monotonic() + 3
                result = None
                while result is None and time.monotonic() < deadline:
                    result = transport.poll()
                    time.sleep(0.01)
                self.assertIsNotNone(result)
                self.assertIn('[description shortened]', result.body)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], ['gh', 'api', 'graphql', '--hostname', 'github.com', '--include',
                                          '-f', 'query=' + QUERY, '-f', 'owner=example', '-f', 'repo=repo', '-F', 'number=115'])
                self.assertNotIn('comments', QUERY)
                self.assertNotIn('history', QUERY)
                self.assertIsNone(transport.process)
            finally:
                transport.close()

    def test_pending_hung_process_timeout_and_close_reap_owned_request(self):
        process = subprocess.Popen
        def hung(command, **kwargs):
            return process([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        for timeout in (False, True):
            with self.subTest(timeout=timeout), patch('ub_agents.view_github.subprocess.Popen', side_effect=hung):
                now = [0]
                transport = GhTransport(clock=lambda: now[0])
                transport.start('example/repo', 115)
                owned = transport.process
                try:
                    self.assertIsNone(transport.poll())
                    started = time.monotonic()
                    if timeout:
                        now[0] = REQUEST_SECONDS
                        self.assertIn('timed out', transport.poll().error)
                    else:
                        transport.close()
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertIsNotNone(owned.poll())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned.pid, 0)
                    self.assertIsNone(transport.process)
                finally:
                    transport.close()

    def test_rate_limit_headers_graphql_errors_and_ordinary_failure(self):
        for stdout in (
            b'HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Reset: 1100\r\nX-Ratelimit-Remaining: 0\r\n\r\n{"message":"API rate limit exceeded"}',
            b'HTTP/2.0 200 OK\nX-Ratelimit-Reset: 1100\n\n{"errors":[{"type":"RATE_LIMITED","message":"rate limit"}]}',
            b'HTTP/2.0 429 Too Many Requests\nRetry-After: 100\n\n{}',
        ):
            result = parse_response(stdout, b'', 1, 1000)
            self.assertEqual(result.reset, 1100)
            self.assertIn('rate limit', result.error)
        self.assertEqual(parse_response(b'', b'ordinary failure', 1, 1000).error, 'ordinary failure')
        self.assertIn('no readable', parse_response(b'{broken', b'', 0, 1000).error)
        self.assertEqual(parse_response(reply(body=''), b'', 0, 1000).body, '')


if __name__ == '__main__':
    unittest.main()
