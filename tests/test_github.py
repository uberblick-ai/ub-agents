from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ub_agents.errors import AgentError, GitHubError
from ub_agents.config import load_config
from ub_agents.github import Dependency, GitHub, closing_issues, parse_item
from ub_agents.loop import Loop
from ub_agents.notices import Notices
from ub_agents.records import body, iso, seconds, timestamp
from tests.support import DiscoveryCostRunner, RecordingRunner, agent, config, issue, pr


class GitHubTests(unittest.TestCase):
    def test_45_item_status_and_cold_discovery_request_cost(self):
        from ub_agents.cli import status_rows
        from ub_agents.config import Priority, Queue
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status in (False, True):
                with self.subTest(status=status):
                    runner = DiscoveryCostRunner()
                    loop = Loop(config(root, agent(root),
                                       queue=Queue(priority=Priority(("urgent", "low"), "low"))),
                                GitHub("org/project", runner), "operator")
                    rows = status_rows(loop) if status else list(loop.iter_plans())
                    self.assertEqual(len(rows), 30)
                    self.assertEqual(len(runner.calls), 96)
                    paths = [urlsplit(c[c.index("--include") + 1]).path for c in runner.calls]
                    self.assertEqual(sum(p.endswith("/comments") and not p.endswith("/issues/comments")
                                         for p in paths), 30)
                    self.assertEqual(sum(p.endswith("/permission") for p in paths), 3)
                    self.assertEqual(sum(p.endswith("/dependencies/blocked_by") for p in paths), 0)

    def test_latest_quota_headers_are_retained_per_resource_including_failures(self):
        runner = RecordingRunner(Path('/synthetic'))
        github = GitHub('org/project', runner)
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')

        def response(endpoint, resource, remaining, status=200, etag=None):
            validator = f'ETag: {etag}\n' if etag is not None else ''
            runner.responses[prefix + (endpoint,)] = subprocess.CompletedProcess(
                [], int(status >= 400), f'HTTP/2.0 {status} Response\n'
                f'X-RateLimit-Resource: {resource}\nX-RateLimit-Remaining: {remaining}\n'
                f'X-RateLimit-Limit: 5000\nX-RateLimit-Reset: 4600\n{validator}\n{{}}', '')

        response('user', 'core', 4000)
        github.request('user')
        response('graphql', 'graphql', 500)
        github.request('graphql')
        response('user', 'core', 0, 403)
        with self.assertRaises(GitHubError):
            github.request('user')
        self.assertEqual(github.resource_quotas['core']['x-ratelimit-remaining'], '0')
        self.assertEqual(github.resource_quotas['graphql']['x-ratelimit-remaining'], '500')
        self.assertEqual(github.quota_headers, github.resource_quotas['core'])
        self.assertEqual(github.rest_requests, 2)
        self.assertEqual(github.quota_requests, 2)
        response('user', 'core', 5000, etag='"user"')
        github.request('user')
        self.assertEqual(github.resource_quotas['core']['x-ratelimit-remaining'], '5000')
        self.assertEqual(github.resource_quotas['graphql']['x-ratelimit-remaining'], '500')
        conditional = prefix + ('user', '-H', 'If-None-Match: "user"')
        runner.responses[conditional] = subprocess.CompletedProcess(
            [], 1, 'HTTP/2.0 304 Not Modified\nX-RateLimit-Resource: core\n'
            'X-RateLimit-Remaining: 4999\nX-RateLimit-Limit: 5000\n'
            'X-RateLimit-Reset: 8200\n\n', 'gh: HTTP 304')
        self.assertEqual(github.request('user'), {})
        self.assertEqual(github.resource_quotas['core']['x-ratelimit-remaining'], '4999')
        self.assertEqual(github.resource_quotas['core']['x-ratelimit-reset'], '8200')
        self.assertEqual(github.resource_quotas['graphql']['x-ratelimit-remaining'], '500')
        self.assertEqual(github.quota_headers, github.resource_quotas['core'])
        self.assertEqual(github.rest_requests, 4)
        self.assertEqual(github.quota_requests, 3)

    def test_rate_limit_classification_uses_real_response_headers_and_messages(self):
        command = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                   'Accept: application/vnd.github+json', '--include', 'user')
        cases = [
            (403, 'X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 4600\n', '{}', '', True, 4605),
            (403, 'Retry-After: 12\n', '{}', '', True, 1012),
            (429, 'Retry-After: 12\n', '{}', '', True, 1012),
            (403, '', '{"message":"API rate limit exceeded"}', 'HTTP 403', True, 1060),
            (429, '', '{"message":"secondary rate limit"}', '', True, 1060),
            (403, 'X-RateLimit-Reset: 1100\n', '{}', 'Resource not accessible', False, None),
            (429, '', '{}', 'Unknown failure', False, None),
            (401, 'Retry-After: 12\n', '{}', 'Bad credentials', False, None),
        ]
        for status, headers, payload, stderr, limited, reset in cases:
            with self.subTest(status=status, headers=headers, payload=payload):
                runner = RecordingRunner(Path('/synthetic'))
                runner.responses[command] = subprocess.CompletedProcess(
                    [], 1, f'HTTP/2.0 {status} Error\n{headers}\n{payload}', stderr)
                github = GitHub('org/project', runner)
                with patch('ub_agents.github.timestamp', return_value=1000), self.assertRaises(GitHubError) as raised:
                    github.actor()
                self.assertEqual(raised.exception.rate_limited, limited)
                self.assertEqual(raised.exception.reset_at, reset)
                self.assertEqual(github.rate_limited, limited)

    def test_minimization_state_reads_rest_node_ids_in_bounded_batches(self):
        comments = [{"id": i, "node_id": f"IC_{i}"} for i in range(205)]
        github = GitHub('org/project')

        def response(endpoint, method, data):
            self.assertEqual((endpoint, method), ('graphql', 'POST'))
            self.assertIn('... on IssueComment { id isMinimized }', data['query'])
            return {"data": {"nodes": [{"id": node, "isMinimized": int(node[3:]) % 2 == 0}
                                       for node in data['variables']['ids']]}}

        with patch.object(github, 'request', side_effect=response) as request:
            self.assertEqual(github.unminimized_comments(comments), comments[1::2])
            self.assertEqual([len(call.args[2]['variables']['ids']) for call in request.call_args_list],
                             [100, 100, 5])
            request.reset_mock()
            self.assertEqual(github.unminimized_comments([]), [])
            request.assert_not_called()

    def test_missing_or_invalid_minimization_state_fails_visibly(self):
        github = GitHub('org/project')
        comments = [{"id": 1, "node_id": "IC_1"}]
        for nodes in (None, {}, [], [None], [{}], [{"id": "IC_2", "isMinimized": False}],
                      [{"id": "IC_1", "isMinimized": 0}],
                      [{"id": "IC_1", "isMinimized": False}] * 2):
            with self.subTest(nodes=nodes), patch.object(github, 'request', return_value={"data": {"nodes": nodes}}):
                with self.assertRaisesRegex(GitHubError, 'Unreadable comment minimization state'):
                    github.unminimized_comments(comments)
        with patch.object(github, 'request') as request:
            for node in (None, '', 42):
                with self.subTest(node=node), self.assertRaisesRegex(GitHubError, 'no node ID'):
                    github.unminimized_comments([{"id": 1, "node_id": node}])
            request.assert_not_called()

    def test_failed_later_state_batch_logs_without_partial_minimization(self):
        comments = [{"id": i, "node_id": f"IC_{i}"} for i in range(101)]
        github = GitHub('org/project')
        first = {"data": {"nodes": [{"id": comment['node_id'], "isMinimized": False}
                                    for comment in comments[:100]]}}
        output = []
        for failure in (GitHubError('POST', 'graphql', 'State unavailable'),
                        {"data": {"nodes": [None]}}):
            with self.subTest(failure=failure), \
                    patch.object(github, 'request', side_effect=[first, failure]), \
                    patch.object(github, 'minimize_comment') as minimize:
                Notices(github, 'operator', output.append).minimize(comments)
                minimize.assert_not_called()
                self.assertIn('Advisory comment minimization state read failed', output[-1])

    def test_minimize_comment_uses_node_id_and_outdated_graphql_classifier(self):
        runner = RecordingRunner(Path('/synthetic'))
        command = ('gh', 'api', '--hostname', 'github.com', '--method', 'POST', '-H',
                   'Accept: application/vnd.github+json', '--include', 'graphql', '--input', '-')
        runner.responses[command] = json.dumps({"data": {"minimizeComment": {
            "minimizedComment": {"isMinimized": True}}}})
        github = GitHub('org/project', runner)
        github.minimize_comment({"id": 123, "node_id": "IC_node"})
        sent = json.loads(runner.calls[-1][1]['input'])
        self.assertEqual(sent['variables'], {"id": "IC_node"})
        self.assertIn('minimizeComment', sent['query'])
        self.assertIn('classifier: OUTDATED', sent['query'])
        self.assertIn('subjectId: $id', sent['query'])
        for response in ({"errors": [{"message": "not authorized"}]},
                         {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": False}}}}):
            with self.subTest(response=response):
                runner.responses[command] = json.dumps(response)
                with self.assertRaises(GitHubError):
                    github.minimize_comment({"node_id": "IC_node"})
        with self.assertRaises(GitHubError):
            github.minimize_comment({"id": 123})

    def test_candidate_evidence_reads_exact_sha_and_identifies_changed_review_head(self):
        sha = 'a' * 40
        query_result = {"data": {"repository": {
            "pullRequest": {"headRefOid": sha, "reviewDecision": "APPROVED"},
            "object": {"oid": sha, "statusCheckRollup": {"state": "SUCCESS"}}}}}
        runner = RecordingRunner(Path('/synthetic'))
        command = ('gh', 'api', '--hostname', 'github.com', '--method', 'POST', '-H',
                   'Accept: application/vnd.github+json', '--include', 'graphql', '--input', '-')
        runner.responses[command] = json.dumps(query_result)
        github = GitHub('org/project', runner)
        self.assertEqual(github.candidate_evidence(2, sha), ('APPROVED', 'SUCCESS'))
        sent = json.loads(runner.calls[-1][1]['input'])
        self.assertEqual(sent['variables'], {"owner": "org", "name": "project", "number": 2, "sha": sha})
        self.assertIn('object(oid: $sha)', sent['query'])
        for head, review, rollup, expected in (
                (sha, None, None, ('no decision', 'no checks or statuses')),
                ('b' * 40, 'APPROVED', {"state": "FAILURE"},
                 ('unavailable for this SHA (current head bbbbbbb)', 'FAILURE'))):
            with self.subTest(head=head):
                repository = query_result['data']['repository']
                repository['pullRequest'] = {"headRefOid": head, "reviewDecision": review}
                repository['object']['statusCheckRollup'] = rollup
                runner.responses[command] = json.dumps(query_result)
                self.assertEqual(github.candidate_evidence(2, sha), expected)
        for response in ({"data": {"repository": None}}, {"data": {"repository": {"object": None}}},
                         {"data": {"repository": {"object": {"oid": 'b' * 40}}}},
                         {"data": {}, "errors": [{"message": "evidence unavailable"}]}):
            with self.subTest(response=response):
                runner.responses[command] = json.dumps(response)
                with self.assertRaises(GitHubError):
                    github.candidate_evidence(2, sha)

    def test_include_parses_success_and_empty_delete_headers(self):
        runner = RecordingRunner(Path('/synthetic'))
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')
        runner.responses[prefix + ('user',)] = (
            'HTTP/2.0 200 OK\nContent-Type: application/json\r\n'
            'Link: first\r\nLink: second\r\n\r\n{"login":"operator"}')
        github = GitHub('org/project', runner)
        self.assertEqual(github.actor(), 'operator')
        delete = prefix[:5] + ('DELETE',) + prefix[6:] + ('repos/org/project/issues/1/labels/ready',)
        runner.responses[delete] = 'HTTP/1.1 204 No Content\r\nX-RateLimit-Remaining: 10\r\n\r\n'
        github.remove_label(1, 'ready')

    def test_known_transport_failures_retry_but_http_and_local_errors_take_precedence(self):
        command = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                   'Accept: application/vnd.github+json', '--include', 'user')
        cases = [(subprocess.TimeoutExpired('gh', 20), True),
                 (ConnectionResetError('Connection reset by peer'), True),
                 (FileNotFoundError('gh is missing'), False),
                 (PermissionError('gh cannot execute'), False)]
        for message in ('dial tcp: lookup api.github.com: no such host', 'connect: connection refused',
                        'read: connection reset by peer', 'i/o timeout', 'TLS handshake timeout',
                        'context deadline exceeded', 'unexpected EOF',
                        'net/http: request canceled (Client.Timeout exceeded while awaiting headers)',
                        'net/http: timeout awaiting response headers', 'connect: operation timed out'):
            cases.append((subprocess.CompletedProcess([], 1, '', message), True))
        cases.extend([(subprocess.CompletedProcess([], 1, 'HTTP/2.0 200 OK\n\n[]', 'unexpected EOF'), True),
                      (subprocess.CompletedProcess([], 1, 'HTTP/2.0 401 Error\n\n{}', 'i/o timeout'), False),
                      (subprocess.CompletedProcess([], 1, '', 'certificate signed by unknown authority'), False),
                      (subprocess.CompletedProcess([], 1, '', 'unknown failure'), False)])
        for response, retryable in cases:
            with self.subTest(response=response):
                runner = RecordingRunner(Path('/synthetic'))
                runner.responses[command] = response
                with self.assertRaises(GitHubError) as raised:
                    GitHub('org/project', runner).actor()
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertIn('GitHub GET user failed', str(raised.exception))

    def test_malformed_http_headers_stop_without_retry(self):
        for response in ('HTTP/2.0 504 Error', 'HTTP/2.0 invalid\n\n{}',
                         'HTTP/2.0 200 OK\ninvalid header\n\n{}'):
            runner = RecordingRunner(Path('/synthetic'))
            runner.responses[('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                              'Accept: application/vnd.github+json', '--include', 'user')] = response
            with self.subTest(response=response), self.assertRaises(GitHubError) as raised:
                GitHub('org/project', runner).actor()
            self.assertFalse(raised.exception.retryable)

    def test_malformed_issue_after_a_pr_still_names_issue_list_request(self):
        github = GitHub('org/project')
        raw = {'number': 2, 'title': 'Candidate', 'body': '', 'state': 'open',
               'labels': [], 'created_at': iso(100), 'pull_request': {}}
        with patch.object(github, 'request', return_value=[raw, {'number': 3}]), \
                patch.object(github, 'item', return_value=pr()), self.assertRaises(GitHubError) as raised:
            github.observe()
        self.assertIn('GET repos/org/project/issues?state=open', str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_labels_reads_all_pages_and_create_only_posts_the_new_label(self):
        runner = RecordingRunner(Path('/synthetic'))
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')
        runner.responses[prefix + ('repos/org/project/labels?per_page=100&page=1',)] = json.dumps(
            [{'name': f'label-{number}'} for number in range(100)])
        runner.responses[prefix + ('repos/org/project/labels?per_page=100&page=2',)] = json.dumps(
            [{'name': 'final-label'}])
        github = GitHub('org/project', runner=runner)
        labels = github.labels()
        self.assertEqual(len(labels), 101)
        self.assertEqual(labels[-1], 'final-label')
        post = prefix[:5] + ('POST',) + prefix[6:] + ('repos/org/project/labels', '--input', '-')
        runner.responses[post] = '{}'
        github.create_label('new-label', 'Synthetic description', '1d76db')
        self.assertEqual([command for command, _ in runner.calls],
                         [prefix + ('repos/org/project/labels?per_page=100&page=1',),
                          prefix + ('repos/org/project/labels?per_page=100&page=2',), post])
        self.assertEqual(json.loads(runner.calls[-1][1]['input']),
                         {'name': 'new-label', 'description': 'Synthetic description', 'color': '1d76db'})

    def test_unreadable_labels_fail_instead_of_offering_existing_labels_for_creation(self):
        for payload in ('{}', 'null', 'not json', '[null]', '[{}]', '[{"name":null}]',
                        '[{"name":42}]', '[{"name":""}]', '[{"name":" "}]'):
            with self.subTest(payload=payload):
                runner = RecordingRunner(Path('/synthetic'))
                runner.responses[('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                                  'Accept: application/vnd.github+json', '--include',
                                  'repos/org/project/labels?per_page=100&page=1')] = payload
                with self.assertRaises(AgentError):
                    GitHub('org/project', runner=runner).labels()

    def test_observe_preserves_valid_dependency_totals_and_falls_back_for_bad_summaries(self):
        raw = {"number": 1, "title": "Work", "body": None, "state": "open",
               "labels": [], "user": {"login": "operator"}, "created_at": iso(100)}
        zero = {"blocked_by": 0, "blocking": 0, "total_blocked_by": 0, "total_blocking": 0}
        cases = [(zero, 0), (zero | {"total_blocked_by": 1}, 1),
                 (zero | {"blocked_by": 2, "total_blocked_by": 2}, 2),
                 (None, None), ([], None), ("unreadable", None), ({}, None),
                 ({"total_blocked_by": 0}, None), (zero | {"blocked_by": 1}, None),
                 (zero | {"blocking": 1}, None)]
        for key in zero:
            cases.extend((zero | {key: value}, None) for value in (None, True, "0", -1, 0.0))
        github = GitHub("org/project")
        for summary, expected in cases:
            with self.subTest(summary=summary), \
                    patch.object(github, "request", return_value=[raw | {"issue_dependencies_summary": summary}]):
                item = github.observe()[0]
                self.assertEqual(item.total_blocked_by, expected)
                self.assertEqual(item.open_blocked_by, summary["blocked_by"] if expected is not None else None)
        with patch.object(github, "request", return_value=[raw]):
            self.assertIsNone(github.observe()[0].total_blocked_by)

    def test_dependency_graph_lists_pages_and_falls_back_for_large_connections(self):
        def links(number, more=False):
            return {"number": number, "blockedBy": {
                "nodes": [{"number": 31, "state": "OPEN", "repository": {"nameWithOwner": "other/project"}}],
                "pageInfo": {"hasNextPage": more}}}
        pages = [{"repository": {"issues": {"nodes": [links(1)],
                  "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
                 {"repository": {"issues": {"nodes": [links(2, True)],
                  "pageInfo": {"hasNextPage": False, "endCursor": None}}}}]
        github = GitHub("org/project")
        with patch.object(github, "graphql", side_effect=pages) as graphql, \
                patch.object(github, "blocked_by", return_value=[Dependency("org/project", 32, "closed")]) as rest:
            self.assertEqual(github.dependency_graph(), {
                1: [Dependency("other/project", 31, "open")], 2: [Dependency("org/project", 32, "closed")]})
        self.assertEqual([call.args[1]["cursor"] for call in graphql.call_args_list], [None, "next"])
        self.assertIn("states:OPEN", graphql.call_args.args[0])
        rest.assert_called_once_with(2)

    def test_dependency_graph_fails_on_unreadable_or_repeated_pages(self):
        good = {"repository": {"issues": {"nodes": [{"number": 1, "blockedBy": {
            "nodes": [], "pageInfo": {"hasNextPage": False}}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        bad = [{}, {"repository": None}]
        for field, value in (("number", True), ("number", 0), ("blockedBy", None)):
            malformed = deepcopy(good)
            malformed["repository"]["issues"]["nodes"][0][field] = value
            bad.append(malformed)
        malformed = deepcopy(good)
        malformed["repository"]["issues"]["pageInfo"]["hasNextPage"] = "yes"
        bad.append(malformed)
        for response in bad:
            with self.subTest(response=response), \
                    patch.object(GitHub, "graphql", return_value=response), self.assertRaises(GitHubError):
                GitHub("org/project").dependency_graph()
        repeated = deepcopy(good)
        repeated["repository"]["issues"]["pageInfo"] = {"hasNextPage": True, "endCursor": "same"}
        with patch.object(GitHub, "graphql", return_value=repeated), self.assertRaises(GitHubError):
            GitHub("org/project").dependency_graph()

    def test_lazy_pr_discovery_uses_only_paginated_issue_list_and_chosen_detail(self):
        class QueueRunner:
            def __init__(self, size):
                self.calls = []
                self.rows = [{"number": n, "title": "Candidate", "body": "", "state": "open",
                              "labels": [{"name": "needs-changes"}], "created_at": iso(n),
                              "updated_at": iso(1000), "pull_request": {}}
                             for n in range(1, size + 1)]
            def __call__(self, command, **kwargs):
                self.calls.append(command)
                endpoint = command[-1]
                path = urlsplit(endpoint).path
                if path.endswith("/issues"):
                    page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
                    response = self.rows[(page - 1) * 100:page * 100]
                elif path.endswith("/pulls/1"):
                    response = self.rows[0] | {"draft": False, "head": {"sha": "a" * 40, "ref": "candidate"}}
                elif path.endswith("/permission"):
                    response = {"role_name": "write"}
                else:
                    response = []
                return subprocess.CompletedProcess(command, 0, json.dumps(response), "")
        from ub_agents.approvals import ApprovalCheck
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for size in (1, 99, 100, 101, 201):
                runner = QueueRunner(size)
                loop = Loop(config(root, agent(root)), GitHub("org/project", runner), "operator")
                with patch.object(loop, "input_check", return_value=ApprovalCheck(True, "Approved")), \
                        patch.object(loop, "execute", return_value=True):
                    self.assertTrue(loop.tick())
                    first_count = len(runner.calls)
                    self.assertTrue(loop.tick())
                list_pages = size // 100 + 1
                # Cold discovery: issue list + repository comments + one PR's
                # details, history and own role. Warm discovery rechecks the
                # own role with the two list scans.
                self.assertEqual(first_count, list_pages + 4)
                self.assertEqual(len(runner.calls) - first_count, list_pages + 2)
                self.assertEqual(sum(urlsplit(c[-1]).path.endswith("/pulls/1") for c in runner.calls), 1)

    def test_closing_keywords_accept_local_qualified_and_url_references(self):
        for keyword in ("close", "closes", "closed", "fix", "fixes", "fixed",
                        "resolve", "resolves", "resolved", "CLOSES:"):
            for reference in ("#21", "ORG/Project#21", "https://github.com/org/project/issues/21"):
                with self.subTest(keyword=keyword, reference=reference):
                    self.assertEqual(closing_issues(pr(body=f"{keyword} {reference}"), "org/project"), {21})
        body = ("Closes #21, fixes #22; resolves org/project#23; closes #21; "
                "fixes other/project#24; relates to #25; forecloses #26; "
                "resolves #27, #28; Closes #0")
        self.assertEqual(closing_issues(pr(body=body), "org/project"), {21, 22, 23, 27})

    def test_dependency_reads_all_pages_and_preserves_repository_and_closed_state(self):
        def row(number, repository="org/project", state="open"):
            return {"number": number, "state": state,
                    "url": f"https://api.github.com/repos/{repository}/issues/{number}"}

        pages = [[row(n) for n in range(1, 101)], [row(31, "other/project", "closed")]]
        results = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with patch("ub_agents.github.subprocess.run", side_effect=results) as run:
            dependencies = GitHub("org/project").blocked_by(1)
        self.assertEqual(len(dependencies), 101)
        self.assertEqual(dependencies[-1], Dependency("other/project", 31, "closed"))
        for page, call in enumerate(run.call_args_list, 1):
            endpoint = call.args[0][-1]
            self.assertIn("issues/1/dependencies/blocked_by?", endpoint)
            self.assertIn(f"page={page}", endpoint)

    def test_unreadable_dependencies_fail_instead_of_returning_an_empty_list(self):
        valid = {"number": 31, "state": "open",
                 "url": "https://api.github.com/repos/other/project/issues/31"}
        for row in (None, {}, valid | {"number": True}, valid | {"number": 32},
                    valid | {"state": "unknown"}, valid | {"state": None},
                    valid | {"url": "https://example.com/repos/other/project/issues/31"},
                    valid | {"url": None}, valid | {"pull_request": {}}):
            with self.subTest(row=row), \
                    patch("ub_agents.github.subprocess.run",
                          return_value=subprocess.CompletedProcess([], 0, json.dumps([row]), "")), \
                    self.assertRaisesRegex(AgentError, "Unreadable GitHub dependency"):
                GitHub("org/project").blocked_by(1)
        for result in (subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 0, "null", ""),
                       subprocess.CompletedProcess([], 0, "not json", ""),
                       subprocess.CompletedProcess([], 1, "", "dependency access denied")):
            with patch("ub_agents.github.subprocess.run", return_value=result), self.assertRaises(AgentError):
                GitHub("org/project").blocked_by(1)

    def test_item_reads_creation_time_and_milestone_for_issues_and_prs(self):
        raw = {"number": 1, "title": "Work", "body": None, "state": "open",
               "labels": [], "user": {"login": "operator"}, "created_at": iso(100),
               "milestone": {"number": 20}, "draft": False, "head": {"sha": "a" * 40, "ref": "candidate"}}
        for kind in ("issue", "pr"):
            with self.subTest(kind=kind):
                item = parse_item(raw, kind)
                self.assertEqual((item.created_at, item.milestone), (iso(100), 20))
                self.assertIsNone(parse_item(dict(raw, milestone=None), kind).milestone)
        for changes in ({"created_at": None}, {"created_at": "bad"},
                        {"milestone": {}}, {"milestone": {"number": True}}):
            with self.subTest(changes=changes), self.assertRaises(AgentError):
                parse_item(raw | changes, "issue")

    def test_milestone_order_reads_every_page_and_sorts_by_creation_then_number(self):
        later = {"number": 1, "created_at": iso(300), "state": "open", "open_issues": 1}
        first_page = [dict(later, number=n) for n in range(1, 101)]
        older = dict(later, number=200, created_at=iso(100))
        tie = dict(older, number=150)
        empty = dict(later, number=300, created_at=iso(1), open_issues=0)
        closed = dict(empty, number=400, state="closed", open_issues=1)
        pages = [first_page, [older, tie, empty, closed]]
        results = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with patch("ub_agents.github.subprocess.run", side_effect=results) as run:
            self.assertEqual(GitHub("org/project").milestone_order(), (150, 200, *range(1, 101)))
        self.assertEqual(run.call_count, 2)
        for page, call in enumerate(run.call_args_list, 1):
            endpoint = call.args[0][-1]
            self.assertIn("/milestones?", endpoint)
            self.assertIn("state=open", endpoint)
            self.assertIn(f"page={page}", endpoint)

    def test_no_incomplete_milestones_returns_empty_order(self):
        for rows in ([], [{"number": 1, "created_at": iso(1), "state": "open", "open_issues": 0}],
                     [{"number": 1, "created_at": iso(1), "state": "closed", "open_issues": 2}]):
            with self.subTest(rows=rows):
                github = GitHub("org/project")
                with patch.object(github, "request", return_value=rows):
                    self.assertEqual(github.milestone_order(), ())

    def test_unreadable_milestones_fail_the_order_read(self):
        valid = {"number": 1, "created_at": iso(1), "state": "open", "open_issues": 1}
        for row in (None, {}, valid | {"created_at": "bad"}, valid | {"number": True},
                    valid | {"open_issues": -1}, valid | {"open_issues": "1"},
                    valid | {"state": "unknown"}):
            with self.subTest(row=row):
                github = GitHub("org/project")
                with patch.object(github, "request", return_value=[row]), self.assertRaises(AgentError):
                    github.milestone_order()

    def test_pr_draft_state_is_required_and_preserved(self):
        raw = {"number": 2, "title": "Candidate", "body": "Closes #1", "labels": [],
               "state": "open", "user": {"login": "operator"}, "created_at": iso(100),
               "head": {"sha": "a" * 40, "ref": "feature/test", "repo": {"full_name": "org/project"}}}
        for draft in (True, False):
            parsed = parse_item(raw | {"draft": draft}, "pr")
            self.assertEqual(parsed.draft, draft)
        for fields in ({}, {"draft": None}, {"draft": "false"}, {"draft": 0}):
            with self.subTest(fields=fields), self.assertRaises(AgentError):
                parse_item(raw | fields, "pr")
        self.assertFalse(parse_item(raw, "issue").draft)

    def test_reads_all_pages_without_indexed_search(self):
        pages = [[{"id": i} for i in range(100)], [{"id": 100}]]
        results = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with patch("ub_agents.github.subprocess.run", side_effect=results) as run:
            self.assertEqual(len(GitHub("org/project").comments(1)), 101)
        self.assertEqual(run.call_count, 2)
        for page, call in enumerate(run.call_args_list, 1):
            argv = call.args[0]
            self.assertIn(f"page={page}", argv[-1])
            self.assertEqual(call.kwargs["timeout"], 20)
            self.assertNotIn("--paginate", argv)
            self.assertNotIn("search", argv)

    def test_repository_scan_is_incremental_with_overlapping_cursor_and_edit_replacement(self):
        github = GitHub("org/project")
        initial = [{"id": 1, "body": "claiming", "updated_at": iso(100)},
                   {"id": 2, "body": "old history", "updated_at": iso(200)}]
        changed = [{"id": 1, "body": "released", "updated_at": iso(1000)},
                   {"id": 3, "body": "new outcome", "updated_at": iso(1001)}]
        with patch("ub_agents.github.timestamp", side_effect=[1000, 1100, 1200]), \
                patch.object(github, "request", side_effect=[initial, changed, AgentError("network failed")]) as request:
            self.assertEqual(github.repository_comments(), initial)
            self.assertEqual(github.repository_comments(), [changed[0], initial[1], changed[1]])
            cursor = github._comment_since
            with self.assertRaises(AgentError):
                github.repository_comments()
            self.assertEqual(github._comment_since, cursor)
        first = parse_qs(urlsplit(request.call_args_list[0].args[0]).query)
        second = parse_qs(urlsplit(request.call_args_list[1].args[0]).query)
        self.assertNotIn("since", first)
        self.assertEqual(second["since"], ["1970-01-01T00:15:40Z"])
        self.assertEqual(second["sort"], ["updated"])
        restarted = GitHub("org/project")
        with patch.object(restarted, "request", return_value=[]) as request:
            restarted.repository_comments()
        self.assertNotIn("since=", request.call_args.args[0])

    def test_failed_page_does_not_advance_incremental_discovery(self):
        github = GitHub("org/project")
        comments = [{"id": i, "updated_at": iso(i + 1000)} for i in range(100)]
        first_page = subprocess.CompletedProcess([], 0, json.dumps(comments), "")
        with patch("ub_agents.github.subprocess.run", side_effect=[first_page, subprocess.TimeoutExpired("gh", 20)]):
            with self.assertRaises(AgentError):
                github.repository_comments()
        self.assertIsNone(github._comment_since)
        self.assertEqual(github._comment_cache, {})

    def test_launcher_and_status_bound_first_scan_independently_of_agent_and_hook_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ub-agents.yaml"
            path.write_text("""repository: org/project
cleanup:
  command: [echo]
  timeout-seconds: 120
agents:
  short:
    command: [echo]
    trigger: ready
    outcomes: {done: {}}
    agent-timeout-minutes: 1
  long:
    command: [echo]
    trigger: ready
    outcomes: {done: {}}
    agent-timeout-minutes: 240
""")
            cfg = load_config(path)
            now = 2_000_000
            for cached in (True, False):  # Launcher discovery and status.
                with self.subTest(cached=cached):
                    github = GitHub("org/project")
                    loop = Loop(cfg, github, "operator")
                    with patch.object(github, "observe", return_value=[]), \
                            patch.object(github, "visibility", return_value="public"), \
                            patch.object(github, "milestone_order", return_value=()), \
                            patch.object(github, "request", return_value=[]) as request, \
                            patch("ub_agents.github.timestamp", side_effect=[now, now + 100]):
                        list(loop.iter_plans(cached=cached))
                        list(loop.iter_plans(cached=cached))
                    queries = [parse_qs(urlsplit(c.args[0]).query) for c in request.call_args_list]
                    self.assertEqual(queries[0]["since"], [iso(now - (1800 + 7 * 86400))])
                    self.assertEqual(queries[1]["since"], [iso(now - 60)])

    def test_edit_or_deletion_during_scan_cannot_hide_closed_item_failure(self):
        for mutation in ("edit", "delete"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                now = int(timestamp())
                comments = [{"id": i, "body": "Bot update", "user": {"login": "ci-bot"},
                             "updated_at": iso(now - 86400 + i)} for i in range(1, 151)]
                failure = {"kind": "lease", "run": "failed", "agent": "worker",
                           "actor": "operator", "runtime": "direct",
                           "assignment": 42, "assignment_sha": None,
                           "created": iso(now - 86400), "expires": iso(now - 86340),
                           "state": "released", "result": "retry", "summary": "Execution timed out",
                           "attempt": 1, "started": True}
                comments[100].update(body=body(failure), user={"login": "operator"},
                                     issue_url="https://api.github.com/repos/org/project/issues/42")
                reads = 0

                class MovingGitHub(GitHub):
                    def request(self, endpoint, method="GET", data=None, paginate=False, array=False):
                        nonlocal reads
                        if paginate:
                            return super().request(endpoint, method, data, paginate=True)
                        query = parse_qs(urlsplit(endpoint).query)
                        if urlsplit(endpoint).path.endswith("/permission"):
                            return {"role_name": "write"}
                        if urlsplit(endpoint).path.endswith("/milestones"):
                            return []
                        if urlsplit(endpoint).path.endswith("/issues/comments"):
                            reads += 1
                            if reads == 2:
                                if mutation == "edit":
                                    comments[4]["updated_at"] = iso(now)
                                else:
                                    comments.pop(4)
                            rows = sorted(comments, key=lambda c: seconds(c["updated_at"]))
                            if "since" in query:
                                rows = [c for c in rows if seconds(c["updated_at"]) > seconds(query["since"][0])]
                        else:
                            rows = [c for c in comments if c["id"] == 101]
                        offset = (int(query.get("page", [1])[0]) - 1) * 100
                        return deepcopy(rows[offset:offset + 100])

                github = MovingGitHub("org/project")
                closed = replace(issue(42, labels=()), state="closed")
                root = Path(directory)
                loop = Loop(config(root, agent(root)), github, "operator")
                with patch.object(github, "observe", return_value=[]), patch.object(github, "item", return_value=closed):
                    for _ in range(2):  # Both initial discovery and subsequent polls retain it.
                        plans = loop.plans()
                        self.assertEqual([(p.item.number, p.state) for p in plans], [(42, "blocked")])
                        self.assertIn("timed out", plans[0].reason)
                self.assertIn(101, github._comment_cache)

    def test_full_timestamp_bucket_fails_visibly_without_committing_cursor_or_cache(self):
        for total in (99, 100, 101):
            with self.subTest(comments_in_same_second=total):
                github = GitHub("org/project")
                github._comment_since = iso(1000)
                github._comment_cache = {1: {"id": 1, "updated_at": iso(900)}}
                batch = [{"id": i + 2, "updated_at": iso(2000)} for i in range(min(total, 100))]
                with patch.object(github, "request", return_value=batch) as request:
                    if total < 100:
                        self.assertEqual(len(github.repository_comments()), 100)
                    else:
                        with self.assertRaisesRegex(AgentError, "one update second"):
                            github.repository_comments()
                        self.assertEqual(github._comment_since, iso(1000))
                        self.assertEqual(github._comment_cache, {1: {"id": 1, "updated_at": iso(900)}})
                        self.assertEqual(request.call_count, 2)

    def test_bad_response_and_failed_auth_are_failures(self):
        for result in [subprocess.CompletedProcess([], 0, "not json", ""),
                       subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 1, "", "not authenticated")]:
            with patch("ub_agents.github.subprocess.run", return_value=result), self.assertRaises(AgentError):
                GitHub("org/project").comments(1)

    def test_branch_pr_reads_can_include_closed_heads_without_changing_open_default(self):
        github = GitHub("org/project")
        with patch.object(github, "request", return_value=[]) as request:
            github.prs_for_branch("ub-agents/worker/27/run")
            endpoint = request.call_args.args[0]
            self.assertEqual(parse_qs(urlsplit(endpoint).query)["state"], ["open"])
            self.assertTrue(request.call_args.kwargs["paginate"])
            github.prs_for_branch("ub-agents/worker/27/run", state="all")
            query = parse_qs(urlsplit(request.call_args.args[0]).query)
            self.assertEqual(query["state"], ["all"])
            self.assertEqual(query["head"], ["org:ub-agents/worker/27/run"])

    def test_active_milestone_reads_every_page_and_sorts_by_creation_then_number(self):
        later = {"number": 1, "created_at": iso(300), "state": "open", "open_issues": 1}
        first_page = [dict(later, number=n) for n in range(1, 101)]
        older = dict(later, number=200, created_at=iso(100))
        tie = dict(older, number=150)
        empty = dict(later, number=300, created_at=iso(1), open_issues=0)
        closed = dict(empty, number=400, state="closed", open_issues=1)
        pages = [first_page, [older, tie, empty, closed]]
        results = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with patch("ub_agents.github.subprocess.run", side_effect=results) as run:
            self.assertEqual(GitHub("org/project").active_milestone(), 150)
        self.assertEqual(run.call_count, 2)
        for page, call in enumerate(run.call_args_list, 1):
            endpoint = call.args[0][-1]
            self.assertIn("/milestones?", endpoint)
            self.assertIn("state=open", endpoint)
            self.assertIn(f"page={page}", endpoint)

    def test_no_incomplete_milestones_returns_no_gate(self):
        for rows in ([], [{"number": 1, "created_at": iso(1), "state": "open", "open_issues": 0}],
                     [{"number": 1, "created_at": iso(1), "state": "closed", "open_issues": 2}]):
            with self.subTest(rows=rows):
                github = GitHub("org/project")
                with patch.object(github, "request", return_value=rows):
                    self.assertIsNone(github.active_milestone())

    def test_unreadable_milestones_never_remove_the_gate(self):
        valid = {"number": 1, "created_at": iso(1), "state": "open", "open_issues": 1}
        for row in (None, {}, valid | {"created_at": "bad"}, valid | {"number": True},
                    valid | {"open_issues": -1}, valid | {"open_issues": "1"},
                    valid | {"state": "unknown"}):
            with self.subTest(row=row):
                github = GitHub("org/project")
                with patch.object(github, "request", return_value=[row]), self.assertRaises(AgentError):
                    github.active_milestone()
