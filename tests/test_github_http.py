from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import traceback
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit

from ub_agents.cleanup_control import CleanupStopped
from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.github import GitHub
from ub_agents.github_http import Token
from ub_agents.logs.launch_log import launch_output
from ub_agents.loop import Loop
from ub_agents.github import response_parts
from ub_agents.records import records
from ub_agents.run_config import run_directory
from ub_agents.run_planning import RunPlanning, _Cancelled
from ub_agents.startup_cleanup import StartupCleanup
from tests.test_etags import CoordinationRunner, SequenceRunner, response
from tests.support import FakeGitHub, agent, config, issue, stub_refresh


class Reply:
    def __init__(self, status=200, payload=None, headers=(), *, will_close=False):
        self.status, self.will_close = status, will_close
        self.payload = json.dumps({} if payload is None else payload).encode()
        self.headers = headers

    def read(self):
        return self.payload

    def getheaders(self):
        return self.headers


class Connection:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def request(self, method, target, **kwargs):
        self.calls.append((method, target, kwargs))

    def getresponse(self):
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


class GitHubHTTPTests(unittest.TestCase):
    def setUp(self):
        self.token = Token()
        self.enterContext(patch('ub_agents.github.token', self.token))
        self.enterContext(patch('ub_agents.github_http.token', self.token))
        self.run = self.enterContext(patch('ub_agents.github_http.subprocess.run', return_value=
                                          subprocess.CompletedProcess([], 0, 'sentinel-credential\n', '')))
        self.connect = self.enterContext(patch('ub_agents.github_http.http.client.HTTPSConnection',
                                              side_effect=AssertionError('Unscripted HTTP connection')))
        self.sleep = self.enterContext(patch('ub_agents.github.sleep'))

    def connection(self, *responses):
        connection = Connection(*responses)
        self.connect.side_effect = None
        self.connect.return_value = connection
        return connection

    def test_rest_graphql_and_writes_reuse_connection_and_read_token_once_across_clients(self):
        connection = self.connection(Reply(payload={'login': 'operator'}), Reply(), Reply(204), Reply())
        github = GitHub('org/project')
        self.assertEqual(github.actor(), 'operator')
        github.request('https://api.github.com/graphql', 'POST', {'query': 'query { viewer { login } }'})
        github.request('repos/org/project/issues/1/labels/ready', 'DELETE')
        self.connect.assert_called_once_with('api.github.com', timeout=20)
        self.assertEqual([c[:2] for c in connection.calls], [
            ('GET', '/user'), ('POST', '/graphql'), ('DELETE', '/repos/org/project/issues/1/labels/ready')])
        sent = connection.calls[1][2]
        self.assertEqual(json.loads(sent['body']), {'query': 'query { viewer { login } }'})
        self.assertEqual(sent['headers']['Content-Type'], 'application/json')
        self.assertEqual(sent['headers']['Authorization'], 'Bearer sentinel-credential')
        GitHub('other/project').request('user')
        self.run.assert_called_once_with(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                        capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual((github.gh_calls, github.rest_requests, github.quota_requests, github.graphql_calls),
                         (3, 2, 2, 1))

    def test_dropped_connection_fails_once_and_next_request_reconnects_without_replaying_write(self):
        first, second = Connection(Reply(), http.client.RemoteDisconnected('dropped')), Connection(Reply())
        self.connect.side_effect = [first, second]
        github = GitHub('org/project')
        github.request('user')
        with self.assertRaises(GitHubError) as raised:
            github.request('resource', 'POST', {'body': 'write'})
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(first.closed)
        self.assertEqual(len(first.calls), 2)
        github.request('user')
        self.assertEqual(len(second.calls), 1)
        self.assertEqual((github.gh_calls, github.quota_requests), (3, 2))

    def test_get_connection_failure_or_timeout_retries_on_a_fresh_connection(self):
        for failure in (OSError('connection refused'), TimeoutError('timed out'),
                        http.client.RemoteDisconnected('stale keep-alive')):
            with self.subTest(failure=type(failure)):
                first, second = Connection(failure), Connection(Reply(payload={'login': 'operator'}))
                self.connect.side_effect = [first, second]
                github = GitHub('org/project')
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(github.actor(), 'operator')
                self.assertTrue(first.closed)
                self.assertFalse(second.closed)
                self.assertEqual([len(c.calls) for c in (first, second)], [1, 1])
                self.assertEqual((github.gh_calls, github.rest_requests, github.quota_requests), (2, 2, 1))

    def test_get_stops_after_four_failures_with_backoff_and_one_log_line_per_retry(self):
        connections = [Connection(OSError('sentinel-credential')) for _ in range(4)]
        self.connect.side_effect = connections
        github = GitHub('org/project')
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            root = Path(directory)
            with launch_output(root), self.assertRaises(GitHubError) as raised:
                github.request('repos/org/project/issues/1/comments')
            lines = (root / '.ub-agents' / 'launch.log').read_text().splitlines()
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(4)])
        self.assertEqual(len(lines), 3)
        for line, attempt, delay in zip(lines, (2, 3, 4), (1, 2, 4)):
            self.assertIn(f'Retrying GitHub GET repos/org/project/issues/1/comments: '
                          f'attempt {attempt}/4 in {delay}s', line)
        self.assertEqual([len(c.calls) for c in connections], [1, 1, 1, 1])
        self.assertTrue(all(c.closed for c in connections))
        self.assertEqual((github.gh_calls, github.rest_requests, github.quota_requests), (4, 4, 0))
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.probe_reason, 'connection failed')
        self.assertNotIn('sentinel-credential', ''.join(traceback.format_exception(raised.exception)))

    def test_writes_and_graphql_connection_failures_are_not_replayed(self):
        cases = [('resource', method) for method in ('POST', 'PATCH', 'PUT', 'DELETE')]
        cases += [('graphql', 'POST'), ('https://api.github.com/graphql', 'GET')]
        for endpoint, method in cases:
            with self.subTest(endpoint=endpoint, method=method):
                connection = self.connection(OSError('connection failed'))
                with self.assertRaises(GitHubError):
                    GitHub('org/project').request(endpoint, method)
                self.assertEqual(len(connection.calls), 1)
                self.assertTrue(connection.closed)
        self.sleep.assert_not_called()

    def test_get_failure_in_gh_fallback_is_not_replayed(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 1, '', ''),
                                subprocess.TimeoutExpired('gh', 20)]
        with self.assertRaises(GitHubError):
            GitHub('org/project').request('user')
        self.assertEqual(self.run.call_count, 2)
        self.connect.assert_not_called()
        self.sleep.assert_not_called()

    def test_401_refreshes_changed_token_and_retries_once_counting_both_responses(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0, 'old-credential', ''),
                                subprocess.CompletedProcess([], 0, 'new-credential', '')]
        connection = self.connection(Reply(401), Reply(payload={'login': 'operator'}), Reply())
        github = GitHub('org/project')
        self.assertEqual(github.actor(), 'operator')
        github.request('user')
        self.assertEqual([c[2]['headers']['Authorization'] for c in connection.calls],
                         ['Bearer old-credential', 'Bearer new-credential', 'Bearer new-credential'])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual((github.gh_calls, github.quota_requests), (3, 3))

    def test_401_with_unchanged_unreadable_or_rejected_replacement_stops(self):
        for replacement, second_status, attempts in (('sentinel-credential', 200, 1), ('', 200, 1),
                                                      ('different', 401, 2)):
            with self.subTest(replacement=replacement):
                self.setUp_tokens()
                self.run.side_effect = [subprocess.CompletedProcess([], 0, 'sentinel-credential', ''),
                                        subprocess.CompletedProcess([], 0, replacement, '')]
                connection = self.connection(Reply(401), Reply(second_status))
                github = GitHub('org/project')
                with self.assertRaises(GitHubError) as raised:
                    github.request('user')
                self.assertFalse(raised.exception.retryable)
                self.assertIn('Authentication failed (HTTP 401)', str(raised.exception))
                self.assertEqual(self.run.call_count, 2)
                self.assertEqual(len(connection.calls), attempts)

    def setUp_tokens(self):
        # Each subcase models a new launcher process.
        self.token.__init__()
        self.run.reset_mock()

    def test_failed_or_empty_token_lookup_uses_unchanged_gh_protocol_and_caches_failure(self):
        for lookup in (subprocess.CompletedProcess([], 1, '', 'secret auth diagnostic'),
                       subprocess.CompletedProcess([], 0, ' \n', ''), OSError('cannot run'),
                       subprocess.TimeoutExpired('gh', 20)):
            with self.subTest(lookup=type(lookup)):
                self.setUp_tokens()
                runner = SequenceRunner(response(payload={'login': 'operator'}, etag='"user"'), response(304))

                def run(command, **kwargs):
                    if command[1:3] == ['auth', 'token']:
                        if isinstance(lookup, Exception):
                            raise lookup
                        return lookup
                    return runner(command, **kwargs)

                self.run.side_effect = run
                github = GitHub('org/project')
                self.assertEqual(github.actor(), 'operator')
                self.assertEqual(github.actor(), 'operator')
                self.assertEqual(self.run.call_count, 3)
                self.assertEqual((github.gh_calls, github.quota_requests, github.not_modified_responses), (2, 1, 1))
                self.assertEqual(runner.calls[0][0], ('gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                                                    '-H', 'Accept: application/vnd.github+json', '--include', 'user'))
                self.assertEqual(runner.calls[1][0][-2:], ('-H', 'If-None-Match: "user"'))
        self.connect.assert_not_called()

    def test_injected_recording_runner_does_not_read_token_or_open_connection(self):
        runner = SequenceRunner(response(payload={'login': 'operator'}))
        self.assertEqual(GitHub('org/project', runner).actor(), 'operator')
        self.run.assert_not_called()
        self.connect.assert_not_called()

    def test_fallback_401_rereads_token_and_can_retry_over_http(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 1, '', ''), response(401),
                                subprocess.CompletedProcess([], 0, 'recovered', '')]
        connection = self.connection(Reply(payload={'login': 'operator'}))
        github = GitHub('org/project')
        self.assertEqual(github.actor(), 'operator')
        self.assertEqual([c.args[0][1] for c in self.run.call_args_list], ['auth', 'api', 'auth'])
        self.assertEqual(connection.calls[0][2]['headers']['Authorization'], 'Bearer recovered')
        self.assertEqual((github.gh_calls, github.quota_requests), (2, 2))

    def test_http_etags_304_refetch_and_resource_quotas_keep_existing_accounting(self):
        core = [('X-RateLimit-Resource', 'core'), ('X-RateLimit-Remaining', '4000')]
        gql = [('X-RateLimit-Resource', 'graphql'), ('X-RateLimit-Remaining', '500')]
        connection = self.connection(Reply(payload={'login': 'operator'}, headers=core + [('ETag', '"one"')]),
                                     Reply(304, headers=core + [('ETag', '"two"')]), Reply(304), Reply(),
                                     Reply(headers=gql), Reply(304), Reply(304))
        github = GitHub('org/project')
        github.request('user')['login'] = 'mutated'
        self.assertEqual(github.actor(), 'operator')
        self.assertEqual(github.request('uncached'), {})
        github.request('graphql', 'POST', {'query': 'query { viewer { login } }'})
        self.assertEqual((github.gh_calls, github.quota_requests, github.not_modified_responses,
                          github.graphql_calls), (5, 2, 2, 1))
        self.assertEqual(github.resource_quotas['core']['x-ratelimit-remaining'], '4000')
        self.assertEqual(github.resource_quotas['graphql']['x-ratelimit-remaining'], '500')
        self.assertEqual([c[2]['headers'].get('If-None-Match') for c in connection.calls],
                         [None, '"one"', None, None, None])
        with self.assertRaisesRegex(GitHubError, 'after refetch'):
            github.request('still-uncached')
        self.assertEqual(github.gh_calls, 7)

    def test_writes_graphql_and_since_reads_are_unconditional(self):
        connection = self.connection(*(Reply(headers=[('ETag', '"etag"')]) for _ in range(8)))
        github = GitHub('org/project')
        for _ in range(2):
            github.request('resource?since=cursor')
            github.request('resource', 'POST', {'body': 'value'})
            github.request('graphql', 'POST', {'query': 'query { viewer { login } }'})
            github.request('https://api.github.com/graphql')
        self.assertTrue(all('If-None-Match' not in c[2]['headers'] for c in connection.calls))
        self.assertEqual(github._etag_cache, {})

    def test_rate_limits_server_failures_and_stopping_errors_match_gh_policy(self):
        cases = [
            (403, [('X-RateLimit-Remaining', '0'), ('X-RateLimit-Reset', '4600')], '', True, True, 4605),
            (403, [('Retry-After', '12')], '', True, True, 1012),
            (429, [], 'secondary rate limit', True, True, 1060),
            (500, [], 'server error', True, False, None),
            (403, [], 'Resource not accessible', False, False, None),
            (404, [], 'not found', False, False, None),
            (422, [], 'invalid', False, False, None),
        ]
        for status, headers, message, retryable, limited, reset in cases:
            with self.subTest(status=status, message=message):
                connection = self.connection(Reply(status, {'message': message}, headers))
                github = GitHub('org/project')
                with patch('ub_agents.github.timestamp', return_value=1000), self.assertRaises(GitHubError) as raised:
                    github.request('user')
                self.assertEqual((raised.exception.retryable, raised.exception.rate_limited,
                                  raised.exception.reset_at), (retryable, limited, reset))
                self.assertEqual(github.rate_limited, limited)
                self.assertEqual(github.quota_requests, 1)
                self.assertEqual(len(connection.calls), 1)
        self.sleep.assert_not_called()

    def test_redirected_reads_stop_without_returning_or_caching_redirect_body(self):
        for status in (300, 301, 302, 303, 305, 307, 308, 399):
            with self.subTest(status=status):
                connection = self.connection(Reply(status, {'message': 'Moved Permanently'}, [
                    ('Location', 'https://api.github.com/repositories/123'),
                    ('ETag', '"redirect"'), ('X-RateLimit-Remaining', '100'),
                    ('X-RateLimit-Resource', 'core')]))
                github = GitHub('org/project')
                with self.assertRaises(GitHubError) as raised:
                    github.request('repos/org/project/issues/5')
                self.assertIn(f'redirect (HTTP {status})', str(raised.exception))
                self.assertIn('repository configuration', str(raised.exception))
                self.assertFalse(raised.exception.retryable)
                self.assertFalse(raised.exception.rate_limited)
                self.assertEqual(github._etag_cache, {})
                self.assertEqual(github.quota_headers['x-ratelimit-remaining'], '100')
                self.assertEqual((github.gh_calls, github.quota_requests), (1, 1))
                self.assertEqual(len(connection.calls), 1)

    def test_redirected_writes_stop_without_reporting_success_or_replaying(self):
        for method in ('POST', 'PATCH', 'DELETE'):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    reply = Reply(status, {'message': 'Temporary Redirect'}, [
                        ('Location', 'https://api.github.com/repositories/123/issues/5/comments')])
                    if method == 'DELETE':
                        reply.payload = b''
                    connection = self.connection(reply)
                    github = GitHub('org/project')
                    with self.assertRaises(GitHubError) as raised:
                        github.request('repos/org/project/issues/5/comments', method)
                    self.assertIn(f'redirect (HTTP {status})', str(raised.exception))
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(len(connection.calls), 1)
                    self.assertEqual((github.gh_calls, github.quota_requests), (1, 1))

    def test_transport_timeout_and_interrupt_discard_connection_without_credential_in_diagnostics(self):
        for failure in (TimeoutError('sentinel-credential'), OSError('sentinel-credential'),
                        http.client.BadStatusLine('sentinel-credential'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure)):
                connection = self.connection(failure)
                github = GitHub('org/project')
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    try:
                        github.request('resource', 'POST', {'body': 'write'})
                    except (GitHubError, KeyboardInterrupt) as exc:
                        output.write(''.join(traceback.format_exception(exc)))
                        if isinstance(exc, GitHubError):
                            self.assertTrue(exc.retryable)
                    else:
                        self.fail('request succeeded')
                self.assertNotIn('sentinel-credential', output.getvalue())
                self.assertTrue(connection.closed)
                self.assertEqual((github.gh_calls, github.quota_requests), (1, 0))

    def test_reflected_credentials_are_redacted_from_response_errors_and_output(self):
        self.connection(Reply(403, {'message': 'sentinel-credential'}))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(GitHubError) as raised:
                GitHub('org/project').request('user')
            print(raised.exception)
            print(''.join(traceback.format_exception(raised.exception)))
        self.assertNotIn('sentinel-credential', output.getvalue())
        self.assertIn('[redacted]', str(raised.exception))

    def test_truncated_body_keeps_status_and_quota_without_replaying(self):
        for method, status, retryable in (('GET', 200, True), ('GET', 403, False), ('GET', 500, True),
                                          ('POST', 200, True), ('POST', 403, False), ('POST', 500, True)):
            with self.subTest(method=method, status=status):
                reply = Reply(status, headers=[('X-RateLimit-Remaining', '100'),
                                               ('X-RateLimit-Resource', 'core')])
                reply.read = Mock(side_effect=http.client.IncompleteRead(b'sentinel-credential'))
                connection = self.connection(reply)
                github = GitHub('org/project')
                with self.assertRaises(GitHubError) as raised:
                    github.request('resource', method)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn('sentinel-credential', str(raised.exception))
                self.assertEqual(github.quota_headers['x-ratelimit-remaining'], '100')
                self.assertEqual((github.gh_calls, github.quota_requests), (1, 1))
                self.assertEqual(len(connection.calls), 1)
                self.assertTrue(connection.closed)
        self.sleep.assert_not_called()

    def test_server_connection_close_opens_a_new_connection_on_next_request(self):
        first, second = Connection(Reply(will_close=True)), Connection(Reply())
        self.connect.side_effect = [first, second]
        github = GitHub('org/project')
        github.request('user')
        github.request('user')
        self.assertTrue(first.closed)
        self.assertEqual([len(c.calls) for c in (first, second)], [1, 1])

    def test_ctrl_c_during_token_lookup_remains_keyboard_interrupt(self):
        for code in (-signal.SIGINT, 128 + signal.SIGINT):
            with self.subTest(code=code):
                self.setUp_tokens()
                self.run.side_effect = None
                self.run.return_value = subprocess.CompletedProcess([], code, '', '')
                with self.assertRaises(KeyboardInterrupt):
                    GitHub('org/project').request('user')
        self.connect.assert_not_called()

    def test_http_interrupt_unwinds_when_launcher_handler_defers_sigint(self):
        handler = Mock()
        previous = signal.signal(signal.SIGINT, handler)
        try:
            connection = self.connection()
            def interrupted():
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            connection.getresponse = interrupted
            with self.assertRaises(KeyboardInterrupt):
                GitHub('org/project').request('user')
            handler.assert_called_once_with(signal.SIGINT, None)
            self.assertIs(signal.getsignal(signal.SIGINT), handler)
            self.assertTrue(connection.closed)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_ctrl_c_during_retry_wait_unwinds_with_deferred_launcher_handler(self):
        handler = Mock()
        previous = signal.signal(signal.SIGINT, handler)
        try:
            connection = self.connection(OSError('connection failed'))
            self.sleep.side_effect = lambda delay: signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                GitHub('org/project').request('user')
            handler.assert_called_once_with(signal.SIGINT, None)
            self.assertIs(signal.getsignal(signal.SIGINT), handler)
            self.assertEqual(len(connection.calls), 1)
            self.assertTrue(connection.closed)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_cancellation_during_backoff_prevents_next_connection(self):
        stop = threading.Event()
        self.sleep.side_effect = lambda delay: stop.set()
        connection = self.connection(OSError('connection failed'))

        def checkpoint():
            if stop.is_set():
                raise _Cancelled

        github = GitHub('org/project', before_request=checkpoint)
        with redirect_stderr(io.StringIO()), self.assertRaises(_Cancelled):
            github.request('user')
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(github.gh_calls, 1)
        self.assertTrue(connection.closed)

    def test_planning_and_cleanup_use_private_connections_and_stop_before_next_request(self):
        connections = []
        barrier = threading.Barrier(3)

        def connect(*args, **kwargs):
            connection = Connection(Reply(), Reply())
            connections.append(connection)
            barrier.wait(timeout=5)
            return connection

        self.connect.side_effect = connect
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(Path(directory))
            github = GitHub('org/project')
            planning = RunPlanning(Loop(cfg, github, 'operator'), 0)
            cleanup = StartupCleanup(cfg, github, 'operator', threading.Event())
            planned = planning.planner.github.github.github
            clients = [github, planned, cleanup.github]

            def read_twice(client):
                client.request('user')
                client.request('user')

            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(read_twice, clients))
            self.assertEqual(len(connections), 3)
            self.assertEqual([len(c.calls) for c in connections], [2, 2, 2])
            self.assertEqual([c.gh_calls for c in clients], [2, 2, 2])
            self.run.assert_called_once()
            planning.cancel()
            cleanup.close()
            for client, failure in ((planned, _Cancelled), (cleanup.github, CleanupStopped)):
                with self.assertRaises(failure):
                    client.request('user')
                self.assertEqual(client.gh_calls, 2)

    def test_cancellation_prevents_401_retry_and_304_refetch(self):
        for status in (401, 304):
            with self.subTest(status=status):
                self.setUp_tokens()
                self.run.side_effect = [subprocess.CompletedProcess([], 0, 'old', ''),
                                        subprocess.CompletedProcess([], 0, 'new', '')]
                reply = Reply(status)
                stop = threading.Event()
                reply.read = lambda: (stop.set(), b'{}')[1]
                connection = self.connection(reply)

                def checkpoint():
                    if stop.is_set():
                        raise _Cancelled

                github = GitHub('org/project', before_request=checkpoint)
                with self.assertRaises(_Cancelled):
                    github.request('user')
                self.assertEqual(len(connection.calls), 1)
                self.assertEqual(github.gh_calls, 1)

    def coordination_http(self, root):
        runner = CoordinationRunner(root, True)
        runner.store = FakeGitHub(issue())
        own, connections = [], []

        def connect(*args, **kwargs):
            connection = Connection()
            connections.append(connection)
            def getresponse():
                method, target, sent = connection.calls[-1]
                endpoint = target.lstrip('/')
                command = ['gh', 'api', '--hostname', 'github.com', '--method', method,
                           '-H', 'Accept: application/vnd.github+json', '--include', endpoint]
                if 'If-None-Match' in sent['headers']:
                    command += ['-H', 'If-None-Match: ' + sent['headers']['If-None-Match']]
                wire = runner(command, input=sent['body'].decode() if sent['body'] is not None else None)
                status, headers, payload = response_parts(wire.stdout)
                own.append((endpoint == 'graphql', status))
                reply = Reply(status, headers=list(headers.items()))
                reply.payload = payload.encode()
                return reply

            connection.getresponse = getresponse
            return connection

        self.connect.side_effect = connect
        return runner, GitHub('org/project'), own, connections

    def test_finalization_retries_get_and_preserves_lease_if_all_attempts_fail(self):
        stub_refresh(self)
        for failure_count in (1, 4):
            with self.subTest(failure_count=failure_count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner, github, own, connections = self.coordination_http(root)
                role = agent(root, outcomes={'done': {'add': ('needs-review',), 'remove': ()}})
                loop = Loop(config(root, role), github, 'operator', output=lambda line: None)
                finished, failures = [], []
                original_dispatch = runner.dispatch

                def dispatch(method, endpoint, data):
                    if (finished and method == 'GET'
                            and urlsplit(endpoint).path == 'repos/org/project/issues/1/comments'
                            and len(failures) < failure_count):
                        failures.append(endpoint)
                        raise OSError('stale connection after execution')
                    return original_dispatch(method, endpoint, data)

                def execute(*args, **kwargs):
                    loop.coordinator.report(loop.github.lease, 'success', 'Finished', outcome='done')
                    finished.append(True)
                    return 0

                with patch.object(runner, 'dispatch', side_effect=dispatch), \
                        patch('ub_agents.assignment_run.supervise', side_effect=execute), \
                        redirect_stderr(io.StringIO()):
                    if failure_count == 1:
                        self.assertTrue(loop.tick())
                    else:
                        with self.assertRaisesRegex(LostOwnership, 'HTTPS connection failed') as raised:
                            loop.tick()
                        self.assertIsInstance(raised.exception.__cause__, GitHubError)
                lease = records(runner.store.comments(1), 'operator')[0]
                if failure_count == 1:
                    self.assertEqual(lease['result'], 'success')
                    self.assertIn('needs-review', runner.store.item(1).labels)
                    self.assertNotIn('ready', runner.store.item(1).labels)
                else:
                    self.assertIsNone(lease.get('result'))
                    self.assertNotIn('released_at', lease)
                    self.assertGreater(loop.coordinator.deadline(lease), loop.coordinator.clock())
                    self.assertIn('ready', runner.store.item(1).labels)
                    self.assertNotIn('needs-review', runner.store.item(1).labels)
                self.assertEqual(len(failures), failure_count)
                self.assertEqual(sum(c.closed for c in connections), failure_count)

    def test_http_requests_keep_pass_and_released_event_counter_names_and_values(self):
        stub_refresh(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, github, own, connections = self.coordination_http(root)
            output, boundaries = [], {}
            loop = Loop(config(root), github, 'operator', output=output.append)
            claim, release = loop.coordinator.claim, loop.coordinator.release

            def claiming(*args, **kwargs):
                boundaries['claim'] = len(own)
                return claim(*args, **kwargs)

            def releasing(*args, **kwargs):
                result = release(*args, **kwargs)
                boundaries['release'] = len(own)
                return result

            def execute(*args, **kwargs):
                self.assertNotIn('sentinel-credential', str(args[2]))
                loop.coordinator.report(loop.github.lease, 'success', 'Finished', outcome='done')
                return 0

            with patch.object(loop.coordinator, 'claim', side_effect=claiming), \
                    patch.object(loop.coordinator, 'release', side_effect=releasing), \
                    patch('ub_agents.assignment_run.supervise', side_effect=execute):
                self.assertTrue(loop.tick())
            lease = records(runner.store.comments(1), 'operator')[0]
            events = [json.loads(line) for line in (run_directory(root, lease['run']) / 'events.jsonl')
                      .read_text().splitlines()]
            released = next(event for event in events if event['event'] == 'released')
            calls = own[boundaries['claim']:boundaries['release']]
            self.assertEqual(released['github_requests'], {
                'gh_calls': len(calls), 'quota_requests': sum(not gql and status != 304 for gql, status in calls),
                'not_modified_responses': sum(status == 304 for _, status in calls),
                'graphql_calls': sum(gql for gql, _ in calls)})
            self.assertTrue(any('gh calls=' in line and 'GraphQL calls=' in line for line in output))
            self.assertNotIn('sentinel-credential', '\n'.join(output) + json.dumps(events))
            for path in run_directory(root, lease['run']).rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'sentinel-credential', path.read_bytes())
            self.run.assert_called_once()
