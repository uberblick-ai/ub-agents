from copy import deepcopy
from contextlib import chdir
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ub_agents.coordination import Plan
from ub_agents.approvals import ApprovalCheck
from ub_agents.config import Runtime
from ub_agents.errors import GitHubError
from ub_agents.execution import group_members
from ub_agents.loop import Loop, _GracefulStop
from ub_agents.notices import ACTION_MARKER, Notices
from ub_agents.observations import (MAX_BYTES, MAX_OUTCOMES, MAX_PLANS,
                                   MAX_TEXT, RETAINED_SESSIONS, STALE_SECONDS,
                                   Observations, Publisher)
from ub_agents.observation_worker import prune, stale, write_snapshot
from ub_agents.records import iso, records, timestamp
from ub_agents.view_data import Session, local_description, work_rows
from ub_agents.view_worker import LocalWorker, Request
from tests.support import FakeGitHub, MemoryPublisher, PollGitHub, agent, config, issue, pr, observation_writer_command, stub_refresh


class ObservationTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = config(self.root, agent(self.root, kind="issue"))
        self.memory = MemoryPublisher()
        self.observer = Observations(self.cfg, "operator", self.root / "ub-agents.yaml", self.memory)

    def loop(self, github, observer=True):
        return Loop(self.cfg, github, "operator", output=lambda *_: None,
                    observer=self.observer if observer else None)

    def test_action_notice_post_deduplication_and_claim_clear_snapshot(self):
        github = FakeGitHub(issue())
        co = self.loop(github).coordinator
        plan = co.plan(github.item(1), self.cfg.agents[0], self.cfg.stop_labels)
        self.observer.begin_pass()
        self.observer.plan(plan)
        lease = co.claim(plan, self.cfg.stop_labels)
        co.update(lease, state='running', started=True)
        outcome = co.report(lease, 'blocked', 'Choose a direction', action="Maintainer: choose A or B; recommend A.")
        co.release(lease, 'blocked', outcome['summary'])
        comment = next(c for c in github.comments(1) if c['body'].startswith(ACTION_MARKER))
        cached = self.memory.snapshots[-1]['action_needed']['1']
        self.assertEqual(cached['text'], comment['body'])
        self.assertEqual(cached['created_at'], comment['created_at'])
        self.assertTrue(self.memory.snapshots[-1]['coordination_authors']['operator']['trusted'])
        self.observer.action_needed(1, None)
        co.notices.post_action(1, lease, outcome, outcome['summary'], ())
        self.assertEqual(self.memory.snapshots[-1]['action_needed']['1'], cached)
        self.assertEqual(len([c for c in github.comments(1) if c['body'].startswith(ACTION_MARKER)]), 1)
        # The next claim clears the advisory even when minimization fails.
        with patch.object(github, 'unminimized_comments', side_effect=RuntimeError('unavailable')):
            co.notices.resumed(1)
        self.assertEqual(self.memory.snapshots[-1]['action_needed'], {})

    def test_approval_notice_post_and_own_existing_gate_are_cached_but_foreign_is_not(self):
        github = FakeGitHub(issue())
        notices = self.loop(github).coordinator.notices
        gate = ApprovalCheck(False, 'Outside input needs approval', gate='start', gate_key='input')
        notices.approval(1, gate, ('needs-human',), ('ready',))
        comment = next(c for c in github.comments(1) if c['body'].startswith(ACTION_MARKER))
        self.assertEqual(self.memory.snapshots[-1]['action_needed']['1']['text'], comment['body'])
        self.observer.action_needed(1, None)
        restarted = Notices(github, 'operator', on_action=self.observer.action_needed)
        restarted.approval(1, gate, ('needs-human',), ('ready',))
        self.assertEqual(self.memory.snapshots[-1]['action_needed']['1']['created_at'], comment['created_at'])
        self.observer.action_needed(1, None)
        github.store[1][-1]['user']['login'] = 'maintainer'
        foreign = Notices(github, 'operator', on_action=self.observer.action_needed)
        foreign.approval(1, gate, ('needs-human',), ('ready',))
        self.assertEqual(self.memory.snapshots[-1]['action_needed'], {})

    def test_new_claim_or_reset_observed_in_history_drops_action_text(self):
        github = FakeGitHub(issue())
        comment = github.create_comment(1, ACTION_MARKER + 'run -->\n**Action needed**\nDecision')
        self.observer.begin_pass()
        for kind in ('lease', 'reset'):
            with self.subTest(kind=kind):
                self.observer.action_needed(1, comment)
                plan = Plan(issue(), self.cfg.agents[0], None, 'ready', '', 1, history=({
                    'kind': kind, 'id': comment['id'] + 1, 'assignment': 1, 'agent': 'worker',
                    'run': 'new', 'created': iso(1000), 'expires': iso(2000), 'state': 'claiming'},))
                self.observer.plan(plan)
                self.assertEqual(self.memory.snapshots[-1]['action_needed'], {})
                self.observer.action_needed(1, comment)
                self.observer.record(plan.history[0])
                self.assertEqual(self.memory.snapshots[-1]['action_needed'], {})

    def test_action_text_and_author_metadata_stay_bounded_without_losing_work(self):
        github = FakeGitHub(issue())
        self.observer.begin_pass()
        self.observer.plan(Plan(issue(), self.cfg.agents[0], None, 'blocked', 'Reason', 1))
        comment = github.create_comment(1, ACTION_MARKER + 'run -->\n' + '😀' * 3000)
        for number in range(150):
            self.observer.action_needed(number + 1, dict(comment, id=number + 1))
            self.observer.coordination_author(f'writer-{number}', True, None)
        self.assertEqual(len(self.observer.state['action_needed']), 128)
        self.assertEqual(len(self.observer.state['coordination_authors']), 128)
        self.assertEqual(len(self.observer.state['action_needed']['150']['text']), MAX_TEXT)
        self.assertGreater(self.observer.state['action_needed']['150']['omitted_characters'], 0)
        snapshot = self.memory.snapshots[-1]
        self.assertLess(len(snapshot['action_needed']), 128)
        self.assertEqual(len(snapshot['latest_pass']['rows']), 1)
        self.assertLessEqual(Observations.byte_size(snapshot), MAX_BYTES)

    def test_author_observations_use_existing_role_reads_and_fail_closed(self):
        github = FakeGitHub(issue())
        loop = self.loop(github)
        trust = loop.coordinator.trust.observation()
        with patch.object(github, 'role', wraps=github.role) as role:
            self.assertTrue(trust({'login': 'operator'}))
            publications = len(self.memory.snapshots)
            self.assertTrue(trust({'login': 'OPERATOR'}))
            self.assertEqual(role.call_count, 1)
            self.assertEqual(len(self.memory.snapshots), publications)
        self.assertTrue(self.memory.snapshots[-1]['coordination_authors']['operator']['trusted'])
        github.roles['operator'] = None
        with self.assertRaises(GitHubError):
            loop.coordinator.trust.reason('operator')
        cached = self.memory.snapshots[-1]['coordination_authors']['operator']
        self.assertFalse(cached['trusted'])
        self.assertIn('could not be read', cached['reason'])

    def test_partial_pass_keeps_rows_details_and_order_until_completion(self):
        plans = [Plan(replace(issue(n), body=f'Body {n}'), self.cfg.agents[0], None,
                      state, 'Reason', 1, history=({'kind': 'lease', 'assignment': n,
                      'agent': 'worker', 'run': f'run-{n}', 'created': iso(1000 + n),
                      'expires': iso(2000 + n), 'state': 'released', 'summary': f'History {n}'},))
                 for n, state in ((1, 'ready'), (2, 'ready'), (3, 'waiting'), (4, 'blocked'))]
        self.observer.begin_pass()
        for plan in plans:
            self.observer.plan(plan)
        self.observer.complete_pass()
        previous = deepcopy(self.memory.snapshots[-1])
        path = self.root / '.ub-agents' / 'sessions' / 'launcher.json'
        path.parent.mkdir(parents=True)
        worker = LocalWorker(self.root, path)
        def published(selected='plan:4:worker'):
            snapshot = self.memory.snapshots[-1]
            path.write_text(json.dumps(snapshot))
            result = worker.read(Request(selected, 1))
            self.assertIsNone(result.session.error)
            return snapshot, work_rows(result.session, self.root), result
        published()
        self.observer.begin_pass()
        changes = [replace(plans[1], item=replace(plans[1].item, body='New body 2')),
                   Plan(issue(5), self.cfg.agents[0], None, 'ready', 'New', 1),
                   replace(plans[2], state='ready'), plans[0]]
        expected_order = ([1, 2, 3], [1, 2, 3], [1, 2, 5, 3], [1, 2, 3, 5], [1, 2, 3, 5])
        for step, eligible in enumerate(expected_order):
            if step:
                self.observer.plan(changes[step - 1])
            snapshot, work, result = published()
            self.assertEqual(snapshot['latest_pass']['state'], 'partial')
            self.assertEqual(len(work), len({row.key for row in work}))
            self.assertTrue({1, 2, 3, 4}.issubset(row.item for row in work))
            self.assertEqual([row.item for row in work if row.group == 'Eligible'], eligible)
            kept = next(row for row in work if row.item == 4)
            self.assertEqual(kept.data['history'], previous['histories']['4'])
            self.assertEqual(local_description(kept, result.session).body, 'Body 4')
            self.assertEqual(next(row for row in result.rows if row.item == 4).state, 'blocked')
        self.observer.complete_pass()
        snapshot, work, result = published()
        self.assertEqual(snapshot['latest_pass']['state'], 'complete')
        self.assertEqual([row.item for row in work], [2, 5, 3, 1])
        self.assertNotIn('4', snapshot['histories'])
        earlier = next(row for row in result.rows if row.item == 4)
        self.assertEqual(earlier.state, 'earlier observation')
        self.assertEqual(earlier.data['history'], previous['histories']['4'])
        self.assertEqual(result.description.body, 'Body 4')
        # A later begin also keeps rows from a pass interrupted by execution.
        self.observer.begin_pass()
        self.observer.plan(replace(changes[0], state='waiting'))
        self.observer.begin_pass()
        self.assertEqual([row['item'] for row in self.memory.snapshots[-1]['latest_pass']['rows']], [2, 5, 3, 1])
        _, work, _ = published('plan:2:worker')
        self.assertEqual([row.item for row in work if row.group == 'Eligible'], [5, 3, 1, 2])

    def test_partial_pass_drops_finished_or_untriggered_items_before_reaching_them(self):
        for item, changes in ((pr(3), {'state': 'merged'}),
                              (issue(3), {'state': 'closed'}),
                              (issue(3), {'labels': frozenset()})):
            with self.subTest(kind=item.kind, changes=changes):
                self.cfg = config(self.root, agent(self.root))
                self.observer = Observations(self.cfg, 'operator', None, self.memory)
                github = PollGitHub(pr(2), item, issue(4))
                loop = self.loop(github)
                self.observer.begin_pass()
                list(loop.iter_plans())
                self.observer.complete_pass()
                kept = deepcopy(self.memory.snapshots[-1]['latest_pass']['rows'][-1])
                github.change(3, **changes)
                github.reads.clear()
                with patch.object(loop, 'execute', return_value=True) as execute:
                    self.assertTrue(loop.tick())
                self.assertEqual(execute.call_args.args[0].item.number, 2)
                snapshot = self.memory.snapshots[-1]
                self.assertEqual(snapshot['latest_pass']['state'], 'partial')
                self.assertEqual([row['item'] for row in snapshot['latest_pass']['rows']], [2, 4])
                self.assertEqual(snapshot['latest_pass']['rows'][-1], kept)
                self.assertNotIn('3', snapshot['histories'])
                self.assertNotIn((3, 'worker'), self.observer.kept_keys)
                session = Session(self.root / 'launcher.json', snapshot)
                self.assertEqual([row.item for row in work_rows(session, self.root)
                                  if row.group == 'Eligible'], [2, 4])
                self.assertFalse(any(name in {'item', 'comments', 'timeline', 'issue_content'}
                                     and args[0] in {3, 4} for name, args in github.reads))

    def test_partial_pass_keeps_needs_human_handoff_before_reaching_it(self):
        self.cfg = config(self.root, agent(self.root, outcomes={
            'done': {'add': ('needs-human',), 'remove': ()}}))
        self.observer = Observations(self.cfg, 'operator', None, self.memory)
        github = PollGitHub(issue(), pr(labels=(), body='Independent change'))
        loop = self.loop(github)
        def finish(*args, **kwargs):
            lease = loop.coordinator.history(1)[0]
            loop.coordinator.report(lease, 'success', 'Human follow-up needed', outcome='done',
                                    action='Maintainer: choose A or B; recommend A.')
            return 0
        with patch('ub_agents.loop.supervise', side_effect=finish):
            self.assertTrue(loop.tick())
        self.assertEqual(github.items[1].labels, frozenset({'needs-human'}))
        self.assertFalse(loop.tick())
        previous = deepcopy(self.memory.snapshots[-1])
        self.assertEqual([(row['item'], row['state']) for row in previous['latest_pass']['rows']],
                         [(1, 'parked')])
        github.change(2, labels=frozenset({'needs-changes'}))
        github.reads.clear()
        # Repeated partial passes must keep the handoff, its details and history.
        for _ in range(2):
            with patch.object(loop, 'execute', return_value=True) as execute:
                self.assertTrue(loop.tick())
            self.assertEqual(execute.call_args.args[0].item.number, 2)
            snapshot = self.memory.snapshots[-1]
            self.assertEqual(snapshot['latest_pass']['state'], 'partial')
            self.assertEqual(snapshot['latest_pass']['rows'][0], previous['latest_pass']['rows'][0])
            self.assertEqual(snapshot['histories']['1'], previous['histories']['1'])
            self.assertIn((1, 'worker'), self.observer.kept_keys)
            session = Session(self.root / 'launcher.json', snapshot)
            self.assertEqual([(row.item, row.state) for row in work_rows(session, self.root)
                              if row.group == 'Needs attention'], [(1, 'parked')])
        self.assertFalse(any(name in {'item', 'comments', 'timeline', 'issue_content'}
                             and args[0] == 1 for name, args in github.reads))

    def test_discovery_only_drops_untriggered_eligible_or_closed_carried_rows(self):
        for state, kept_open in (('ready', False), ('recover', False), ('backoff', False),
                                 ('waiting', False), ('parked', True), ('blocked', True),
                                 ('owned', True), ('failed', True)):
            for read in ('open', 'closed', 'missing'):
                with self.subTest(state=state, read=read):
                    self.observer = Observations(self.cfg, 'operator', None, self.memory)
                    self.observer.begin_pass()
                    self.observer.plan(Plan(issue(), self.cfg.agents[0], None, state, 'Reason', 1))
                    self.observer.complete_pass()
                    previous = deepcopy(self.memory.snapshots[-1])
                    self.observer.begin_pass()
                    items = {} if read == 'missing' else {
                        1: replace(issue(), state=read, labels=frozenset())}
                    self.observer.discovered(items, self.cfg.agents, all_open=read == 'missing')
                    snapshot = self.memory.snapshots[-1]
                    if not kept_open or read != 'open':
                        self.assertEqual(snapshot['latest_pass']['rows'], [])
                        self.assertEqual(snapshot['histories'], {})
                        self.assertEqual(self.observer.kept_keys, set())
                    else:
                        self.assertEqual(snapshot['latest_pass']['rows'], previous['latest_pass']['rows'])
                        self.assertEqual(snapshot['histories'], previous['histories'])
                        self.assertEqual(self.observer.kept_keys, {(1, 'worker')})

    def test_partial_pass_drops_previous_trigger_agent_and_keeps_matching_agent(self):
        reviewer = agent(self.root, name='reviewer', kind='pr', triggers=('needs-review',))
        integrator = agent(self.root, name='integrator', kind='pr', triggers=('ready-to-merge',))
        self.cfg = config(self.root, reviewer, integrator)
        for labels in (('needs-review',), ('needs-review', 'ready-to-merge')):
            with self.subTest(labels=labels):
                self.observer = Observations(self.cfg, 'operator', None, self.memory)
                github = PollGitHub(pr(2, labels=('needs-review',)), pr(3, labels=labels),
                                    pr(4, labels=('needs-review',)))
                loop = self.loop(github)
                self.observer.begin_pass()
                list(loop.iter_plans())
                self.observer.complete_pass()
                previous = deepcopy(self.memory.snapshots[-1]['latest_pass']['rows'])
                github.change(3, labels=frozenset({'ready-to-merge'}), body='Updated candidate')
                github.reads.clear()
                with patch.object(loop, 'execute', return_value=True) as execute:
                    self.assertTrue(loop.tick())
                self.assertEqual(execute.call_args.args[0].item.number, 2)
                snapshot = self.memory.snapshots[-1]
                self.assertEqual(snapshot['latest_pass']['state'], 'partial')
                expected = [row for row in previous[1:] if row['agent'] != 'reviewer' or row['item'] != 3]
                self.assertEqual(snapshot['latest_pass']['rows'][1:], expected)
                self.assertFalse(any(name in {'item', 'comments', 'pr_content'} and args[0] in {3, 4}
                                     for name, args in github.reads))
                # Once reached, the integrator gets a new row or replaces its retained row.
                github.change(2, labels=frozenset())
                with patch.object(loop, 'execute', return_value=True) as execute:
                    self.assertTrue(loop.tick())
                self.assertEqual((execute.call_args.args[0].item.number, execute.call_args.args[0].agent.name),
                                 (3, 'integrator'))
                snapshot = self.memory.snapshots[-1]
                self.assertEqual(snapshot['latest_pass']['state'], 'partial')
                rows = snapshot['latest_pass']['rows']
                order = [(3, 'integrator'), (4, 'reviewer')]
                self.assertEqual([(row['item'], row['agent']) for row in rows],
                                 order if 'ready-to-merge' in labels else order[::-1])
                self.assertEqual(next(row for row in rows if row['item'] == 3)['description']['text'],
                                 'Updated candidate')
                self.assertEqual(next(row for row in rows if row['item'] == 4), previous[-1])

    def test_partial_pass_replaces_dropped_row_with_unfinished_run_recovery(self):
        for changes in ({'state': 'closed'}, {'labels': frozenset()}):
            with self.subTest(changes=changes):
                self.observer = Observations(self.cfg, 'operator', None, self.memory)
                github = PollGitHub(issue(), issue(3))
                loop = self.loop(github)
                now = timestamp()
                loop.coordinator.clock = lambda: now
                self.observer.begin_pass()
                plans = list(loop.iter_plans())
                self.observer.complete_pass()
                lease = loop.coordinator.claim(plans[0])
                loop.coordinator.update(lease, state='running', started=True)
                loop.coordinator.report(lease, 'success', 'Finished before outage', outcome='done')
                github.change(1, **changes)
                loop.coordinator.clock = lambda: now + 61
                with patch.object(loop, 'recover', return_value=True) as recover:
                    self.assertTrue(loop.tick())
                self.assertEqual(recover.call_args.args[0].item.number, 1)
                snapshot = self.memory.snapshots[-1]
                self.assertEqual(snapshot['latest_pass']['state'], 'partial')
                self.assertEqual([(row['item'], row['state']) for row in snapshot['latest_pass']['rows']],
                                 [(3, 'ready'), (1, 'recover')])
                self.assertNotIn((1, 'worker'), self.observer.kept_keys)
                self.assertIn('1', snapshot['histories'])
                # A later discovery read must not discard this pass's new recovery plan.
                self.observer.discovered(github.items, self.cfg.agents, all_open=True)
                self.assertEqual(self.memory.snapshots[-1]['latest_pass'], snapshot['latest_pass'])

    def test_targeted_item_read_only_drops_that_items_stale_rows(self):
        github = PollGitHub(issue(), issue(3))
        loop = self.loop(github)
        self.observer.begin_pass()
        list(loop.iter_plans())
        self.observer.complete_pass()
        kept = deepcopy(self.memory.snapshots[-1]['latest_pass']['rows'][0])
        github.change(3, state='closed')
        github.reads.clear()
        self.observer.begin_pass()
        item, _ = loop.item_plans(3)
        self.assertEqual(item.state, 'closed')
        snapshot = self.memory.snapshots[-1]
        self.assertEqual(snapshot['latest_pass']['state'], 'partial')
        self.assertEqual(snapshot['latest_pass']['rows'], [kept])
        self.assertEqual(set(snapshot['histories']), {'1'})
        self.assertFalse(any(name == 'observe' or (name == 'item' and args[0] == 1)
                             for name, args in github.reads))

    def test_kept_agent_uses_its_previous_description_and_item_history(self):
        first = Plan(replace(issue(), body='Previous body'), self.cfg.agents[0], None,
                     'ready', 'Ready', 1)
        second = replace(first, agent=replace(first.agent, name='reviewer'))
        self.observer.begin_pass()
        self.observer.plan(first)
        self.observer.plan(second)
        self.observer.complete_pass()
        previous = self.memory.snapshots[-1]['histories']['1']
        self.observer.begin_pass()
        self.observer.plan(replace(first, item=replace(first.item, body='New body'), history=({
            'kind': 'lease', 'assignment': 1, 'agent': 'worker', 'run': 'new-run',
            'created': iso(1000), 'expires': iso(2000), 'state': 'released'},)))
        session = Session(self.root / 'launcher.json', self.memory.snapshots[-1])
        merged, = work_rows(session, self.root)
        refreshed, kept = merged.eligible_plans
        self.assertEqual(local_description(merged, session).body, 'New body')
        self.assertEqual([plan['agent'] for plan in merged.eligible_plans], ['worker', 'reviewer'])
        self.assertEqual(kept['description']['text'], 'Previous body')
        self.assertEqual(kept['history'], previous)
        self.assertEqual(len(refreshed['history']['runs']), 1)
        self.observer.complete_pass()
        self.assertEqual(list(self.memory.snapshots[-1]['histories']), ['1'])

    def test_retained_rows_do_not_use_the_new_pass_plan_budget(self):
        def plan(n):
            return Plan(issue(n), self.cfg.agents[0], None, 'ready', 'Ready', 1)
        self.observer.begin_pass()
        for n in range(1, MAX_PLANS + 1):
            self.observer.plan(plan(n))
        self.observer.complete_pass()
        self.observer.begin_pass()
        for n in range(MAX_PLANS + 1, MAX_PLANS * 2 + 8):
            self.observer.plan(plan(n))
        partial = self.memory.snapshots[-1]
        self.assertEqual(len(partial['latest_pass']['rows']), MAX_PLANS)
        self.assertEqual(partial['omitted']['plans'], MAX_PLANS + 7)
        self.assertEqual(len(self.observer.pass_rows), MAX_PLANS)
        self.assertLessEqual(self.observer.byte_size(partial), MAX_BYTES)
        # A newly assigned item can be outside the partial display's row budget.
        # Its live records must also survive promotion of the new pass's rows.
        self.observer.assignment(plan(MAX_PLANS + 1))
        self.observer.record({'kind': 'lease', 'assignment': MAX_PLANS + 1, 'agent': 'worker',
                              'run': 'current-run', 'created': iso(3000), 'runtime': 'direct',
                              'state': 'running', 'expires': iso(4000)})
        self.observer.complete_pass()
        complete = self.memory.snapshots[-1]
        self.assertEqual([row['item'] for row in complete['latest_pass']['rows']],
                         list(range(MAX_PLANS + 1, MAX_PLANS * 2 + 1)))
        self.assertEqual(complete['omitted']['plans'], 7)
        self.assertEqual(set(complete['histories']), {str(n) for n in range(MAX_PLANS + 1, MAX_PLANS * 2 + 1)})
        self.assertEqual(complete['histories'][str(MAX_PLANS + 1)]['runs'][0]['state'], 'running')

    def test_header_context_uses_plan_failures_and_claims_authoritative_attempt(self):
        plan = Plan(issue(), self.cfg.agents[0], Runtime('codex', 'gpt-6.1-sol', 'xhigh'),
                    'ready', 'Trigger matched', 3)
        self.observer.begin_pass()
        self.observer.plan(plan)
        row = self.memory.snapshots[-1]['latest_pass']['rows'][0]
        self.assertEqual((row['failures'], row['max_attempts'], row['runtime']),
                         (2, plan.agent.max_attempts, 'codex:gpt-6.1-sol:xhigh'))
        self.observer.assignment(plan)
        self.observer.record({'kind': 'lease', 'assignment': 1, 'agent': 'worker', 'run': 'run',
                              'runtime': 'codex:gpt-6.1-sol:xhigh', 'state': 'running',
                              'expires': iso(timestamp()), 'attempt': 4})
        self.assertEqual(self.memory.snapshots[-1]['assignment']['attempt'], 4)
        self.observer.record({'kind': 'outcome', 'assignment': 1, 'agent': 'worker', 'run': 'run',
                              'created': iso(timestamp()), 'status': 'success', 'summary': 'Done', 'handoff': 7})
        outcome = self.memory.snapshots[-1]['outcomes'][0]
        self.assertEqual((outcome['kind'], outcome['title'], outcome['handoff']), ('issue', plan.item.title, 7))

    def test_observation_preserves_request_order_writes_outcomes_and_lazy_evaluation(self):
        results = []
        for observed in (False, True):
            github = PollGitHub(issue(1), issue(2), issue(3), issue(4))
            # The first reached plan parks, the second claims; later plans remain unread.
            github.timelines[1] = []
            loop = self.loop(github, observed)
            loop.coordinator.clock = lambda: 1800000000
            with patch("ub_agents.coordination.uuid.uuid4") as uuid:
                uuid.return_value.hex = "same-run"
                def finish(*args, **kwargs):
                    lease = next(r for r in loop.coordinator.history(2) if r["kind"] == "lease")
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    return 0
                with patch("ub_agents.loop.supervise", side_effect=finish):
                    loop.launch(once=True)
            results.append((github.reads[:], github.writes[:], deepcopy(github.items),
                            records([c for comments in github.store.values() for c in comments])))
            self.assertFalse(any(name in {"timeline", "issue_content", "comments"} and args[0] in {3, 4}
                                 for name, args in github.reads))
        self.assertEqual(results[0], results[1])
        state = self.memory.snapshots[-1]
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [1, 2])
        self.assertEqual(state["latest_pass"]["state"], "partial")
        self.assertEqual(state["outcomes"][0]["acceptance"], "finalized")
        self.assertEqual(state["outcomes"][0]["runtime"], "direct")
        self.assertTrue(state["ended"])

    def test_complete_pass_includes_only_evaluated_plans_and_foreign_owner_without_paths(self):
        github = PollGitHub(issue())
        other = self.loop(github, False)
        lease = other.coordinator.claim(other.plans()[0])
        other.coordinator.update(lease, state="running", host=socket.gethostname(),
                                 log_dir="/private/other/logs", process_group=os.getpid())
        loop = self.loop(github)
        loop.launch(once=True)
        row = self.memory.snapshots[-1]["latest_pass"]["rows"][0]
        self.assertEqual(row["owner"], {"actor": "operator", "host": socket.gethostname(),
                                       "run": lease["run"], "host_reason": None})
        self.assertNotIn("/private/other/logs", json.dumps(self.memory.snapshots))
        self.assertEqual(self.memory.snapshots[-1]["latest_pass"]["state"], "complete")
        self.assertIsNone(self.memory.snapshots[-1]["assignment"])
        history = self.memory.snapshots[-1]["histories"]["1"]
        self.assertEqual([run["time"] for run in history["runs"]], [lease["created"]])
        self.assertEqual(history["runs"][0]["host"], socket.gethostname())

    def test_pr_filing_uses_only_items_already_read_by_discovery(self):
        self.cfg = config(self.root, agent(self.root, kind="pr", triggers=("needs-changes",)))
        filed = replace(issue(), author="bk-one")
        reads = []
        for observed in (False, True):
            github = PollGitHub(filed, pr())
            loop = self.loop(github, observed)
            self.observer.begin_pass()
            list(loop.iter_plans())
            reads.append(github.reads[:])
        self.assertEqual(reads[0], reads[1])
        history = self.memory.snapshots[-1]["histories"]["2"]
        self.assertEqual(history["filing"], {"author": "bk-one", "time": filed.created_at})
        # A targeted pass has not read the closing issue. Its PR author's data
        # cannot stand in for the filing row, and display must not fetch it.
        self.observer.begin_pass()
        github = PollGitHub(filed, replace(pr(), author="pr-author"))
        loop = self.loop(github)
        _, plans = loop.item_plans(2)
        list(plans)
        self.assertIsNone(self.memory.snapshots[-1]["histories"]["2"]["filing"])
        self.assertFalse(any(name == "item" and args[0] == 1 for name, args in github.reads))

    def test_targeted_launch_preserves_request_order_writes_and_outcomes(self):
        results = []
        for observed in (False, True):
            github = PollGitHub(issue(1), issue(2), issue(3))
            loop = self.loop(github, observed)
            loop.coordinator.clock = lambda: 1800000000
            with patch("ub_agents.coordination.uuid.uuid4") as uuid:
                uuid.return_value.hex = "same-run"
                def finish(*args, **kwargs):
                    lease = next(r for r in loop.coordinator.history(2) if r["kind"] == "lease")
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    return 0
                with patch("ub_agents.loop.supervise", side_effect=finish):
                    self.assertEqual(loop.launch(number=2, agent_name="worker"), 0)
            results.append((github.reads[:], github.writes[:], deepcopy(github.items),
                            records([c for comments in github.store.values() for c in comments])))
        self.assertEqual(results[0], results[1])
        state = self.memory.snapshots[-1]
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [2])
        self.assertEqual(state["latest_pass"]["state"], "partial")
        self.assertEqual(state["outcomes"][0]["acceptance"], "finalized")
        self.assertTrue(state["ended"])

    def test_targeted_refusal_completes_observed_pass_without_an_assignment(self):
        github = PollGitHub(issue(labels=("ready", "needs-human")))
        loop = self.loop(github)
        self.assertEqual(loop.launch(number=1), 1)
        state = self.memory.snapshots[-1]
        self.assertEqual(state["latest_pass"]["state"], "complete")
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [1])
        self.assertIsNone(state["assignment"])
        self.assertTrue(state["ended"])

    def test_interrupt_during_observer_close_still_clears_launch_selection(self):
        for error in (KeyboardInterrupt, _GracefulStop):
            with self.subTest(error=error):
                loop = self.loop(PollGitHub())
                with patch.object(loop, "_launch"), \
                        patch.object(self.observer, "close", side_effect=error), \
                        self.assertRaises(error):
                    loop.launch(number=1, agent_name="worker")
                self.assertIsNone(loop._launch_number)
                self.assertIsNone(loop._launch_agent)

    def test_unfinalized_reports_do_not_gain_blockers_from_later_plans(self):
        plan = Plan(issue(labels=("ready", "needs-human")), self.cfg.agents[0], None,
                    "parked", "Stop label", 1)
        self.observer.begin_pass()
        self.observer.assignment(plan)
        self.observer.record({"kind": "lease", "assignment": 1, "agent": "worker", "run": "run",
                              "runtime": "direct", "state": "running", "expires": iso(timestamp())})
        for fields, acceptance in (({}, "unaccepted"), ({"accepted": True}, "accepted"),
                                   ({"rejected": "Paused"}, "rejected")):
            with self.subTest(acceptance=acceptance):
                self.observer.record({"kind": "outcome", "assignment": 1, "agent": "worker", "run": "run",
                                      "created": iso(timestamp()), "status": "success", "summary": "Done",
                                      **fields})
                self.observer.plan(plan)
                row = self.memory.snapshots[-1]["outcomes"][0]
                self.assertEqual(row["acceptance"], acceptance)
                self.assertIsNone(row["human_blocker"])
                self.assertIsNone(row["blocker_observed_at"])
                self.assertEqual(row["blocker_reason"], "Transition is not finalized")

    def test_claim_start_process_exit_report_and_human_blocker_are_distinct(self):
        self.cfg = replace(self.cfg, agents=(replace(self.cfg.agents[0], outcomes={
            "done": {"add": ("needs-human",), "remove": ()}}),))
        github = PollGitHub(issue())
        loop = self.loop(github)
        def execute(*args, **kwargs):
            current = self.memory.snapshots[-1]["assignment"]
            self.assertEqual(current["process"], "starting")
            self.assertEqual(current["lease_state"], "running")
            kwargs["process_started"](12345)
            self.assertEqual(self.memory.snapshots[-1]["assignment"]["process"], "running")
            lease = loop.coordinator.history(1)[0]
            loop.coordinator.report(lease, "success", "Step done", outcome="done",
                                    action="Maintainer: choose A or B; recommend A.")
            self.assertEqual(self.memory.snapshots[-1]["outcomes"][0]["acceptance"], "unaccepted")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            loop.launch(once=True)
        assignments = [s["assignment"] for s in self.memory.snapshots if s["assignment"]]
        self.assertTrue(any(a["process"] == "claiming" and a["run"] for a in assignments))
        self.assertTrue(any(a["process"] == "exited" for a in assignments))
        row = self.memory.snapshots[-1]["outcomes"][0]
        self.assertEqual(row["acceptance"], "finalized")
        self.assertTrue(row["completed"])
        self.assertEqual(row["human_blocker"], ["needs-human"])
        self.assertTrue(any(s["outcomes"] and s["outcomes"][0]["transition_complete"]
                            and not s["outcomes"][0]["completed"] for s in self.memory.snapshots))
        loop.tick()
        github.change(1, labels=frozenset({"ready"}))
        # A fresh observed plan can clear the currently observed blocker.
        self.observer.plan(next(loop.iter_plans()))
        self.assertEqual(self.memory.snapshots[-1]["outcomes"][0]["human_blocker"], [])

    def test_rejected_report_remains_unfinalized_with_supervisor_result(self):
        github = PollGitHub(issue())
        loop = self.loop(github)
        def execute(*args, **kwargs):
            lease = loop.coordinator.history(1)[0]
            loop.coordinator.report(lease, "success", "Step done", outcome="done",
                                    action="Maintainer: choose A or B; recommend A.")
            github.change(1, labels=frozenset({"ready", "needs-human"}))
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            loop.launch(once=True)
        row = self.memory.snapshots[-1]["outcomes"][0]
        self.assertEqual((row["acceptance"], row["result"], row["report_result"]),
                         ("rejected", "blocked", "success"))
        self.assertFalse(row["completed"])
        self.assertIn("paused", row["rejection"])

    def test_recovery_records_this_sessions_recovered_outcome_only(self):
        github = PollGitHub(issue(), issue(3))
        other = self.loop(github, False)
        now = timestamp()
        other.coordinator.clock = lambda: now
        lease = other.coordinator.claim(other.plans()[0])
        other.coordinator.update(lease, state="running", started=True)
        other.coordinator.report(lease, "success", "Recovered step", outcome="done")
        loop = self.loop(github)
        loop.coordinator.clock = lambda: now + 61
        with patch("ub_agents.loop.supervise") as execution:
            loop.launch(once=True)
        execution.assert_not_called()
        rows = self.memory.snapshots[-1]["outcomes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run"], lease["run"])
        self.assertTrue(rows[0]["recovered"])
        self.assertEqual(rows[0]["acceptance"], "finalized")
        self.assertTrue(any(s["assignment"] and s["assignment"]["process"] == "recovery"
                            for s in self.memory.snapshots))

    def test_row_text_outcome_and_total_byte_limits_with_omission_counts(self):
        self.observer.begin_pass()
        for number in range(MAX_PLANS + 7):
            item = replace(issue(number + 1), title="T" * (MAX_TEXT + 3),
                           body="😀" * (MAX_TEXT + 100))
            self.observer.plan(Plan(item, self.cfg.agents[0], None, "parked", "reason", 1))
        # In-memory row counts and strings are bounded too.
        self.assertEqual(len(self.observer.state["latest_pass"]["rows"]), MAX_PLANS)
        state = self.memory.snapshots[-1]
        self.assertGreaterEqual(state["omitted"]["plans"], 7)
        self.assertGreater(state["shortened"]["characters"], 0)
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()), MAX_BYTES)
        for row in state["latest_pass"]["rows"]:
            self.assertEqual(len(row["title"]), MAX_TEXT)
            self.assertGreaterEqual(row["description"]["omitted_characters"], 100)
            self.assertEqual(len(row["description"]["text"]) + row["description"]["omitted_characters"],
                             MAX_TEXT + 100)
        self.observer.begin_pass()
        plan = Plan(replace(issue(), body=None), self.cfg.agents[0], None, "parked", "reason", 1)
        self.observer.plan(plan)
        self.assertFalse(self.memory.snapshots[-1]["latest_pass"]["rows"][0]["description"]["available"])
        for number in range(MAX_OUTCOMES + 3):
            self.observer.assignment(plan)
            self.observer.record({"kind": "lease", "assignment": 1, "agent": "worker", "run": str(number),
                                  "runtime": "direct", "state": "claiming", "expires": iso(timestamp())})
            self.observer.record({"kind": "outcome", "assignment": 1, "agent": "worker", "run": str(number),
                                  "created": iso(timestamp()), "status": "retry", "summary": "S" * (MAX_TEXT + 4)})
        state = self.memory.snapshots[-1]
        self.assertEqual(len(state["outcomes"]), MAX_OUTCOMES)
        self.assertEqual(state["omitted"]["outcomes"], 3)
        self.assertGreater(state["shortened"]["characters"], 0)

    def test_wait_deadlines_and_rate_limit_use_existing_clock_only(self):
        loop = self.loop(PollGitHub())
        with patch.object(loop.stop_event, "wait"):
            loop._wait(loop.stop_event, 15, "runtime pause")
            loop.wait_rate_limit(GitHubError("GET", "user", "limit", rate_limited=True,
                                            reset_at=loop.coordinator.clock() + 60))
        waits = [s["activity"] for s in self.memory.snapshots if s["activity"]["state"] == "waiting"]
        self.assertEqual([r["reason"] for r in waits], ["runtime pause", "rate-limit reset"])
        self.assertTrue(all(r["until"] for r in waits))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lines = []

    def publisher(self, command=None):
        publisher = Publisher(self.root, self.lines.append, command=command)
        def finish():
            publisher.close()
            if publisher.process:
                publisher.process.wait(timeout=5)
                publisher.diagnostics.join(timeout=5)
                self.assertFalse(publisher.diagnostics.is_alive())
        self.addCleanup(finish)
        return publisher

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Publisher did not finish the expected operation")

    def read(self, session):
        try:
            return json.loads((self.root / ".ub-agents" / "sessions" / f"{session}.json").read_text())
        except FileNotFoundError:
            return None

    def test_atomic_private_snapshots_without_consumer_and_two_launchers(self):
        observers = [Observations(config(self.root), "operator", self.root / "ub-agents.yaml", self.publisher())
                     for _ in range(2)]
        for observer in observers:
            state = self.wait_for(lambda: self.read(observer.state["session"]))
            self.assertEqual(state["version"], 1)
            path = self.root / ".ub-agents" / "sessions" / f"{state['session']}.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(observers[0].state["session"], observers[1].state["session"])
        # Saturate the mailbox; it still converges on the final coalesced state.
        for number in range(500):
            observers[0].activity("waiting", iso(timestamp() + number), "x" * MAX_TEXT)
        expected = observers[0].state["activity"]
        self.wait_for(lambda: self.read(observers[0].state["session"])["activity"] == expected)
        observers[0].close()
        self.wait_for(lambda: self.read(observers[0].state["session"])["ended"])
        self.assertFalse(self.read(observers[1].state["session"])["ended"])
        observers[1].close()
        self.wait_for(lambda: self.read(observers[1].state["session"])["ended"])
        self.assertEqual(list((self.root / ".ub-agents" / "sessions").glob("*.tmp")), [])
        self.assertEqual(self.lines, [])

    def test_worker_ignores_checkout_modules_that_shadow_standard_library(self):
        (self.root / "json.py").write_text("raise RuntimeError('checkout module imported')\n")
        with chdir(self.root):
            observer = Observations(config(self.root), "operator", None, self.publisher())
        self.wait_for(lambda: self.read(observer.state["session"]))
        observer.close()
        self.wait_for(lambda: self.read(observer.state["session"])["ended"])
        self.assertEqual(self.lines, [])

    def test_non_object_session_json_does_not_stop_publication_or_pruning(self):
        directory = self.root / ".ub-agents" / "sessions"
        directory.mkdir(parents=True)
        now = timestamp()
        values = ([], "text", None, 7, True)
        for number in range(RETAINED_SESSIONS + 10):
            path = directory / f"malformed-{number}.json"
            path.write_text(json.dumps(values[number % len(values)]))
            os.utime(path, (now - STALE_SECONDS - 1, now - STALE_SECONDS - 1))
        fresh = directory / "fresh.json"
        fresh.write_text("[]")
        state = {"session": "own-session", "host": socket.gethostname(), "pid": os.getpid(),
                 "ended": False, "version": 1}
        write_snapshot(directory, state)
        self.assertEqual(len(list(directory.glob("malformed-*.json"))), RETAINED_SESSIONS)
        self.assertTrue(fresh.exists())
        write_snapshot(directory, state | {"ended": True})
        self.assertTrue(self.read(state["session"])["ended"])

    def test_publishes_near_size_limit_and_worker_start_failure_is_nonfatal(self):
        observer = Observations(config(self.root), "operator", None, self.publisher())
        observer.begin_pass()
        for number in range(MAX_PLANS + 1):
            plan = Plan(replace(issue(number + 1), body="😀" * (MAX_TEXT + 10)),
                        config(self.root).agents[0], None, "parked", "reason", 1)
            observer.plan(plan)
        observer.complete_pass()
        state = self.wait_for(lambda: self.read(observer.state["session"]))
        self.wait_for(lambda: self.read(observer.state["session"])["latest_pass"]["state"] == "complete")
        state = self.read(observer.state["session"])
        path = self.root / ".ub-agents" / "sessions" / f"{state['session']}.json"
        self.assertLessEqual(path.stat().st_size, MAX_BYTES)
        self.assertGreater(path.stat().st_size, MAX_BYTES // 2)
        self.assertGreaterEqual(state["omitted"]["plans"], 1)
        self.assertEqual(len(state["latest_pass"]["rows"]) + state["omitted"]["plans"], MAX_PLANS + 1)
        self.assertGreater(state["shortened"]["characters"], 0)
        observer.close()
        with patch("ub_agents.observations.subprocess.Popen", side_effect=OSError("Cannot start writer")):
            failed = self.publisher()
        for _ in range(10):
            failed.submit(b"{}")
        self.assertEqual(len(self.lines), 1)
        self.assertIn("Cannot start writer", self.lines[0])

    def test_clean_exit_during_an_in_progress_write_drains_final_snapshot(self):
        body = ("    from ub_agents.observation_worker import write_snapshot\n"
                "    write_snapshot(directory,state)\n"
                "    while not (Path(root)/'finish-write').exists():\n"
                "        time.sleep(0.01)\n"
                "    time.sleep(0.1)")
        publisher = self.publisher(observation_writer_command(body))
        observer = Observations(config(self.root), "operator", None, publisher)
        self.wait_for(lambda: self.read(observer.state["session"]))
        observer.close()
        (self.root / "finish-write").touch()
        self.wait_for(lambda: self.read(observer.state["session"])["ended"])
        self.assertEqual(self.lines, [])

    def test_interrupt_during_listener_start_preserves_descriptor_ownership(self):
        publisher = Publisher.__new__(Publisher)
        start = threading.Thread.start
        def interrupt(thread):
            start(thread)
            raise KeyboardInterrupt
        with patch("ub_agents.observations.threading.Thread.start", side_effect=interrupt, autospec=True), \
                self.assertRaises(KeyboardInterrupt):
            publisher.__init__(self.root, self.lines.append)
        publisher.process.wait(timeout=5)
        publisher.diagnostics.join(timeout=5)
        self.assertFalse(publisher.diagnostics.is_alive())
        self.assertEqual(self.lines, [])

    def test_heartbeat_keeps_idle_session_fresh(self):
        # The real helper with a short heartbeat, so the test need not wait 5 seconds.
        command = [sys.executable, "-c", "import ub_agents.observation_worker as worker\n"
                   "worker.HEARTBEAT_SECONDS = 0.2\nworker.main()"]
        observer = Observations(config(self.root), "operator", None, self.publisher(command))
        observer.activity("waiting", iso(timestamp() + 60), "next poll")
        state = self.wait_for(lambda: self.read(observer.state["session"]))
        self.wait_for(lambda: self.read(observer.state["session"])["published_at"] != state["published_at"])
        self.assertFalse(stale(self.read(observer.state["session"]), timestamp(), socket.gethostname()))
        observer.close()

    def test_unwritable_session_directory_does_not_change_launch(self):
        stub_refresh(self)
        local = self.root / ".ub-agents"
        local.mkdir()
        (local / "sessions").write_text("not a writable directory")
        observer = Observations(config(self.root), "operator", None, self.publisher())
        loop = Loop(config(self.root), PollGitHub(issue()), "operator", observer=observer, output=lambda *_: None)
        with patch("ub_agents.loop.supervise", return_value=0):
            loop.launch(once=True)
        self.wait_for(lambda: self.lines)
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(loop.coordinator.history(1)[0]["state"], "released")
        for _ in range(20):
            observer.emit()
        self.assertEqual(len(self.lines), 1)

    def test_write_error_warns_once_and_hanging_writer_never_delays_execution_or_exit(self):
        stub_refresh(self)
        for body in ("    raise OSError('write failed')", "    time.sleep(100000)"):
            with self.subTest(writer=body):
                self.lines.clear()
                (self.root / "writer-entered").unlink(missing_ok=True)
                publisher = self.publisher(observation_writer_command(body))
                observer = Observations(config(self.root), "operator", None, publisher)
                self.wait_for(lambda: (self.root / "writer-entered").exists())
                loop = Loop(config(self.root), PollGitHub(issue()), "operator", observer=observer,
                            output=lambda *_: None)
                loop.config_path = self.root / "ub-agents.yaml"
                reloaded = replace(loop.config, poll_seconds=41)
                def finish(*args, **kwargs):
                    lease = loop.coordinator.history(1)[0]
                    loop.coordinator.report(lease, "success", "Accepted despite writer", outcome="done")
                    return 0
                started = time.monotonic()
                with patch("ub_agents.loop.supervise", side_effect=finish), \
                        patch("ub_agents.loop.refresh_checkout"), \
                        patch("ub_agents.loop.load_config", return_value=reloaded):
                    loop.launch(once=True)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(loop.coordinator.history(1)[0]["result"], "success")
                self.assertEqual(loop.config.poll_seconds, 41)
                self.assertEqual(observer.state["outcomes"][0]["acceptance"], "finalized")
                publisher.process.wait(timeout=5)
                publisher.diagnostics.join(timeout=5)
                self.assertLessEqual(len(self.lines), 1)
                if "raise" in body:
                    self.assertIn("write failed", self.lines[0])

    def test_hanging_writer_does_not_delay_recovery(self):
        stub_refresh(self)
        publisher = self.publisher(observation_writer_command("    time.sleep(100000)"))
        observer = Observations(config(self.root), "operator", None, publisher)
        self.wait_for(lambda: (self.root / "writer-entered").exists())
        github = PollGitHub(issue())
        loop = Loop(config(self.root), github, "operator", output=lambda *_: None)
        now = timestamp()
        loop.coordinator.clock = lambda: now
        lease = loop.coordinator.claim(loop.plans()[0])
        loop.coordinator.update(lease, state="running", started=True)
        loop.coordinator.report(lease, "success", "Recovered with stalled writer", outcome="done")
        loop.coordinator.clock = lambda: now + 61
        loop.observer = observer
        started = time.monotonic()
        with patch("ub_agents.loop.supervise") as execution:
            loop.launch(once=True)
        self.assertLess(time.monotonic() - started, 0.5)
        execution.assert_not_called()
        self.assertEqual(observer.state["outcomes"][0]["acceptance"], "finalized")
        self.assertTrue(observer.state["outcomes"][0]["recovered"])

    def test_killed_launcher_leaves_detectably_stale_session_and_helper_exits(self):
        script = ("import sys,time\nfrom pathlib import Path\n"
                  "from ub_agents.observations import Publisher,Observations\n"
                  "from tests.support import config\n"
                  "root=Path(sys.argv[1])\n"
                  "observer=Observations(config(root),'operator',None,Publisher(root))\n"
                  "(root/'session').write_text(observer.state['session'])\n"
                  "(root/'helper-pid').write_text(str(observer.publisher.process.pid))\n"
                  "time.sleep(60)\n")
        launcher = subprocess.Popen([sys.executable, "-c", script, str(self.root)])
        try:
            session = self.wait_for(lambda: (self.root / "session").read_text()
                                    if (self.root / "session").exists() else None)
            self.wait_for(lambda: self.read(session))
            launcher.kill()
            launcher.wait(timeout=5)
            self.assertTrue(stale(self.read(session), timestamp(), socket.gethostname()))
            self.assertFalse(self.read(session)["ended"])
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=5)
        helper_pid = int((self.root / "helper-pid").read_text())
        self.wait_for(lambda: group_members(helper_pid) == [])

    def test_crashed_sessions_are_pruned_to_bound_and_live_sessions_survive(self):
        directory = self.root / ".ub-agents" / "sessions"
        directory.mkdir(parents=True)
        now = timestamp()
        for number in range(RETAINED_SESSIONS + 10):
            (directory / f"old-{number}.json").write_text(json.dumps({
                "ended": False, "pid": 99999999, "host": socket.gethostname(),
                "published_at": iso(now)}))
        (directory / "live.json").write_text(json.dumps({
            "ended": False, "pid": os.getpid(), "host": socket.gethostname(),
            "published_at": iso(now)}))
        (directory / "stale.json").write_text(json.dumps({
            "ended": False, "pid": os.getpid(), "host": "another-host",
            "published_at": iso(now - STALE_SECONDS - 1)}))
        prune(directory, "own-session", now, socket.gethostname())
        self.assertTrue((directory / "live.json").exists())
        self.assertEqual(len(list(directory.glob("*.json"))), RETAINED_SESSIONS + 1)
