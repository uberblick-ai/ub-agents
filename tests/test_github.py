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
from ub_agents.github import Dependency, GitHub, closing_issues, parse_item
from ub_agents.loop import Loop
from ub_agents.records import body, iso, seconds, timestamp
from tests.support import RecordingRunner, agent, config, issue, pr


class GitHubTests(unittest.TestCase):
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
                self.assertEqual(github.observe()[0].total_blocked_by, expected)
        with patch.object(github, "request", return_value=[raw]):
            self.assertIsNone(github.observe()[0].total_blocked_by)

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
            github.prs_for_branch("ub-agent/worker/27/run")
            endpoint = request.call_args.args[0]
            self.assertEqual(parse_qs(urlsplit(endpoint).query)["state"], ["open"])
            self.assertTrue(request.call_args.kwargs["paginate"])
            github.prs_for_branch("ub-agent/worker/27/run", state="all")
            query = parse_qs(urlsplit(request.call_args.args[0]).query)
            self.assertEqual(query["state"], ["all"])
            self.assertEqual(query["head"], ["org:ub-agent/worker/27/run"])
