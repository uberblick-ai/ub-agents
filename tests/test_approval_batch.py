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
from tests.support import DiscoveryCostRunner, PollGitHub, agent, config, issue, pr, stub_refresh


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
                response = {"role_name": {"operator": "write", "maintainer": "maintain"}.get(login, "none")}
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

    def test_cold_observation_batches_and_warm_pass_reads_no_approval_input(self):
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
        class BulkGitHub(PollGitHub):
            def approval_inputs(self, items):
                self.reads.append(("approval_inputs", tuple(i.number for i in items)))
                return {i.number: (PollGitHub.issue_content(self, i.number), PollGitHub.timeline(self, i.number))
                        for i in items}
        github = BulkGitHub(*(issue(n, milestone=10 if n <= 2 else 20) for n in range(1, 84)))
        github.milestones = [{"number": n, "state": "open", "created_at": iso(n)} for n in (10, 20)]
        github.timelines[1] = []
        loop = Loop(config(self.root, agent(self.root), queue=Queue("gate")), github, "operator")
        with patch.object(loop, "execute", return_value=True), patch.object(loop, "park_approval"):
            self.assertTrue(loop.tick())
        self.assertEqual([args for name, args in github.reads if name == "approval_inputs"], [(1,), (2,)])
        self.assertEqual({args[0] for name, args in github.reads if name in {"issue_content", "timeline"}}, {1, 2})

    def test_fresh_input_checks_ignore_bulk_discovery_cache(self):
        item, node, timeline = fixture(FIXTURES["cases"][0], "issue")
        runner = ApprovalFixtureRunner(item, node, timeline)
        loop = self.loop(runner)
        loop.discovery.prepare_approval_inputs([item])
        self.assertTrue(loop.input_check(item, loop.discovery).allowed)
        runner.timeline = []
        self.assertFalse(loop.input_check(item).allowed)
        self.assertEqual(sum(name.endswith("/timeline") for name, _ in runner.calls), 1)
