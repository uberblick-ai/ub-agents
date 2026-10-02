from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from ub_agents.config import Priority, Queue
from ub_agents.errors import GitHubError
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.records import records
from tests.support import FakeGitHub, RecordingRunner, agent, config, issue, pr, stub_refresh


def response(status=200, payload=None, etag=None, returncode=None):
    headers = f'ETag: {etag}\r\n' if etag is not None else ''
    headers += 'X-RateLimit-Remaining: 4000\r\n'
    wire = '' if payload is None else json.dumps(payload)
    code = int(status >= 400 or status == 304) if returncode is None else returncode
    return subprocess.CompletedProcess([], code, f'HTTP/2.0 {status}\r\n{headers}\r\n{wire}',
                                       'gh: HTTP 304' if status == 304 else '')


def validator(command):
    return next((part.split(': ', 1)[1] for part in command if part.startswith('If-None-Match: ')), None)


class SequenceRunner(RecordingRunner):
    def __init__(self, *responses):
        super().__init__(Path('/synthetic'))
        self.pending = list(responses)

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        result = self.pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ETagTests(unittest.TestCase):
    def test_304_returns_stored_payload_before_checking_gh_exit_status(self):
        for code in (0, 1):
            with self.subTest(returncode=code):
                runner = SequenceRunner(response(payload={'login': 'operator'}, etag='W/"old"'),
                                        response(304, etag='"old"', returncode=code), response(304))
                github = GitHub('org/project', runner)
                first = github.request('user')
                first['login'] = 'mutated by caller'
                self.assertEqual(github.actor(), 'operator')
                self.assertEqual(github.actor(), 'operator')
                self.assertEqual([validator(c) for c, _ in runner.calls], [None, 'W/"old"', '"old"'])
                self.assertEqual(github.quota_requests, 1)
                self.assertEqual(github.quota_headers['x-ratelimit-remaining'], '4000')

    def test_changed_response_replaces_payload_and_next_validator(self):
        runner = SequenceRunner(response(payload={'version': 1}, etag='"one"'),
                                response(payload={'version': 2}, etag='"two"'), response(304))
        github = GitHub('org/project', runner)
        self.assertEqual([github.request('resource') for _ in range(3)],
                         [{'version': 1}, {'version': 2}, {'version': 2}])
        self.assertEqual([validator(c) for c, _ in runner.calls], [None, '"one"', '"two"'])
        self.assertEqual(github.quota_requests, 2)

    def test_304_without_entry_refetches_once_unconditionally(self):
        runner = SequenceRunner(response(304), response(payload={'fresh': True}, etag='"new"'), response(304))
        github = GitHub('org/project', runner)
        self.assertEqual(github.request('resource'), {'fresh': True})
        self.assertEqual(github.request('resource'), {'fresh': True})
        self.assertEqual([validator(c) for c, _ in runner.calls], [None, None, '"new"'])
        self.assertEqual(github.quota_requests, 1)
        repeated = SequenceRunner(response(304), response(304))
        with self.assertRaisesRegex(GitHubError, 'after refetch'):
            GitHub('org/project', repeated).request('resource')
        self.assertEqual(len(repeated.calls), 2)

    def test_each_query_and_page_has_its_own_validator(self):
        first = [{'page': 1}] * 100
        last = [{'page': 2}]
        runner = SequenceRunner(response(payload=first, etag='"page1"'),
                                response(payload=last, etag='"page2"'),
                                response(payload=[], etag='"closed"'),
                                response(304), response(304), response(304))
        github = GitHub('org/project', runner)
        for _ in range(2):
            self.assertEqual(github.request('items?state=open', paginate=True), first + last)
            self.assertEqual(github.request('items?state=closed&page=1', array=True), [])
        self.assertEqual([validator(c) for c, _ in runner.calls],
                         [None, None, None, '"page1"', '"page2"', '"closed"'])
        self.assertEqual(github.quota_requests, 3)

    def test_writes_and_graphql_are_never_conditional(self):
        runner = SequenceRunner(response(payload={}, etag='"read"'),
                                *(response(payload={}, etag='"write"') for _ in range(3)),
                                response(204),
                                *(response(payload={}, etag='"graphql"') for _ in range(4)), response(304))
        github = GitHub('org/project', runner)
        github.request('resource')
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            github.request('resource', method, {'value': 1})
        for endpoint in ('graphql', 'https://api.github.com/graphql'):
            github.request(endpoint)
            github.request(endpoint, 'POST', {'query': 'query { viewer { login } }'})
        self.assertEqual(github.request('resource'), {})
        self.assertEqual([validator(c) for c, _ in runner.calls], [None] * 9 + ['"read"'])
        self.assertEqual(github.quota_requests, 5)

    def test_missing_etag_clears_entry_and_invalid_or_failed_responses_do_not_replace_it(self):
        runner = SequenceRunner(response(payload={'version': 1}, etag='"one"'),
                                response(500, {'message': 'failure'}, '"failed"'), response(304),
                                response(payload=[], etag='"invalid"'), response(304),
                                response(payload={'version': 2}), response(payload={'version': 3}))
        github = GitHub('org/project', runner)
        github.request('resource')
        with self.assertRaises(GitHubError):
            github.request('resource')
        self.assertEqual(github.request('resource'), {'version': 1})
        with self.assertRaisesRegex(GitHubError, 'expected an object'):
            github.request('resource')
        self.assertEqual(github.request('resource'), {'version': 1})
        self.assertEqual(github.request('resource'), {'version': 2})
        self.assertEqual(github.request('resource'), {'version': 3})
        self.assertEqual([validator(c) for c, _ in runner.calls], [None] + ['"one"'] * 5 + [None])
        self.assertEqual(github.quota_requests, 5)

    def test_cached_payload_still_requires_the_requested_shape(self):
        runner = SequenceRunner(response(payload=[], etag='"array"'), response(304))
        github = GitHub('org/project', runner)
        github.request('resource', array=True)
        with self.assertRaisesRegex(GitHubError, 'expected an object'):
            github.request('resource')

    def test_transport_failure_has_no_quota_response(self):
        runner = SequenceRunner(subprocess.TimeoutExpired('gh', 20))
        github = GitHub('org/project', runner)
        with self.assertRaises(GitHubError):
            github.actor()
        self.assertEqual(github.quota_requests, 0)


class CoordinationRunner(RecordingRunner):
    """Serve the claim-to-release audit at the gh boundary, with real HTTP headers.

    Forty-five open items: 30 ready issues and 15 untriggered PRs. The worker
    selects issue #1, hands off to ready PR #31, consumes its trigger and routes
    the handoff. Priority ranking and milestone/dependency gates are enabled.
    """
    def __init__(self, root, honor_etags):
        super().__init__(root)
        self.store = FakeGitHub(*(issue(n, labels=('ready', 'p1'), milestone=1) for n in range(1, 31)),
                                *(pr(n, labels=(), milestone=1) for n in range(31, 46)))
        self.store.milestones = [{'number': 1, 'state': 'open', 'open_issues': 45,
                                 'created_at': issue().created_at}]
        self.honor_etags = honor_etags
        self.quota_requests = 0
        self.rest_requests = 0
        self.not_modified = 0

    def raw_item(self, number):
        item = self.store.item(number)
        raw = {'number': number, 'title': item.title, 'body': item.body,
               'labels': [{'name': label} for label in sorted(item.labels)],
               'state': item.state, 'created_at': item.created_at,
               'updated_at': item.updated_at or item.created_at, 'milestone': {'number': item.milestone},
               'issue_dependencies_summary': {'blocked_by': 0, 'blocking': 0,
                                               'total_blocked_by': 0, 'total_blocking': 0}}
        if item.kind == 'pr':
            raw |= {'pull_request': {}, 'draft': item.draft,
                    'head': {'sha': item.head, 'ref': item.branch}}
        return raw

    def dispatch(self, method, endpoint, data):
        parts = urlsplit(endpoint)
        path = parts.path.removeprefix('repos/org/project/')
        if path == 'graphql':
            query, variables = data['query'], data['variables']
            if 'userContentEdits' in query:
                item = self.store.item(variables['number'])
                return {'data': {'repository': {'issue': {
                    'title': item.title, 'body': item.body, 'createdAt': item.created_at,
                    'lastEditedAt': None, 'userContentEdits': {
                        'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}
            raise AssertionError(f'Unexpected GraphQL query: {query}')
        if path == 'issues':
            return [self.raw_item(n) for n in sorted(self.store.items)]
        if path == 'milestones':
            return deepcopy(self.store.milestones)
        if path == 'issues/comments':
            return sorted(self.store.repository_comments(), key=lambda c: c['updated_at'])
        if path.startswith('collaborators/') and path.endswith('/permission'):
            return {'role_name': self.store.role(path.split('/')[1])}
        if path.startswith('issues/comments/'):
            return self.store.update_comment(int(path.rsplit('/', 1)[1]), data['body'])
        fields = path.split('/')
        if fields[0] in {'issues', 'pulls'}:
            number = int(fields[1])
            if len(fields) == 2:
                return self.raw_item(number)
            if fields[2] == 'comments':
                if method == 'POST':
                    return self.store.create_comment(number, data['body'])
                return self.store.comments(number)
            if fields[2] == 'timeline':
                return self.store.timeline(number)
            if fields[2:] == ['dependencies', 'blocked_by']:
                return []
            if fields[2] == 'labels':
                if method == 'DELETE':
                    self.store.remove_label(number, unquote(fields[3]))
                    return None
                self.store.add_labels(number, data['labels'])
                return [{'name': label} for label in sorted(self.store.item(number).labels)]
        raise AssertionError(f'Unexpected request: {method} {endpoint}')

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        method = command[command.index('--method') + 1]
        endpoint = command[command.index('--include') + 1]
        data = json.loads(kwargs['input']) if kwargs['input'] is not None else None
        value = self.dispatch(method, endpoint, data)
        if endpoint == 'graphql':
            if validator(command) is not None:
                raise AssertionError('Conditional GraphQL request')
            return response(payload=value)
        self.rest_requests += 1
        if method != 'GET' and validator(command) is not None:
            raise AssertionError('Conditional write')
        etag = '"' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() + '"'
        if method == 'GET' and self.honor_etags and validator(command) == etag:
            self.not_modified += 1
            return response(304, etag=etag)
        self.quota_requests += 1
        return response(204 if method == 'DELETE' else 200, value, etag if method == 'GET' else None)


class RequestBudgetTests(unittest.TestCase):
    def test_45_item_claim_to_release_uses_at_most_half_the_rest_quota(self):
        stub_refresh(self)
        counts, request_counts = [], []
        for honor_etags in (True, False):
            with self.subTest(honor_etags=honor_etags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = CoordinationRunner(root, honor_etags)
                github = GitHub('org/project', runner)
                worker = agent(root, kind='issue', outcomes={'done': {'add': ('needs-review',), 'remove': ()}})
                queue = Queue(milestones='gate', priority=Priority(('p1', 'p2')))
                output = []
                loop = Loop(config(root, worker, queue=queue), github, 'operator', output=output.append)

                def execute(command, cwd, env, run_dir, *args, **kwargs):
                    # Exercise the supervised process-start callback and the real
                    # report path; only the subprocess itself is replaced.
                    kwargs['process_started'](12345)
                    lease = next(r for r in loop.coordinator.history(1) if r['kind'] == 'lease')
                    loop.coordinator.report(lease, 'success', 'Candidate ready', handoff=31, outcome='done')
                    return 0

                with patch('ub_agents.loop.supervise', side_effect=execute) as executed:
                    self.assertTrue(loop.tick())
                executed.assert_called_once()
                history = records(runner.store.comments(1), 'operator')
                lease, outcome = history
                self.assertEqual((lease['state'], lease['result'], lease['attempt_effect']),
                                 ('released', 'success', 'reset'))
                self.assertTrue(outcome['accepted'])
                self.assertTrue(outcome['transition_complete'])
                self.assertEqual(outcome['candidate_sha'], 'a' * 40)
                self.assertEqual(runner.store.item(1).labels, frozenset({'p1'}))
                self.assertEqual(runner.store.item(31).labels, frozenset({'needs-review'}))
                copied = records(runner.store.comments(31), 'operator')
                self.assertEqual(len(copied), 1)
                self.assertTrue(copied[0]['accepted'])
                self.assertFalse(any('failed' in line.lower() for line in output), output)
                self.assertEqual(github.quota_requests, runner.quota_requests)
                self.assertEqual(runner.not_modified > 0, honor_etags)
                # Count before test assertions can add client reads.
                counts.append(github.quota_requests)
                request_counts.append(runner.rest_requests)
        self.assertEqual(len(counts), 2)
        self.assertEqual(request_counts[0], request_counts[1])
        self.assertLessEqual(counts[0] * 2, counts[1], f'conditional={counts[0]}, baseline={counts[1]}')
