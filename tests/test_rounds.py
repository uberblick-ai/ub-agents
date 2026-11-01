from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.coordination import Coordinator
from ub_agents.errors import RecordError
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import body, outcome_rounds, payload, records, timestamp
from tests.support import settlement, FakeGitHub, agent, config, pr, stub_refresh


class RoundTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reviewer = agent(self.root, name='reviewer', kind='pr', triggers=('needs-review',), outcomes={
            'approved': {'add': ('ready-to-merge',), 'remove': ()},
            'changes-requested': {'add': ('needs-changes',), 'remove': ('old',), 'max-rounds': 3}})
        self.github = FakeGitHub(pr(1, labels=('needs-review', 'old', 'unrelated')))
        self.now = timestamp()
        self.loop = self.new_loop()
        self.contexts = []

    def new_loop(self, role=None, *, actor='operator', stops=('needs-human',)):
        loop = Loop(replace(config(self.root, role or self.reviewer), stop_labels=stops),
                    self.github, actor, output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        return loop

    def resume(self, number=1, trigger='needs-review'):
        self.github.change(number, labels=frozenset({'unrelated', 'old', trigger}))

    def run_review(self, name='changes-requested', *, number=1, summary='P1: acceptance criterion unmet.'):
        def supervise(command, cwd, env, *args, **kwargs):
            context = json.loads(Path(env['UB_AGENTS_CONTEXT']).read_text())
            self.contexts.append(context)
            self.assertEqual(context['outcome_rounds'], lease['outcome_rounds'])
            self.loop.coordinator.report(lease, 'success', summary, outcome=name)
            return 0

        def claimed(record):
            nonlocal lease
            lease = record

        lease = None
        self.loop.coordinator.on_claim = claimed
        with patch('ub_agents.loop.supervise', side_effect=supervise):
            self.assertTrue(self.loop.tick_item(number))
        report = self.loop.coordinator.outcome(lease)
        self.assertTrue(report['accepted'], report)
        return lease, report

    def count(self, number=1, actor='operator'):
        coordinator = Coordinator(self.github, actor)
        return outcome_rounds(coordinator.history(number), number, 'reviewer', 'changes-requested')

    def use_rounds(self, count=3):
        for used in range(count):
            self.resume()
            self.run_review()
            self.assertEqual(self.count(), used + 1)

    def test_first_three_request_changes_fourth_parks_and_carries_findings_and_resume_paths(self):
        for used in range(4):
            self.resume()
            # Another trusted launcher sees the same history after every restart.
            self.github.roles['second'] = 'write'
            self.github.login = 'operator' if used % 2 == 0 else 'second'
            self.loop = self.new_loop(actor='operator' if used % 2 == 0 else 'second')
            lease, report = self.run_review(summary='P1: lost update.\n\nP1: failing check.')
            self.assertEqual(lease['outcome_rounds'], {'changes-requested': {'used': used, 'limit': 3}})
            self.assertEqual(self.github.item(1).labels,
                             {'unrelated', 'needs-changes' if used < 3 else 'needs-human'})
            self.assertEqual(self.count(), used + 1)
        notices = [c['body'] for c in self.github.store[1] if c['body'].startswith(ACTION_MARKER)]
        notice, = notices
        self.assertIn('**Action needed**', notice)
        for text in ('max-rounds: 3', 'P1: lost update.', 'P1: failing check.',
                     'one more run', 'keep the round count', 'fresh set of rounds',
                     'ub-agents retry 1 --agent reviewer --reason', '`needs-review`', '`needs-human`'):
            self.assertIn(text, notice)
        self.assertNotIn('Retry does not restore labels', notice)
        self.assertNotIn('Then remove', notice)
        self.assertIn('Maintainer: reset the round count for a fresh set of rounds\n\n'
                      '   ```sh\n   ub-agents retry 1 --agent reviewer --reason "Human resolved the blocker"\n   ```', notice)
        self.assertNotIn('needs-changes', report['transition']['add'])
        self.assertEqual(report['summary'], 'P1: lost update.\n\nP1: failing check.')

    def test_approval_at_any_round_keeps_its_transition_and_does_not_reset_rounds(self):
        for used in range(5):
            self.resume()
            self.run_review(name='approved', summary='Only P2 suggestions remain in the review.')
            self.assertEqual(self.github.item(1).labels, {'unrelated', 'old', 'ready-to-merge'})
            self.assertEqual(self.count(), used)
            self.resume()
            self.run_review()

    def test_manual_resume_keeps_count_but_retry_resets_only_selected_item_and_agent(self):
        self.use_rounds(4)
        self.resume()
        lease, _ = self.run_review()
        self.assertEqual(lease['outcome_rounds']['changes-requested']['used'], 4)
        self.assertEqual(self.github.item(1).labels, {'unrelated', 'needs-human'})
        self.assertEqual(self.count(), 5)
        for role in (agent(self.root, name='integrator'), self.reviewer):
            configured = config(self.root, role)
            with patch('ub_agents.cli.load_config', return_value=configured), \
                    patch('ub_agents.cli.GitHub', return_value=self.github), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['retry', '1', '--agent', role.name, '--reason', 'Findings resolved']), 0)
            self.assertEqual(self.github.item(1).labels,
                             {'unrelated', 'ready'} | ({'needs-review'} if role.name == 'reviewer' else set()))
            self.assertEqual(self.count(), 0 if role.name == 'reviewer' else 5)
        lease, _ = self.run_review()
        self.assertEqual(lease['outcome_rounds']['changes-requested']['used'], 0)
        self.assertEqual(self.github.item(1).labels, {'unrelated', 'ready', 'needs-changes'})

    def test_integrator_outcomes_including_conflicts_and_new_pr_do_not_use_reviewer_rounds(self):
        self.use_rounds(2)
        integrator = agent(self.root, name='integrator', kind='pr', triggers=('ready-to-merge',),
                           on_conflict='changes-requested', outcomes={
                               'changes-requested': {'add': ('needs-changes',), 'remove': ()}})
        for conflict in (False, True):
            self.resume(trigger='ready-to-merge')
            self.github.change(1, mergeable=not conflict)
            loop = self.new_loop(integrator)
            def supervise(*args, **kwargs):
                lease = next(r for r in reversed(loop.coordinator.history(1)) if r['kind'] == 'lease')
                loop.coordinator.report(lease, 'success', 'Repair before merge', outcome='changes-requested')
                return 0
            with patch('ub_agents.loop.supervise', side_effect=supervise) as executed:
                self.assertTrue(loop.tick_item(1))
                self.assertEqual(executed.call_count, 0 if conflict else 1)
            self.assertEqual(self.count(), 2)
        self.github.items[2] = pr(2, labels=('needs-review',), body='Closes #1')
        self.loop = self.new_loop()
        lease, _ = self.run_review(number=2)
        self.assertEqual(lease['outcome_rounds']['changes-requested']['used'], 0)
        self.assertEqual(self.count(2), 1)
        self.assertEqual(self.count(1), 2)

    def test_recovery_counts_once_and_retains_limit_transition_after_configuration_changes(self):
        for used in range(4):
            self.resume()
            self.loop = self.new_loop()
            coordinator = self.loop.coordinator
            plan = next(self.loop.item_plans(1)[1])
            lease = coordinator.claim(plan, self.loop.config.stop_labels)
            coordinator.update(lease, state='running', started=True)
            report = coordinator.report(lease, 'success', 'P1: still broken.', outcome='changes-requested')
            # Simulate a crash after labels were changed, before acceptance.
            settlement(self.loop).apply_transition(lease, report)
            self.assertEqual(self.count(), used)
            self.now += 61
            changed = replace(self.reviewer, outcomes={'new': {'add': ('wrong',), 'remove': ()}})
            recovery = self.new_loop(changed)
            with patch('ub_agents.loop.supervise', side_effect=AssertionError('must not execute')):
                self.assertTrue(recovery.tick_item(1))
                recovery.tick_item(1)
            self.assertEqual(self.count(), used + 1)
            self.assertEqual(self.github.item(1).labels,
                             {'unrelated', 'needs-human' if used == 3 else 'needs-changes'})
        notices = [c['body'] for c in self.github.store[1] if c['body'].startswith(ACTION_MARKER)]
        notice, = notices
        self.assertIn('max-rounds: 3', notice)
        self.assertIn('P1: still broken.', notice)
        self.assertIn('ub-agents retry 1 --agent reviewer --reason', notice)

    def test_pending_and_rejected_reports_do_not_count_and_transition_uses_first_stop_label(self):
        self.use_rounds()
        self.resume()
        self.loop = self.new_loop(stops=('paused', 'needs-human'))
        coordinator = self.loop.coordinator
        lease = coordinator.claim(next(self.loop.item_plans(1)[1]), self.loop.config.stop_labels)
        report = coordinator.report(lease, 'success', 'P1: regression.', outcome='changes-requested')
        self.assertEqual(report['transition']['add'], ['paused'])
        self.assertEqual(self.count(), 3)
        coordinator.update_outcome(lease, report, accepted=True, rejected='stale')
        self.assertEqual(self.count(), 3)

    def test_count_ignores_copies_other_items_and_duplicate_recovery_receipts(self):
        self.use_rounds(1)
        history = self.loop.coordinator.history(1)
        report = next(r for r in history if r['kind'] == 'outcome')
        copied = report | {'id': report['id'] + 100}
        unrelated_reset = {'kind': 'reset', 'assignment': 2, 'agent': 'reviewer', 'id': report['id'] + 101}
        self.assertEqual(outcome_rounds(history + [copied, unrelated_reset], 1, 'reviewer', 'changes-requested'), 1)

    def test_malformed_round_snapshots_are_rejected(self):
        coordinator = self.loop.coordinator
        lease = coordinator.claim(next(self.loop.item_plans(1)[1]), self.loop.config.stop_labels)
        for rounds in ([], {}, {'': {'used': 0, 'limit': 3}},
                       {'changes-requested': {'used': -1, 'limit': 3}},
                       {'changes-requested': {'used': True, 'limit': 3}},
                       {'changes-requested': {'used': 0, 'limit': 0}},
                       {'changes-requested': {'used': 0, 'limit': 3.0}}):
            with self.subTest(rounds=rounds), self.assertRaises(RecordError):
                records([{'id': lease['id'], 'user': {'login': 'operator'},
                          'body': body(payload(lease) | {'outcome_rounds': rounds})}])
