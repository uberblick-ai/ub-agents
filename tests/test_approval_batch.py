from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from ub_agents.approvals import approval_body, historical_content
from ub_agents.config import Queue
from ub_agents.discovery import Discovery
from ub_agents.github import APPROVAL_BATCH_SIZE, GitHub
from ub_agents.loop import Loop
from ub_agents.records import iso
from ub_agents.run_planning import RunPlanning
from tests.support import DiscoveryCostRunner, agent, config, issue, pr, stub_refresh


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "approval_inputs.json").read_text())


def fixture(case, kind):
    node = deepcopy(FIXTURES["content"] | (FIXTURES["pr"] if kind == "pr" else {}) | case.get("content", {}))
    events = ([FIXTURES["start"]] if case["start"] else []) + case.get("events", [])
    node["timelineItems"] = {"nodes": [deepcopy(e["graphql"]) for e in events],
                             "totalCount": len(events), "pageInfo": {"hasNextPage": False, "endCursor": None}}
    item = issue(1) if kind == "issue" else pr(1, labels=("ready",))
    return replace(item, title=node["title"], body=node["body"]), node, [deepcopy(e["rest"]) for e in events]


class ApprovalFixtureRunner:
    """Paired GraphQL/REST responses through the real gh adapter."""
    def __init__(self, item, node, timeline):
        self.item, self.node, self.timeline = item, node, timeline
        self.calls, self.batch_responses, self.content_responses = [], [], []
        self.comments = []
        self.roles = {"operator": "write", "maintainer": "maintain"}

    def __call__(self, command, **kwargs):
        endpoint = command[command.index("--include") + 1]
        path = urlsplit(endpoint).path
        if path == "graphql":
            data = json.loads(kwargs["input"])
            if "timelineItems" in data["query"]:
                self.calls.append(("batch", data))
                response = (self.batch_responses.pop(0) if self.batch_responses else
                            {"data": {"repository": {"item1": deepcopy(self.node)}}})
            else:
                self.calls.append(("content", data))
                kind = "issue" if self.item.kind == "issue" else "pullRequest"
                node = self.content_responses.pop(0) if self.content_responses else deepcopy(self.node)
                response = {"data": {"repository": {kind: node}}}
        else:
            self.calls.append((path, None))
            if path.endswith("/timeline"):
                response = deepcopy(self.timeline)
            elif path.endswith("/comments"):
                response = deepcopy(self.comments)
            elif path.endswith("/permission"):
                login = path.split("/")[-2]
                response = {"role_name": self.roles.get(login, "none")}
            else:
                raise AssertionError(f"Unexpected fixture request: {path}")
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")


class ApprovalBatchTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def loop(self, runner):
        github = GitHub("org/project", runner)
        # PR feedback is outside this optimization and retains its readers.
        github.reviews = lambda _: []
        github.review_comments = lambda _: []
        return Loop(config(self.root, agent(self.root)), github, "operator")

    def check_pair(self, item, node, timeline, setup=None):
        ordinary = ApprovalFixtureRunner(item, deepcopy(node), deepcopy(timeline))
        bulk = ApprovalFixtureRunner(item, deepcopy(node), deepcopy(timeline))
        if setup:
            setup(ordinary, bulk)
        expected = self.loop(ordinary).input_check(item)
        loop = self.loop(bulk)
        discovery = Discovery(loop.github)
        discovery.prepare_approval_inputs([item])
        actual = loop.input_check(item, discovery)
        self.assertEqual(actual, expected)  # Includes snapshots, cleared IDs and byte-identical gate keys.
        return actual, bulk, discovery

    def test_recorded_issue_and_pr_verdicts_match_including_gate_keys(self):
        for kind in ("issue", "pr"):
            for case in FIXTURES["cases"]:
                with self.subTest(kind=kind, case=case["name"]):
                    result, runner, discovery = self.check_pair(*fixture(case, kind))
                    self.assertEqual(result.allowed, case["allowed"])
                    self.assertEqual(any(name == "content" for name, _ in runner.calls), case.get("fallback", False))
                    self.assertEqual(any(name.endswith("/timeline") for name, _ in runner.calls), case.get("fallback", False))
                    if case["name"] in {"bot actor", "ghost actor"}:
                        role_paths = [name for name, _ in runner.calls if name.endswith("/permission")]
                        self.assertFalse(any("helper" in name or "ghost" in name for name in role_paths))
                    if case["name"] == "deleted edit":
                        content = discovery.issue_content(1) if kind == "issue" else discovery.pr_content(1)
                        self.assertIsNone(historical_content(content, [], 1767225612))

    def test_mapping_uncertainty_falls_back_for_both_reads(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "issue")
        mutations = [
            lambda n: n["timelineItems"]["nodes"][0]["actor"].update(__typename="Organization"),
            lambda n: n["timelineItems"]["nodes"][0]["actor"].update(__typename="Mannequin"),
            lambda n: n["timelineItems"]["nodes"][0]["actor"].update(__typename="EnterpriseUserAccount"),
            lambda n: n["timelineItems"]["nodes"][0].update(__typename="UnknownEvent"),
            lambda n: n["timelineItems"]["nodes"][0].update(label=None),
            lambda n: n["timelineItems"]["nodes"][0]["label"].pop("updatedAt"),
            lambda n: n["timelineItems"]["nodes"][0]["label"].update(updatedAt=iso(1767225605)),
            lambda n: n["timelineItems"].update(totalCount=2),
            lambda n: n["userContentEdits"]["pageInfo"].update(hasNextPage=True, endCursor="next"),
            lambda n: n["timelineItems"]["pageInfo"].update(hasNextPage=True, endCursor="next"),
            lambda n: n.update(lastEditedAt=iso(1767225610)),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                def setup(_, bulk):
                    altered = deepcopy(node)
                    mutate(altered)
                    bulk.batch_responses = [{"data": {"repository": {"item1": altered}}}]
                result, runner, discovery = self.check_pair(item, node, timeline, setup)
                self.assertTrue(result.allowed)
                self.assertNotIn(1, discovery.bulk_inputs)
                self.assertEqual(sum(name == "batch" for name, _ in runner.calls), 1)
                self.assertEqual(sum(name == "content" for name, _ in runner.calls), 1)
                self.assertEqual(sum(name.endswith("/timeline") for name, _ in runner.calls), 1)

    def test_approval_records_use_rename_history_and_deleted_edits_prove_no_digest(self):
        cases = {c["name"]: c for c in FIXTURES["cases"]}
        for kind in ("issue", "pr"):
            for name in ("trusted rename", "body edited after start", "deleted edit"):
                with self.subTest(kind=kind, case=name):
                    item, node, timeline = fixture(cases[name], kind)
                    title = "Requirements" if name == "trusted rename" else item.title
                    at = "2026-01-01T00:00:08Z" if name == "trusted rename" else "2026-01-01T00:00:12Z"
                    record = {"id": 601, "body": approval_body(1, title, item.body, [],
                              head=item.head if kind == "pr" else None),
                              "created_at": at, "updated_at": at,
                              "user": {"login": "maintainer", "type": "User"}}
                    def setup(ordinary, bulk):
                        ordinary.comments = [deepcopy(record)]
                        bulk.comments = [deepcopy(record)]
                    result, _, _ = self.check_pair(item, node, timeline, setup)
                    self.assertEqual(result.allowed, name != "deleted edit")

    def test_outside_pr_head_verdicts_and_cleared_feedback_match(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "pr")
        node["author"] = {"login": "outsider", "__typename": "User"}
        outside = {"id": 602, "body": "Outside feedback", "created_at": "2026-01-01T00:00:08Z",
                   "updated_at": "2026-01-01T00:00:08Z", "user": {"login": "outsider", "type": "User"}}
        for approved in (False, True):
            with self.subTest(approved=approved):
                def setup(ordinary, bulk):
                    comments = [deepcopy(outside)]
                    if approved:
                        comments.append({"id": 603, "body": approval_body(1, item.title, item.body, [outside], head=item.head),
                                         "created_at": "2026-01-01T00:00:12Z", "updated_at": "2026-01-01T00:00:12Z",
                                         "user": {"login": "maintainer", "type": "User"}})
                    ordinary.comments = deepcopy(comments)
                    bulk.comments = deepcopy(comments)
                result, _, _ = self.check_pair(item, node, timeline, setup)
                self.assertEqual(result.allowed, approved)
                self.assertEqual(result.cleared_comment_ids, frozenset({602}) if approved else frozenset())

    def test_partial_or_graphql_error_batch_supplies_no_items_and_is_not_retried(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "issue")
        for response in ({"data": {"repository": {"item1": node}}, "errors": [{"message": "Unavailable"}]},
                         {"data": {"repository": {"item1": node}}},
                         {"data": {"repository": {"item1": node, "item2": None}}}):
            with self.subTest(response=response):
                runner = ApprovalFixtureRunner(item, node, timeline)
                runner.batch_responses = [response]
                loop = self.loop(runner)
                discovery = Discovery(loop.github)
                discovery.prepare_approval_inputs([item, replace(item, number=2)])
                self.assertEqual(discovery.bulk_inputs, set())
                self.assertEqual(discovery.cache, {})
                discovery.prepare_approval_inputs([item])
                self.assertTrue(loop.input_check(item, discovery).allowed)
                self.assertEqual(sum(name == "batch" for name, _ in runner.calls), 1)

    def test_followups_discard_batch_content_and_detect_changes_between_pages(self):
        for kind in ("issue", "pr"):
            item, node, timeline = fixture(FIXTURES["cases"][0], kind)
            for field, changed in (("title", "Changed"), ("body", "Changed"),
                                   ("lastEditedAt", iso(1767225610)), ("headRefOid", "b" * 40)):
                if kind == "issue" and field == "headRefOid":
                    continue
                with self.subTest(kind=kind, field=field):
                    def setup(ordinary, bulk):
                        paged = deepcopy(node)
                        paged["userContentEdits"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
                        altered = deepcopy(node) | {field: changed}
                        ordinary.content_responses = [deepcopy(paged), deepcopy(altered)]
                        bulk.content_responses = [deepcopy(paged), deepcopy(altered)]
                        bulk.batch_responses = [{"data": {"repository": {"item1": deepcopy(paged)}}}]
                    result, runner, discovery = self.check_pair(item, node, timeline, setup)
                    self.assertFalse(result.allowed)
                    self.assertIn("unreadable", result.reason)
                    self.assertNotIn(1, discovery.bulk_inputs)
                    self.assertEqual(sum(name == "content" for name, _ in runner.calls), 2)

    def test_followups_use_complete_fresh_content_for_timeline_overflow(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "issue")
        runner = ApprovalFixtureRunner(item, node, timeline)
        overflow = deepcopy(node)
        overflow["timelineItems"]["pageInfo"]["hasNextPage"] = True
        runner.batch_responses = [{"data": {"repository": {"item1": overflow}}}]
        runner.content_responses = [deepcopy(node) | {"body": "Changed after batch"}]
        loop = self.loop(runner)
        discovery = Discovery(loop.github)
        discovery.prepare_approval_inputs([item])
        result = loop.input_check(item, discovery)
        self.assertFalse(result.allowed)
        self.assertIn("changed", result.reason)
        self.assertNotIn(1, discovery.bulk_inputs)

    def test_followups_complete_long_edit_histories_using_only_per_item_pages(self):
        for kind in ("issue", "pr"):
            with self.subTest(kind=kind):
                item, node, timeline = fixture(FIXTURES["cases"][0], kind)
                edits = [{"editedAt": iso(1767225600 + n), "editor": {"login": "operator", "__typename": "User"},
                          "diff": item.body, "deletedAt": None} for n in range(1, 102)]
                first, last = deepcopy(node), deepcopy(node)
                for page in (first, last):
                    page["lastEditedAt"] = edits[-1]["editedAt"]
                first["userContentEdits"] = {"nodes": edits[:100], "totalCount": 101,
                                             "pageInfo": {"hasNextPage": True, "endCursor": "next"}}
                last["userContentEdits"] = {"nodes": edits[100:], "totalCount": 101,
                                            "pageInfo": {"hasNextPage": False, "endCursor": None}}
                def setup(ordinary, bulk):
                    ordinary.content_responses = deepcopy([first, last])
                    bulk.content_responses = deepcopy([first, last])
                    bulk.batch_responses = [{"data": {"repository": {"item1": deepcopy(first)}}}]
                result, runner, discovery = self.check_pair(item, node, timeline, setup)
                self.assertTrue(result.allowed)
                self.assertNotIn(1, discovery.bulk_inputs)
                calls = [data for name, data in runner.calls if name == "content"]
                self.assertEqual([c["variables"]["cursor"] for c in calls], [None, "next"])

    def test_cold_observation_batches_and_warm_pass_reads_no_approval_input(self):
        self.assertEqual(APPROVAL_BATCH_SIZE, 50)
        runner = DiscoveryCostRunner()
        # Repeat the recording audit fixture beyond one batch.
        original = deepcopy(runner.rows[0])
        runner.rows = [original | {"number": n, "comments": 0, "issue_dependencies_summary":
                                  dict.fromkeys(("blocked_by", "blocking", "total_blocked_by", "total_blocking"), 0)}
                       for n in range(1, 124)]
        runner.comments = {n: [] for n in range(1, 124)}
        loop = Loop(config(self.root, agent(self.root)), GitHub("org/project", runner), "operator")
        for claiming in (False, True):
            loop.discovery = Discovery(loop.github)
            runner.calls.clear()
            with patch("ub_agents.github.timestamp", return_value=1000):
                plans = list(loop.iter_plans(claiming=claiming))
            self.assertEqual(len(plans), 123)
            queries = [c for c in runner.calls if c[c.index("--include") + 1] == "graphql"]
            self.assertEqual(len(queries), 123 if claiming else 3)
            self.assertFalse(any("/timeline" in c[c.index("--include") + 1] for c in runner.calls))
            runner.calls.clear()
            with patch("ub_agents.github.timestamp", return_value=1000):
                self.assertEqual(list(loop.iter_plans(claiming=claiming)), plans)
            self.assertFalse(any(c[c.index("--include") + 1] == "graphql" for c in runner.calls))

    def test_milestone_claiming_batches_only_reached_plans_and_preserves_call_ceiling(self):
        class MilestoneRunner(DiscoveryCostRunner):
            def __call__(self, command, **kwargs):
                path = urlsplit(command[command.index("--include") + 1]).path
                if path.endswith("/milestones"):
                    self.calls.append(command)
                    return subprocess.CompletedProcess(command, 0, json.dumps([
                        {"number": n, "state": "open", "created_at": iso(n), "open_issues": 83}
                        for n in (10, 20)]), "")
                response = super().__call__(command, **kwargs)
                if path == "graphql" and "timelineItems" in kwargs["input"]:
                    data = json.loads(response.stdout)
                    node = data["data"]["repository"].get("item1")
                    if node:
                        node["timelineItems"].update(nodes=[], totalCount=0)
                    return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
                if path.endswith("/issues/1/timeline"):
                    return subprocess.CompletedProcess(command, 0, "[]", "")
                return response
        counts = []
        for bulk in (False, True):
            runner = MilestoneRunner()
            original = deepcopy(runner.rows[0])
            runner.rows = [original | {"number": n, "comments": 0, "milestone": {"number": 10 if n <= 2 else 20}}
                           for n in range(1, 84)]
            runner.comments = {n: [] for n in range(1, 84)}
            github = GitHub("org/project", runner)
            if not bulk:
                github.approval_inputs = None
            loop = Loop(config(self.root, agent(self.root), queue=Queue("gate")), github, "operator", output=lambda _: None)
            with patch.object(loop, "execute", return_value=True) as execute, \
                    patch.object(loop, "park_approval"), patch("ub_agents.github.timestamp", return_value=1000):
                self.assertTrue(loop.tick())
            self.assertEqual(execute.call_args.args[0].item.number, 2)
            counts.append(len(runner.calls))
            paths = [c[c.index("--include") + 1] for c in runner.calls]
            self.assertFalse(any("/issues/3/" in path for path in paths))
            if bulk:
                self.assertEqual(sum(path == "graphql" for path in paths), 3)  # Graph and two reached inputs.
                self.assertFalse(any("/timeline" in path for path in paths))
        self.assertEqual(counts[1], counts[0] - 2)

    def test_copied_observation_cache_retains_rest_title_gate_fallback(self):
        case = next(c for c in FIXTURES["cases"] if c["name"] == "trusted rename")
        item, node, timeline = fixture(case, "issue")
        runner = ApprovalFixtureRunner(item, node, timeline)
        loop = self.loop(runner)
        loop.discovery.prepare_approval_inputs([item])
        worker = RunPlanning(loop, 0)
        self.assertEqual(worker.planner.discovery.bulk_inputs, {1})
        runner.roles["operator"] = "none"
        actual = worker.planner.input_check(item, worker.planner.discovery)
        expected = self.loop(runner).input_check(item)
        self.assertEqual(actual, expected)
        self.assertEqual(actual.gate, "input")
        self.assertNotIn(1, worker.planner.discovery.bulk_inputs)
        self.assertEqual(loop.discovery.bulk_inputs, {1})

    def test_fresh_input_checks_ignore_bulk_discovery_cache(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "issue")
        runner = ApprovalFixtureRunner(item, node, timeline)
        loop = self.loop(runner)
        loop.discovery.prepare_approval_inputs([item])
        self.assertTrue(loop.input_check(item, loop.discovery).allowed)
        runner.timeline = []
        self.assertFalse(loop.input_check(item).allowed)
        self.assertEqual(sum(name.endswith("/timeline") for name, _ in runner.calls), 1)
