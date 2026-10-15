from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import filter_input
from ub_agents.errors import AgentError, LostOwnership, RecordError, ValidationError
from ub_agents.github import Dependency, GitHub, SubIssue
from ub_agents.loop import Loop
from ub_agents.read_input import read_item, read_policy
from ub_agents.records import body, iso, payload, records, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class SubIssueTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root, kind='issue', triggers=('needs-preparation',), outcomes={
            'split': {'add': (), 'remove': (), 'sub-issues': {'add': ('needs-preparation',)}},
            'done': {'add': (), 'remove': ()}})
        self.now = timestamp()
        self.reset()

    def reset(self, approvals='on'):
        self.github = FakeGitHub(issue(labels=('needs-preparation', 'unrelated')))
        self.cfg = replace(config(self.root, self.worker), approvals=approvals)
        self.loop = self.new_loop()

    def new_loop(self, actor='operator'):
        loop = Loop(self.cfg, self.github, actor, output=lambda *_: None)
        loop.coordinator.clock = lambda: self.now
        return loop

    def child(self, number=2, **changes):
        item = replace(issue(number, labels=('unrelated',), created_at=iso(self.now)),
                       **({'author': 'operator'} | changes))
        self.github.items[number] = item
        self.github.timelines[number] = []
        self.github.children.setdefault(1, []).append(number)

    def claim_and_report(self, before_report=None):
        plan = next(p for p in self.loop.plans() if p.item.number == 1)
        lease = self.loop.coordinator.claim(plan, self.cfg.stop_labels)
        self.loop.coordinator.update(lease, state='running', started=True)
        self.now += 2
        self.child()
        if before_report:
            before_report(lease)
        outcome = self.loop.coordinator.report(lease, 'success', 'Split', outcome='split')
        return lease, outcome

    def label(self, number, labels):
        FakeGitHub.add_labels(self.github, number, labels)
        self.github.timelines.setdefault(number, []).extend(
            {'event': 'labeled', 'actor': {'login': self.github.login}, 'label': {'name': label},
             'created_at': iso(int(self.now))} for label in labels)

    def split(self, label_writer=None):
        def execute(*args, **kwargs):
            self.now += 2
            for number in (2, 3, 4):
                self.child(number)
            lease = self.loop.coordinator.history(1)[0]
            self.loop.coordinator.report(lease, 'success', 'Split', outcome='split')
            return 0
        with patch.object(self.github, 'add_labels', side_effect=label_writer or self.label), \
                patch('ub_agents.loop.supervise', side_effect=execute):
            self.assertTrue(self.loop.tick())

    def approval(self, number=2, **kwargs):
        return filter_input(self.github, self.github.item(number), 'on', ('needs-preparation',),
                            sub_issue_labels=self.cfg.sub_issue_labels, **kwargs)

    def test_split_starts_and_loop_claims_every_child_with_approvals_on_and_off(self):
        for approvals in ('on', 'off'):
            with self.subTest(approvals=approvals):
                self.reset(approvals)
                self.split()
                self.assertEqual(self.github.item(1).labels, {'unrelated'})
                outcome = self.loop.coordinator.history(1)[1]
                self.assertEqual(outcome['sub_issues'], [2, 3, 4])
                self.assertTrue(outcome['accepted'])
                for number in (2, 3, 4):
                    self.assertEqual(self.github.item(number).labels, {'unrelated', 'needs-preparation'})
                    def execute(*args, **kwargs):
                        lease = self.loop.coordinator.history(number)[0]
                        self.loop.coordinator.report(lease, 'success', 'Done', outcome='done')
                        return 0
                    with patch('ub_agents.loop.supervise', side_effect=execute):
                        self.assertTrue(self.loop.tick())
                    self.assertEqual(self.loop.coordinator.history(number)[0]['result'], 'success')

    def test_only_qualifying_native_sub_issues_are_resolved_and_others_named(self):
        def children(lease):
            self.child(3, author='outsider')
            self.child(4, created_at=lease['created'])
            self.child(5, state='closed')
            self.github.children[1].append(SubIssue('org/elsewhere', 6, 'open', iso(self.now), 'operator'))
            self.github.items[7] = replace(issue(7, labels=(), created_at=iso(self.now)), author='operator')
            self.child(8, author=None)
        lease, outcome = self.claim_and_report(children)
        self.assertEqual(outcome['sub_issues'], [2])
        for reference in ('#3', '#4', '#5', 'org/elsewhere#6', '#8'):
            self.assertIn(reference, outcome['summary'])
        with patch.object(self.github, 'add_labels', side_effect=self.label):
            self.loop.apply_transition(lease, outcome)
        self.assertEqual([w[1] for w in self.github.writes if w[0] == 'add-labels'], [2])

    def test_lost_ownership_stops_before_labeling_another_child(self):
        lease, outcome = self.claim_and_report(lambda _: self.child(3))
        def lose_lease(number, labels):
            self.label(number, labels)
            self.github.update_comment(lease['id'], body(payload(lease) | {'state': 'withdrawn'}))
        with patch.object(self.github, 'add_labels', side_effect=lose_lease), self.assertRaises(LostOwnership):
            self.loop.apply_transition(lease, outcome)
        self.assertIn('needs-preparation', self.github.item(2).labels)
        self.assertNotIn('needs-preparation', self.github.item(3).labels)

    def test_prompt_shows_sub_issue_labels_and_leaves_application_to_launcher(self):
        plan = self.loop.plans()[0]
        lease = self.loop.coordinator.claim(plan, self.cfg.stop_labels)
        prompt = self.loop.prompt_for(plan, lease, {'earlier_branches': [], 'report_command': '/launcher/report'}, '')
        self.assertIn('"sub-issues": {"add": ["needs-preparation"]}', prompt)
        self.assertIn('File issues without trigger labels', prompt)
        self.assertIn('launcher start qualifying sub-issues', prompt)

    def test_either_agent_cannot_report_sub_issue_outcome_on_a_pr(self):
        self.cfg = replace(self.cfg, agents=(replace(self.worker, kind='either'),))
        self.github.items[2] = pr(labels=('needs-preparation',))
        self.loop = self.new_loop()
        plan = next(p for p in self.loop.plans() if p.item.number == 2)
        lease = self.loop.coordinator.claim(plan, self.cfg.stop_labels)
        with self.assertRaisesRegex(AgentError, 'requires an issue assignment'):
            self.loop.coordinator.report(lease, 'success', 'Split', outcome='split')

    def test_malformed_or_missing_resolved_targets_cannot_apply_labels(self):
        lease, outcome = self.claim_and_report()
        for targets in ('2', [True], [2, 2], [1]):
            with self.subTest(targets=targets), self.assertRaises(RecordError):
                records([{'id': 99, 'user': {'login': 'operator'},
                          'body': body(payload(outcome) | {'sub_issues': targets})}])
        unresolved = outcome.copy()
        del unresolved['sub_issues']
        with self.assertRaisesRegex(ValidationError, 'not resolved'):
            self.loop.apply_transition(lease, unresolved)
        self.assertFalse(any(w[0] in {'add-labels', 'remove-label'} for w in self.github.writes))

    def test_record_before_transition_recovers_same_targets_after_native_links_change(self):
        self.claim_and_report()
        self.github.children[1].clear()
        self.child(3)
        self.now += 61
        # Recovery must use the old declaration and targets, even after reload.
        self.cfg = replace(self.cfg, agents=(replace(self.worker, outcomes={'done': {'add': (), 'remove': ()}}),))
        with patch.object(self.github, 'sub_issues', side_effect=AssertionError('must not resolve again')), \
                patch.object(self.github, 'add_labels', side_effect=self.label), \
                patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            self.assertTrue(self.new_loop().tick())
        self.assertIn('needs-preparation', self.github.item(2).labels)
        self.assertNotIn('needs-preparation', self.github.item(3).labels)
        self.assertTrue(self.loop.coordinator.history(1)[1]['accepted'])

    def test_partial_label_transition_recovers_with_another_launcher(self):
        def drop(number, labels):
            self.label(number, labels)
            raise AgentError('Connection dropped after label write')
        with self.assertRaisesRegex(AgentError, 'Connection dropped'):
            self.split(label_writer=drop)
        self.now += 61
        self.github.login = 'second'
        self.github.roles['second'] = 'write'
        with patch.object(self.github, 'add_labels', side_effect=self.label), \
                patch('ub_agents.loop.supervise', side_effect=AssertionError('must not rerun')):
            self.assertTrue(self.new_loop(actor='second').tick())
        for number in (2, 3, 4):
            self.assertTrue(self.approval(number).allowed)
        self.assertEqual(self.github.timelines[2][0]['actor']['login'], 'operator')
        self.assertEqual(self.github.timelines[3][0]['actor']['login'], 'second')

    def test_pending_parent_does_not_park_child_before_acceptance_or_release(self):
        lease, outcome = self.claim_and_report()
        with patch.object(self.github, 'add_labels', side_effect=self.label):
            self.loop.apply_transition(lease, outcome)
        for accepted in (False, True):
            if accepted:
                self.loop.coordinator.accept(lease, outcome)
            check = self.approval()
            self.assertFalse(check.allowed)
            self.assertIn('pending', check.reason)
            self.assertIsNone(check.gate)
            self.assertFalse(self.new_loop().tick())
            self.assertNotIn('needs-human', self.github.item(2).labels)
        self.loop.coordinator.release(lease, 'success', 'Done', attempt_effect='reset')
        self.assertTrue(self.approval().allowed)

    def test_launcher_label_without_matching_accepted_parent_is_not_a_start(self):
        self.now += 2
        self.child()
        self.label(2, ['needs-preparation'])
        self.github.change(1, labels=frozenset())
        self.assertIn('No maintainer', self.approval().reason)
        self.assertFalse(self.loop.tick())
        self.assertIn('needs-human', self.github.item(2).labels)

    def test_inherited_start_keeps_outside_edit_and_comment_rules(self):
        self.split()
        self.assertTrue(self.approval().allowed)
        comment = self.github.create_comment(2, 'Outside feedback', login='outside')
        for row in self.github.store[2]:
            if row['id'] == comment['id']:
                row.update(created_at=iso(self.now + 1), updated_at=iso(self.now + 1))
        check = self.approval()
        self.assertTrue(check.allowed)
        self.assertEqual(check.snapshot['comments'], [])
        self.assertEqual(check.snapshot['withheld_counts']['comments'], 1)
        self.github.change(2, body='Outside edit')
        self.github.content_histories[2] = {'lastEditedAt': iso(self.now + 2), 'edits': [
            {'editedAt': iso(self.now + 2), 'editor': {'login': 'outside'}}]}
        self.assertIn('Outside body edit', self.approval().reason)
        for number in (3, 4):
            self.github.change(number, labels=frozenset())
        self.assertFalse(self.loop.tick())
        self.assertIn('needs-human', self.github.item(2).labels)

    def test_untrusted_parent_record_label_actor_or_unlisted_launcher_grants_no_start(self):
        self.split()
        for change in ('record-author', 'label-actor', 'launchers', 'listed-bot', 'rejected', 'targets', 'labels'):
            with self.subTest(change=change):
                self.reset()
                self.split()
                kwargs = {}
                if change == 'record-author':
                    self.github.store[1][1]['user'] = {'login': 'outside'}
                elif change == 'label-actor':
                    self.github.timelines[2][0]['actor'] = {'login': 'outside'}
                elif change == 'launchers':
                    kwargs['launchers'] = ('different',)
                elif change == 'listed-bot':
                    kwargs['trusted_bots'] = ('operator',)
                    self.github.timelines[2][0]['actor']['type'] = 'Bot'
                else:
                    outcome = self.loop.coordinator.history(1)[1]
                    changes = {'rejected': {'rejected': 'Rejected'}, 'targets': {'sub_issues': [3]},
                               'labels': {'transition': outcome['transition'] | {'sub-issues': {'add': ['other']}}}}
                    self.github.update_comment(outcome['id'], body(payload(outcome) | changes[change]))
                self.assertFalse(self.approval(**kwargs).allowed)

    def test_later_launcher_reapplication_cannot_clear_outside_edits(self):
        self.split()
        self.now += 5
        self.github.change(2, body='Outside edit')
        self.github.content_histories[2] = {'lastEditedAt': iso(self.now), 'edits': [
            {'editedAt': iso(self.now), 'editor': {'login': 'outside'}}]}
        self.now += 5
        self.label(2, ['needs-preparation'])
        self.assertFalse(self.approval().allowed)
        self.github.timelines[2][-1]['actor'] = {'login': 'maintainer'}
        self.assertTrue(self.approval().allowed)

    def test_read_clearance_uses_the_pinned_launcher_allowlist(self):
        self.split()
        self.github.change(2, author='outside')
        self.github.content_histories[2] = {'author': {'login': 'outside'}}
        comment = self.github.create_comment(2, 'Outside input before start', login='outside')
        self.github.store[2][0].update(created_at=iso(self.now - 1), updated_at=iso(self.now - 1))
        approved = read_policy(replace(self.cfg, launchers=('operator',)))
        unlisted = read_policy(replace(self.cfg, launchers=('other',)))
        self.assertEqual(approved['launchers'], ['operator'])
        cleared = read_item(self.github, 2, approved)
        self.assertEqual(cleared['body'], self.github.item(2).body)
        self.assertEqual([c['id'] for c in cleared['comments']], [comment['id']])
        withheld = read_item(self.github, 2, unlisted)
        self.assertTrue(withheld['body']['withheld'])
        self.assertEqual(withheld['comments'], [])

    def test_read_clearance_uses_only_pinned_sub_issue_labels(self):
        self.split()
        self.github.change(2, author='outside')
        self.github.content_histories[2] = {'author': {'login': 'outside'}}
        pinned = read_policy(self.cfg)
        self.assertEqual(pinned['sub-issue-labels'], ['needs-preparation'])
        changed = replace(self.cfg, agents=(replace(self.worker, outcomes={'done': {'add': (), 'remove': ()}}),))
        self.assertNotIn('sub-issue-labels', read_policy(changed))
        self.assertEqual(read_item(self.github, 2, pinned)['body'], self.github.item(2).body)
        with patch.object(self.github, 'parent_issue', wraps=self.github.parent_issue) as parent:
            self.assertTrue(read_item(self.github, 2, read_policy(changed))['body']['withheld'])
        parent.assert_not_called()


class SubIssueGitHubTests(unittest.TestCase):
    def raw(self, number=2):
        return {'url': f'https://api.github.com/repos/org/project/issues/{number}', 'number': number,
                'state': 'open', 'created_at': '2026-01-01T00:00:00Z', 'user': {'login': 'operator'}}

    def test_sub_issues_paginates_and_preserves_repository_author_time_and_state(self):
        github = GitHub('org/project')
        raw = self.raw()
        with patch.object(github, 'request', return_value=[raw]) as request:
            self.assertEqual(github.sub_issues(1), [SubIssue('org/project', 2, 'open', raw['created_at'], 'operator')])
        request.assert_called_once_with('repos/org/project/issues/1/sub_issues?per_page=100', paginate=True)
        for changes in ({'number': True}, {'url': 'wrong'}, {'state': 'unknown'}, {'created_at': 'wrong'},
                        {'pull_request': {}}, {'user': {'login': []}}):
            with self.subTest(changes=changes), patch.object(github, 'request', return_value=[raw | changes]):
                with self.assertRaisesRegex(AgentError, 'Unreadable GitHub sub-issues'):
                    github.sub_issues(1)

    def test_parent_read_distinguishes_no_parent_and_unreadable_parent(self):
        github = GitHub('org/project')
        for parent, expected in ((None, None), ({'number': 1, 'state': 'CLOSED',
                                               'repository': {'nameWithOwner': 'org/project'}},
                                              Dependency('org/project', 1, 'closed'))):
            with patch.object(github, 'graphql', return_value={'repository': {'issue': {'parent': parent}}}):
                self.assertEqual(github.parent_issue(2), expected)
        with patch.object(github, 'graphql', return_value={'repository': {'issue': None}}):
            with self.assertRaisesRegex(AgentError, 'Unreadable GitHub parent issue'):
                github.parent_issue(2)
