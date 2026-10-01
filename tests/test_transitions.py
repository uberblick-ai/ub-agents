from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import Runtime
from ub_agents.errors import AgentError, LostOwnership
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, iso, payload, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = agent(self.root, outcomes={
            'handed-off': {'add': ('needs-review',), 'remove': ('old',)},
            'maintainer': {'add': ('needs-human',), 'remove': ()}})
        self.github = FakeGitHub(issue(labels=('ready', 'needs-changes', 'old', 'unrelated')),
                                 pr(labels=('unrelated',)))
        self.now = timestamp()
        self.loop = self.new_loop()

    def new_loop(self, *agents):
        loop = Loop(config(self.root, *(agents or (self.agent,))), self.github,
                    'operator', output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        return loop

    def report(self, loop=None, handoff=None, status='success', name='handed-off'):
        loop = loop or self.loop
        lease = next(r for r in reversed(loop.coordinator.history(1)) if r['kind'] == 'lease')
        return loop.coordinator.report(lease, status, 'Checks passed', handoff,
                                       outcome=name if status == 'success' else None)

    def execute(self, callback=None, handoff=None, status='success', name='handed-off', exit_code=0):
        def supervise(*args, **kwargs):
            outcome = self.report(handoff=handoff, status=status, name=name)
            if callback:
                callback(outcome)
            return exit_code
        with patch('ub_agents.loop.supervise', side_effect=supervise):
            return self.loop.tick()

    def labels_changed(self):
        return [w for w in self.github.writes if w[0] in {'add-labels', 'remove-label'}]

    def claim_and_report(self, handoff=None):
        plan = next(p for p in self.loop.plans() if p.item.number == 1)
        lease = self.loop.coordinator.claim(plan, self.loop.config.stop_labels)
        self.loop.coordinator.update(lease, state='running', started=True)
        outcome = self.report(handoff=handoff)
        return lease, outcome

    def test_issue_to_pr_handoff_removes_all_triggers_and_preserves_other_labels(self):
        self.execute(handoff=2)
        self.assertEqual(self.github.item(1).labels, {'unrelated'})
        self.assertEqual(self.github.item(2).labels, {'unrelated', 'needs-review'})
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual(lease['result'], 'success')
        self.assertEqual(outcome['outcome'], 'handed-off')
        self.assertTrue(outcome['accepted'])
        self.assertEqual(outcome['transition']['remove'], ['needs-changes', 'old', 'ready'])
        self.assertTrue(self.loop.coordinator.history(2)[0]['accepted'])

    def test_issue_outcome_without_handoff_adds_to_assignment(self):
        self.execute()
        self.assertEqual(self.github.item(1).labels, {'unrelated', 'needs-review'})
        self.assertEqual(self.github.item(2).labels, {'unrelated'})

    def test_reviewer_pr_outcome_routes_to_next_role(self):
        self.agent = replace(self.agent, triggers=('needs-review',),
                             outcomes={'approved': {'add': ('ready-to-merge',), 'remove': ()}})
        self.github.items = {1: pr(1, labels=('needs-review', 'unrelated'))}
        self.loop = self.new_loop()
        self.execute(name='approved')
        self.assertEqual(self.github.item(1).labels, {'ready-to-merge', 'unrelated'})
        self.assertTrue(self.loop.coordinator.history(1)[1]['accepted'])

    def test_pr_outcome_targets_assignment_and_can_add_human_gate(self):
        self.github.items = {1: pr(1, labels=('ready', 'unrelated'))}
        self.execute(name='maintainer')
        self.assertEqual(self.github.item(1).labels, {'needs-human', 'unrelated'})
        self.assertTrue(self.loop.coordinator.history(1)[1]['accepted'])

    def test_paused_assignment_or_handoff_never_applies_later(self):
        for number in (1, 2):
            with self.subTest(number=number):
                self.setUp()
                def pause(outcome):
                    self.github.change(number, labels=self.github.item(number).labels | {'needs-human'})
                self.execute(pause, handoff=2)
                lease, outcome = self.loop.coordinator.history(1)
                self.assertEqual(lease['result'], 'blocked')
                self.assertIn('paused', outcome['rejected'])
                self.assertFalse(outcome['accepted'])
                self.assertFalse(outcome['transition']['started'])
                self.assertEqual(self.labels_changed(), [])
                self.github.change(number, labels=self.github.item(number).labels - {'needs-human'})
                self.now += 61
                with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
                    self.assertFalse(self.new_loop().tick())
                self.assertEqual(self.labels_changed(), [])

    def test_paused_recovery_rejection_survives_crash_before_blocked_release(self):
        self.claim_and_report(handoff=2)
        self.github.change(2, labels=frozenset({'needs-human'}))
        self.now += 61
        with patch.object(self.loop.coordinator, 'release', side_effect=AgentError('Cannot release')):
            with self.assertRaises(AgentError):
                self.loop.tick()
        self.assertIn('paused', self.loop.coordinator.history(1)[1]['rejected'])
        self.github.change(2, labels=frozenset())
        self.now += 61
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            self.new_loop().tick()
        self.assertFalse(self.loop.coordinator.history(1)[1]['accepted'])
        self.assertEqual(self.labels_changed(), [])

    def test_human_retry_runs_new_role_instead_of_applying_paused_outcome(self):
        self.execute(lambda _: self.github.change(2, labels=frozenset({'needs-human'})), handoff=2)
        self.github.change(2, labels=frozenset())
        reset = {'kind': 'reset', 'run': 'human-reset', 'agent': self.agent.name,
                 'actor': 'operator', 'runtime': 'operator', 'assignment': 1,
                 'created': iso(self.now), 'summary': 'Decision made'}
        self.github.create_comment(1, body(reset))
        self.execute(handoff=2)
        outcomes = [r for r in self.loop.coordinator.history(1) if r['kind'] == 'outcome']
        self.assertEqual([r['accepted'] for r in outcomes], [False, True])
        self.assertNotEqual(outcomes[0]['run'], outcomes[1]['run'])

    def test_trigger_gone_blocks_before_first_change(self):
        self.execute(lambda _: self.github.change(1, labels=frozenset({'old', 'unrelated'})), handoff=2)
        lease, outcome = self.loop.coordinator.history(1)
        self.assertEqual(lease['result'], 'blocked')
        self.assertIn('trigger disappeared', outcome['rejected'])
        self.assertEqual(self.labels_changed(), [])

    def test_reported_outcome_recovers_with_changed_configuration_without_execution(self):
        self.claim_and_report(handoff=2)
        self.now += 61
        changed = replace(self.agent, triggers=('different',), outcomes={'new': {'add': ('wrong',), 'remove': ()}})
        restarted = self.new_loop(changed)
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            self.assertTrue(restarted.tick())
        history = restarted.coordinator.history(1)
        self.assertTrue(history[1]['accepted'])
        self.assertEqual(self.github.item(2).labels, {'unrelated', 'needs-review'})
        self.assertEqual(len(attempts(history, self.agent.name, self.now)), 1)

    def test_partial_transition_recovers_even_if_stop_added_after_start(self):
        remove = self.github.remove_label
        def fail_after_remove(number, label):
            remove(number, label)
            self.github.change(1, labels=self.github.item(1).labels | {'needs-human'})
            raise AgentError('Connection dropped after mutation')
        with patch.object(self.github, 'remove_label', side_effect=fail_after_remove):
            with self.assertRaises(LostOwnership):
                self.execute(handoff=2)
        outcome = self.loop.coordinator.history(1)[1]
        self.assertTrue(outcome['transition']['started'])
        self.assertFalse(outcome['accepted'])
        self.now += 61
        restarted = self.new_loop()
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            restarted.tick()
        self.assertEqual(self.github.item(1).labels, {'unrelated', 'needs-human'})
        self.assertEqual(self.github.item(2).labels, {'unrelated', 'needs-review'})
        self.assertTrue(restarted.coordinator.history(1)[1]['accepted'])
        self.assertEqual(len(attempts(restarted.coordinator.history(1), self.agent.name, self.now)), 1)

    def test_interrupt_during_transition_keeps_it_pending_for_recovery(self):
        remove = self.github.remove_label
        def interrupt(number, label):
            remove(number, label)
            raise KeyboardInterrupt
        with patch.object(self.github, 'remove_label', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute(handoff=2)
        self.assertEqual(self.loop.coordinator.history(1)[0]['state'], 'running')
        self.now += 61
        with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            self.new_loop().tick()
        self.assertTrue(self.loop.coordinator.history(1)[1]['accepted'])

    def test_overlapping_add_and_remove_replay_preserves_completed_final_label(self):
        self.agent = replace(self.agent, outcomes={'done': {'add': ('old',), 'remove': ('old',)}})
        self.loop = self.new_loop()
        with patch.object(self.loop.coordinator, 'accept', side_effect=AgentError('Cannot accept')):
            with self.assertRaises(LostOwnership):
                self.execute(name='done')
        previous_writes = self.labels_changed()
        self.now += 61
        self.new_loop().tick()
        self.assertEqual(self.labels_changed(), previous_writes)
        self.assertEqual(self.github.item(1).labels, {'old', 'unrelated'})

    def test_fully_applied_transition_before_acceptance_is_idempotent(self):
        with patch.object(self.loop.coordinator, 'accept', side_effect=AgentError('Cannot accept')):
            with self.assertRaises(LostOwnership):
                self.execute(handoff=2)
        previous_writes = self.labels_changed()
        self.now += 61
        self.new_loop().tick()
        self.assertEqual(self.labels_changed(), previous_writes)
        self.assertTrue(self.loop.coordinator.history(1)[1]['accepted'])

    def test_incomplete_handoff_reserves_both_items_and_rechecks_stale_claim(self):
        reviewer = agent(self.root, name='reviewer', triggers=('needs-review',), kind='pr')
        self.loop = self.new_loop(self.agent, reviewer)
        self.github.change(2, labels=frozenset({'needs-review'}))
        stale_plan = next(p for p in self.loop.plans() if p.item.number == 2)
        self.claim_and_report(handoff=2)
        self.now += 61
        plans = self.loop.plans()
        self.assertEqual(next(p for p in plans if p.item.number == 1).state, 'recover')
        self.assertEqual(next(p for p in plans if p.item.number == 2).state, 'owned')
        self.assertIsNone(self.loop.coordinator.claim(stale_plan))
        other = agent(self.root, name='other', triggers=('ready',))
        self.assertEqual(self.loop.coordinator.plan(self.github.item(1), other, ()).state, 'owned')

    def test_blocked_partial_recovery_keeps_handoff_reserved_even_after_reset(self):
        reviewer = agent(self.root, name='reviewer', triggers=('needs-review',), kind='pr')
        self.loop = self.new_loop(self.agent, reviewer)
        add = self.github.add_labels
        def fail_after_add(number, labels):
            add(number, labels)
            raise AgentError('Connection dropped after adding next trigger')
        with patch.object(self.github, 'add_labels', side_effect=fail_after_add):
            with self.assertRaises(LostOwnership):
                self.execute(handoff=2)
        self.github.change(2, head='b' * 40)
        self.now += 61
        self.loop.tick()
        outcome = self.loop.coordinator.history(1)[1]
        self.assertFalse(outcome.get('transition_complete', False))
        self.assertFalse(outcome['accepted'])
        plan = next(p for p in self.loop.plans() if p.item.number == 2)
        self.assertEqual(plan.state, 'owned')
        reset = {'kind': 'reset', 'run': 'human-reset', 'agent': self.agent.name,
                 'actor': 'operator', 'runtime': 'operator', 'assignment': 1,
                 'created': iso(self.now), 'summary': 'Inspect partial transition'}
        self.github.create_comment(1, body(reset))
        plan = next(p for p in self.loop.plans() if p.item.number == 2)
        self.assertEqual(plan.state, 'owned')

    def test_invalid_success_records_block_without_labels(self):
        for change in ({'outcome': 'undeclared'}, {'outcome': None, 'transition': None},
                       {'transition': {'add': ['wrong'], 'remove': [], 'triggers': ['ready'],
                                       'stop_labels': [], 'started': False}}):
            with self.subTest(change=change):
                self.setUp()
                def forge(outcome):
                    record = payload(outcome) | change
                    record = {k: v for k, v in record.items() if v is not None}
                    self.github.update_comment(outcome['id'], body(record))
                self.execute(forge)
                lease, outcome = self.loop.coordinator.history(1)
                self.assertEqual(lease['result'], 'blocked')
                self.assertFalse(outcome['accepted'])
                self.assertEqual(self.labels_changed(), [])

    def test_stale_candidate_or_unlinked_handoff_changes_no_labels(self):
        for changes in ({'head': 'b' * 40}, {'body': 'No issue link'}):
            with self.subTest(changes=changes):
                self.setUp()
                self.execute(lambda _: self.github.change(2, **changes), handoff=2)
                self.assertEqual(self.loop.coordinator.history(1)[0]['result'], 'blocked')
                self.assertEqual(self.labels_changed(), [])

    def test_retry_blocked_nonzero_timeout_and_interrupt_change_no_labels(self):
        for verdict in ('retry', 'blocked', 'nonzero', 'timeout', 'interrupt'):
            with self.subTest(verdict=verdict):
                self.setUp()
                if verdict in {'retry', 'blocked'}:
                    self.execute(status=verdict)
                elif verdict == 'nonzero':
                    self.execute(exit_code=1)
                else:
                    def fail(_):
                        if verdict == 'timeout':
                            raise AgentError('Execution timed out')
                        raise KeyboardInterrupt
                    if verdict == 'interrupt':
                        with self.assertRaises(KeyboardInterrupt):
                            self.execute(fail)
                    else:
                        self.execute(fail)
                self.assertEqual(self.labels_changed(), [])
                self.assertFalse(self.loop.coordinator.history(1)[1]['accepted'])

    def test_report_cli_rejections_and_named_outcome_use_running_lease_snapshot(self):
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan, self.loop.config.stop_labels)
        env = {'UB_AGENT_REPOSITORY': 'org/project', 'UB_AGENT_ASSIGNMENT': '1',
               'UB_AGENT_RUN': lease['run'], 'UB_AGENT_LEASE_ID': str(lease['id'])}
        with patch.dict(os.environ, env), patch('ub_agents.cli.GitHub', return_value=self.github), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['report', '--outcome', 'unknown', '--summary', 'done']), 1)
            self.assertEqual(main(['report', '--status', 'success', '--summary', 'done']), 1)
            self.assertEqual(len(self.loop.coordinator.history(1)), 1)
            self.assertEqual(main(['report', '--outcome', 'handed-off', '--summary', 'done']), 0)
        self.assertEqual(self.labels_changed(), [])
        self.assertEqual(self.loop.coordinator.history(1)[1]['outcome'], 'handed-off')

    def test_legacy_agent_rejects_named_outcome(self):
        self.loop = self.new_loop(replace(self.agent, outcomes=None))
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan)
        with self.assertRaisesRegex(AgentError, 'not declared'):
            self.loop.coordinator.report(lease, 'success', 'done', outcome='handed-off')

    def test_prompt_lists_outcomes_and_forbids_workflow_label_changes(self):
        instructions = self.root / 'instructions.md'
        instructions.write_text('Project acceptance rules')
        runtime = Runtime('recording', 'model', 'high', 'provider')
        configured = replace(self.agent, instructions=instructions, runtimes=(runtime,), command=())
        self.loop = self.new_loop(configured)
        def execute(*args, **kwargs):
            prompt = args[-1]
            self.assertIn('Declared outcomes:', prompt)
            self.assertIn('handed-off', prompt)
            self.assertIn('Do not change workflow labels', prompt)
            self.assertIn('needs-human', prompt)
            self.assertNotIn('Remove the triggering labels', prompt)
            self.report()
            return 0
        with patch('ub_agents.coordination.shutil.which', return_value='/bin/true'), \
                patch('ub_agents.loop.supervise', side_effect=execute):
            self.loop.tick()

    def test_github_label_api_adds_only_named_labels_and_handles_empty_delete(self):
        replies = [subprocess.CompletedProcess([], 0, '[]', ''),
                   subprocess.CompletedProcess([], 0, '[]', ''),
                   subprocess.CompletedProcess([], 0, '', '')]
        with patch('ub_agents.github.subprocess.run', side_effect=replies) as run:
            github = GitHub('org/project')
            github.add_labels(2, ['needs-review'])
            github.remove_label(1, 'workflow/ready')
            github.remove_label(1, 'empty-response')
        self.assertIn('POST', run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[0].kwargs['input'], '{"labels": ["needs-review"]}')
        self.assertIn('repos/org/project/issues/1/labels/workflow%2Fready', run.call_args_list[1].args[0])
