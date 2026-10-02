from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import (approval_body, approve_issue, body_sha,
                                check_pr, parse_approval)
from ub_agents.cli import main, status_rows
from ub_agents.errors import AgentError
from ub_agents.loop import Loop
from ub_agents.records import attempts, body, payload, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh
from tests.test_approvals import at


def feedback(number, login='outsider', text='Outside input', second=8, **extra):
    return dict(id=number, user={'login': login}, body=text, created_at=at(second),
                updated_at=at(second), **extra)


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.github = FakeGitHub(issue())
        self.github.roles['outsider'] = 'read'
        self.output = []
        self.loop = Loop(config(self.root, self.worker), self.github, 'operator', output=self.output.append)

    def start(self, number=1, label='ready', second=5):
        self.github.timelines.setdefault(number, []).append(
            dict(event='labeled', actor={'login': 'maintainer'}, label={'name': label}, created_at=at(second)))

    def outside_edit(self, number=1, second=10):
        self.github.change(number, body='Edited input')
        self.github.content_histories[number] = dict(lastEditedAt=at(second), edits=[
            dict(editedAt=at(second), editor={'login': 'outsider'}, diff='Edited input', deletedAt=None)])

    def assert_parked(self, reason):
        rows = status_rows(self.loop)
        self.assertEqual((rows[0]['state'], rows[0]['attempts']), ('parked', 0))
        self.assertIn(reason, rows[0]['reason'])
        with patch('ub_agents.loop.supervise') as run:
            self.assertFalse(self.loop.tick())
        run.assert_not_called()
        self.assertEqual(self.github.writes, [])
        self.assertTrue(any('parked' in line and reason in line for line in self.output))

    def execute(self, verify=None):
        def run(command, cwd, env, run_dir, *args, **kwargs):
            context = json.loads(Path(env['UB_AGENT_CONTEXT']).read_text())
            if verify:
                verify(context)
            lease = self.loop.coordinator.history(context['assignment'])[-1]
            self.loop.coordinator.report(lease, 'success', 'Completed', outcome='done')
            return 0
        with patch('ub_agents.loop.supervise', side_effect=run) as run_mock:
            self.assertTrue(self.loop.tick())
        run_mock.assert_called_once()

    def test_issue_pickup_parks_without_writes_or_attempts(self):
        self.github.timelines[1] = []
        self.assert_parked('No maintainer')
        self.start()
        self.outside_edit()
        self.assert_parked('Outside body edit')
        self.start(second=15)
        self.github.content_histories[1]['edits'] = []
        self.assert_parked('unreadable')
        self.github.content_histories.clear()
        self.execute()

    def test_post_claim_issue_edit_withdraws_and_resumes_without_retry(self):
        self.github.timelines[1] = []
        self.start()
        original = self.loop.coordinator.claim
        def claim(*args, **kwargs):
            lease = original(*args, **kwargs)
            self.outside_edit()
            return lease
        with patch.object(self.loop.coordinator, 'claim', side_effect=claim), patch('ub_agents.loop.supervise') as run:
            self.assertTrue(self.loop.tick())
        run.assert_not_called()
        history = self.loop.coordinator.history(1)
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]['state'], history[0]['started']), ('withdrawn', False))
        self.assertEqual(attempts(history, self.worker.name, timestamp()), [])
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.start(second=20)
        self.execute()

    def test_preparation_uses_all_issue_triggers_and_trusted_post_claim_snapshot(self):
        preparer = replace(self.worker, name='preparer', triggers=('needs-preparation',), kind='issue')
        self.loop.config = config(self.root, preparer, replace(self.worker, kind='issue'))
        self.github.change(1, labels=frozenset({'ready'}))
        self.github.timelines[1] = []
        self.start(label='needs-preparation')
        original = self.loop.coordinator.claim
        def claim(*args, **kwargs):
            lease = original(*args, **kwargs)
            self.github.change(1, title='Prepared title', body='Prepared body')
            self.github.content_histories[1] = dict(lastEditedAt=at(10), edits=[
                dict(editedAt=at(10), editor={'login': 'operator'}, diff='Prepared body', deletedAt=None)])
            return lease
        with patch.object(self.loop.coordinator, 'claim', side_effect=claim):
            self.execute(lambda c: self.assertEqual((c['title'], c['body']), ('Prepared title', 'Prepared body')))
        # Preparation itself is subject to the same gate.
        self.github.change(1, labels=frozenset({'needs-preparation'}))
        self.github.timelines[1] = []
        self.assertEqual(self.loop.plans()[0].state, 'parked')

    def test_issue_context_excludes_records_and_uncleared_or_edited_comments(self):
        self.github.timelines[1] = []
        self.start()
        cleared = feedback(100, second=3)
        trusted = feedback(101, 'operator', 'Trusted note')
        unapproved = feedback(102)
        edited = feedback(103, second=2)
        edited['updated_at'] = at(9)
        record = feedback(104, 'maintainer', approval_body(1, 'Requirements', 'Acceptance criteria', [cleared]), 15)
        notice = feedback(105, 'operator', '<!-- ub-agent:action-needed old-run -->\nAction needed')
        self.github.store[1] = [cleared, trusted, unapproved, edited, record, notice]
        def verify(c):
            self.assertEqual([r['id'] for r in c['comments']], [100, 101])
            prompt = self.loop.prompt_for(self.loop.plans()[0], self.loop.coordinator.history(1)[0], c, 'Read GitHub comments')
            self.assertIn('Other comments on GitHub are not assignment input', prompt)
            # Later edits do not affect this run's input or prevent completion.
            self.outside_edit(second=20)
        self.execute(verify)
        self.assertEqual(self.loop.coordinator.history(1)[-2]['result'], 'success')

    def use_pr(self, trusted=False, head_repository='org/project'):
        self.github.items = {2: pr()}
        self.github.timelines[2] = []
        original = self.github.pr_content
        if not trusted:
            patcher = patch.object(self.github, 'pr_content', side_effect=lambda n: original(n) | {
                'author': {'login': 'outsider'}, 'head_repository': head_repository})
            self.addCleanup(patcher.stop)
            patcher.start()

    def approve(self, second=15, head=None, comments=(), reviews=(), review_comments=()):
        item = self.github.item(2)
        self.github.store.setdefault(2, []).append(feedback(
            200 + len(self.github.store.get(2, [])), 'maintainer',
            approval_body(2, item.title, item.body, comments, head=head or item.head,
                          reviews=reviews, review_comments=review_comments), second))

    def test_trusted_pr_runs_without_start_and_outside_feedback_does_not_park(self):
        self.use_pr(trusted=True)
        self.github.store[2] = [feedback(100)]
        self.github.review_store[2] = [feedback(100, state='COMMENTED', commit_id='a' * 40)]
        self.github.review_comment_store[2] = [feedback(100)]
        self.execute(lambda c: self.assertEqual([c['comments'], c['reviews'], c['review_comments']], [[], [], []]))

    def test_outside_pr_needs_start_and_current_head_approval(self):
        self.use_pr()
        self.assert_parked('No maintainer')
        self.start(2, 'needs-changes')
        self.assert_parked('head is not approved')
        self.approve(head='b' * 40)
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.approve(second=20)
        self.execute()

    def test_maintainer_approving_review_allows_head_but_does_not_clear_feedback(self):
        self.use_pr()
        self.start(2, 'needs-changes')
        self.github.store[2] = [feedback(100)]
        self.github.review_store[2] = [feedback(101, 'maintainer', 'Approved', 15,
                                                state='APPROVED', commit_id='a' * 40)]
        self.execute(lambda c: self.assertEqual(c['comments'], []))

    def test_post_claim_ineligible_head_is_parked_without_attempt(self):
        self.use_pr()
        self.start(2, 'needs-changes')
        self.approve()
        original = self.loop.coordinator.claim
        def claim(*args, **kwargs):
            lease = original(*args, **kwargs)
            self.github.change(2, head='b' * 40)
            return lease
        with patch.object(self.loop.coordinator, 'claim', side_effect=claim), patch('ub_agents.loop.supervise') as run:
            self.assertTrue(self.loop.tick())
        run.assert_not_called()
        history = self.loop.coordinator.history(2)
        self.assertEqual((history[0]['state'], history[0]['started']), ('withdrawn', False))
        self.assertEqual(attempts(history, self.worker.name, timestamp()), [])
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.approve(second=25)
        self.execute()

    def test_outside_pr_edits_pushes_and_feedback_suspend(self):
        for change in ('push', 'title', 'body', 'comments', 'reviews', 'review_comments'):
            with self.subTest(change=change):
                self.setUp()
                self.use_pr()
                self.start(2, 'needs-changes')
                self.approve()
                if change == 'push':
                    self.github.change(2, head='b' * 40)
                elif change == 'title':
                    self.github.change(2, title='New title')
                    self.github.timelines[2].append(dict(event='renamed', actor={'login': 'outsider'},
                        rename={'from': 'Candidate', 'to': 'New title'}, created_at=at(20)))
                elif change == 'body':
                    self.outside_edit(2, 20)
                else:
                    row = feedback(100, second=20)
                    if change == 'reviews':
                        row |= dict(state='COMMENTED', commit_id='a' * 40)
                    store = {'comments': self.github.store, 'reviews': self.github.review_store,
                             'review_comments': self.github.review_comment_store}[change]
                    store.setdefault(2, []).append(row)
                self.assertEqual(self.loop.plans()[0].state, 'parked')
                self.approve(second=30)
                self.assertEqual(self.loop.plans()[0].state, 'ready')

    def test_pr_context_clearance_is_separate_for_each_feedback_type(self):
        self.use_pr()
        self.start(2, 'needs-changes')
        for name, store in (('comments', self.github.store), ('reviews', self.github.review_store),
                            ('review_comments', self.github.review_comment_store)):
            store[2] = [feedback(100, text='Cleared'), feedback(101, 'operator', 'Trusted'),
                        feedback(102, text='Uncleared'), feedback(103, text='Edited later')]
            if name == 'reviews':
                for row in store[2]:
                    row |= dict(state='COMMENTED', commit_id='a' * 40)
        self.approve(comments=self.github.store[2][:1], reviews=self.github.review_store[2][:1],
                     review_comments=self.github.review_comment_store[2][:1])
        def verify(c):
            for name in ('comments', 'reviews', 'review_comments'):
                self.assertEqual([row['id'] for row in c[name]], [100, 101])
        self.execute(verify)
        # Later feedback edits lose clearance even after a review lifts suspension.
        for store in (self.github.store, self.github.review_store, self.github.review_comment_store):
            store[2][0]['updated_at'] = at(20)
        self.github.review_store[2].append(feedback(110, 'maintainer', 'Approved again', 25,
                                                   state='APPROVED', commit_id='a' * 40))
        result = check_pr(self.github, 2, {'needs-changes'})
        self.assertTrue(result.allowed)
        for name in ('comments', 'reviews', 'review_comments'):
            self.assertNotIn(100, [r['id'] for r in result.snapshot[name]])

    def test_accepted_agent_revision_inherits_eligible_head_only(self):
        self.use_pr(head_repository='ORG/PROJECT')
        self.start(2, 'needs-changes')
        self.approve()
        def revise(c):
            self.github.change(2, head='b' * 40)
        self.execute(revise)
        self.github.change(2, labels=frozenset({'needs-changes'}))
        self.assertEqual(self.loop.plans()[0].state, 'ready')
        self.execute(lambda c: self.github.change(2, head='c' * 40))
        self.github.change(2, labels=frozenset({'needs-changes'}))
        self.assertEqual(self.loop.plans()[0].state, 'ready')
        self.github.change(2, head='d' * 40)
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        # A copied outcome from an ineligible assignment cannot establish ancestry.
        history = self.loop.coordinator.history(2)
        lease, outcome = history[-2:]
        for record in (lease, outcome):
            changed = payload(record) | {'assignment_sha': 'e' * 40}
            if record['kind'] == 'outcome':
                changed['candidate_sha'] = 'd' * 40
            self.github.update_comment(record['id'], body(changed))
        self.assertEqual(self.loop.plans()[0].state, 'parked')

    def test_fork_push_during_successful_run_cannot_inherit_head_approval(self):
        for repository in ('outsider/project', None):
            with self.subTest(head_repository=repository):
                self.setUp()
                self.use_pr(head_repository=repository)
                self.start(2, 'needs-changes')
                self.approve()
                self.execute(lambda c: self.github.change(2, head='b' * 40))
                lease, outcome = self.loop.coordinator.history(2)[-2:]
                self.assertEqual((lease['state'], lease['result']), ('released', 'success'))
                self.assertTrue(outcome['accepted'])
                self.assertEqual((outcome['assignment_sha'], outcome['candidate_sha']), ('a' * 40, 'b' * 40))
                self.github.change(2, labels=frozenset({'needs-changes'}))
                self.assertEqual(self.loop.plans()[0].state, 'parked')
                # Relabeling cannot approve the fork's newly observed head either.
                self.start(2, 'needs-changes', second=20)
                self.assertEqual(self.loop.plans()[0].state, 'parked')
                self.approve(second=25)
                self.execute()
                # A maintainer approving review can also approve a new fork head.
                self.github.change(2, head='c' * 40, labels=frozenset({'needs-changes'}))
                self.github.review_store[2] = [feedback(100, 'maintainer', 'Approved', 30,
                                                       state='APPROVED', commit_id='c' * 40)]
                self.execute()

    def test_approve_pr_pins_head_and_feedback_and_refuses_display_races(self):
        self.use_pr()
        self.github.login = 'maintainer'
        self.github.store[2] = [feedback(100)]
        self.github.review_store[2] = [feedback(100, state='COMMENTED', commit_id='a' * 40)]
        self.github.review_comment_store[2] = [feedback(100)]
        with patch('ub_agents.cli.load_config', return_value=self.loop.config), \
                patch('ub_agents.cli.GitHub', return_value=self.github), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['approve', '--number', '2']), 0)
        self.assertIn('a' * 40, output.getvalue())
        record = parse_approval(self.github.store[2][-1]['body'], 2, 'pr')
        self.assertEqual(record['head_sha'], 'a' * 40)
        for name in ('comments', 'reviews', 'review_comments'):
            self.assertEqual(record[name], [{'id': 100, 'body_sha256': body_sha('Outside input')}])
        self.github.change(2, head='b' * 40)
        self.start(2, 'needs-changes')
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        original = self.github.item
        reads = 0
        def item(number):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.github.change(2, head='c' * 40)
            return original(number)
        writes = self.github.writes[:]
        with patch.object(self.github, 'item', side_effect=item), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(AgentError, 'changed'):
                approve_issue(self.github, 2, 'maintainer')
        self.assertEqual(self.github.writes, writes)

    def test_post_claim_unreadable_input_withdraws_without_execution_or_attempt(self):
        original = self.loop.coordinator.claim
        def claim(*args, **kwargs):
            lease = original(*args, **kwargs)
            self.github.content_histories[1] = {'lastEditedAt': at(10), 'edits': []}
            return lease
        with patch.object(self.loop.coordinator, 'claim', side_effect=claim), patch('ub_agents.loop.supervise') as run:
            self.assertTrue(self.loop.tick())
        run.assert_not_called()
        history = self.loop.coordinator.history(1)
        self.assertEqual((history[0]['state'], history[0]['started']), ('withdrawn', False))
        self.assertEqual(attempts(history, self.worker.name, timestamp()), [])
        self.assertEqual(self.loop.plans()[0].state, 'parked')

    def test_approval_and_review_cannot_replace_start_or_clear_same_second_input(self):
        self.use_pr()
        self.approve()
        self.github.review_store[2] = [feedback(100, 'maintainer', 'Approved', 20,
                                                state='APPROVED', commit_id='a' * 40)]
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.start(2, 'ready', second=25)
        self.github.store[2].append(feedback(101, second=25))
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.github.review_store[2].append(feedback(102, 'maintainer', 'Approved again', 30,
                                                   state='APPROVED', commit_id='a' * 40))
        check = check_pr(self.github, 2, {'ready'})
        self.assertTrue(check.allowed)
        self.assertNotIn(101, check.cleared_comment_ids)

    def test_unaccepted_unreleased_or_foreign_revision_outcomes_do_not_grant_a_head(self):
        self.use_pr()
        self.start(2, 'needs-changes')
        self.approve()
        self.execute(lambda c: self.github.change(2, head='b' * 40))
        self.github.change(2, labels=frozenset({'needs-changes'}))
        history = self.loop.coordinator.history(2)
        lease, outcome = history[-2:]
        original = {r['id']: payload(r) for r in (lease, outcome)}
        for alteration in ('unaccepted', 'unreleased', 'foreign', 'after-assignment'):
            with self.subTest(alteration=alteration):
                for record in (lease, outcome):
                    self.github.update_comment(record['id'], body(original[record['id']]))
                if alteration == 'unaccepted':
                    self.github.update_comment(outcome['id'], body(original[outcome['id']] | {'accepted': False}))
                elif alteration == 'unreleased':
                    self.github.update_comment(lease['id'], body(original[lease['id']] | {'state': 'withdrawn'}))
                elif alteration == 'foreign':
                    row = next(c for c in self.github.store[2] if c['id'] == outcome['id'])
                    row['user'] = {'login': 'outsider'}
                else:
                    # Reapproving a parent after the child run cannot retroactively grant ancestry.
                    self.github.store[2] = [c for c in self.github.store[2]
                                            if not c['body'].startswith('<!-- ub-agent:approval:')]
                    self.github.review_store[2] = [dict(feedback(300, 'maintainer'),
                        created_at='2027-01-01T00:00:00Z', updated_at='2027-01-01T00:00:00Z',
                        state='APPROVED', commit_id='a' * 40)]
                self.assertEqual(check_pr(self.github, 2, {'needs-changes'}).allowed, False)
                # Restore author for the next scenario.
                for row in self.github.store[2]:
                    if row['id'] == outcome['id']:
                        row['user'] = {'login': 'operator'}

    def test_pr_records_reject_wrong_kinds_malformed_heads_and_duplicate_feedback(self):
        raw = approval_body(2, 'Candidate', 'Closes #1', [], head='a' * 40)
        for invalid in ('[]', 'null', '"text"'):
            malformed = raw[:raw.index('{')] + invalid + "\n```\n"
            self.assertIsNone(parse_approval(malformed, 2, 'pr'))
        self.assertIsNone(parse_approval(raw, 2))
        self.assertIsNone(parse_approval(approval_body(2, 'Candidate', 'Closes #1', []), 2, 'pr'))
        self.assertIsNone(parse_approval(raw.replace('a' * 40, 'bad'), 2, 'pr'))
        duplicate = feedback(10)
        self.assertIsNone(parse_approval(approval_body(2, 'Candidate', 'Closes #1', [], head='a' * 40,
                                                     reviews=[duplicate, duplicate]), 2, 'pr'))

    def test_editing_maintainer_review_body_preserves_approval_at_submission(self):
        self.use_pr()
        self.start(2, 'needs-changes')
        review = feedback(100, 'maintainer', 'Approved', 15, state='APPROVED', commit_id='a' * 40)
        review['updated_at'] = at(25)
        self.github.review_store[2] = [review]
        self.assertTrue(check_pr(self.github, 2, {'needs-changes'}).allowed)
        # Editing review prose is not a new approval of later outside input.
        self.github.store[2] = [feedback(101, second=20)]
        self.assertFalse(check_pr(self.github, 2, {'needs-changes'}).allowed)

    def test_unreadable_item_comments_are_parked_without_a_claim(self):
        self.github.unreadable = True
        self.assert_parked('unreadable')
        self.github.unreadable = False
        self.execute()

    def test_outside_pr_starts_use_union_of_pr_and_either_triggers_only(self):
        self.use_pr()
        preparer = replace(self.worker, name='preparer', kind='issue', triggers=('needs-preparation',))
        reviewer = replace(self.worker, name='reviewer', kind='pr', triggers=('needs-review',))
        self.loop.config = config(self.root, preparer, reviewer, self.worker)
        self.start(2, 'needs-preparation')
        self.approve()
        self.assertEqual(self.loop.plans()[0].state, 'parked')
        self.start(2, 'needs-review', second=20)
        self.execute()
