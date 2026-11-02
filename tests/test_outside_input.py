from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import filter_input
from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.records import attempts, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh
from tests.test_approval_enforcement import feedback
from tests.test_approvals import at


class OutsideInputTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root)
        self.github = FakeGitHub(issue())
        self.github.roles['outsider'] = 'read'
        self.cfg = config(self.root, self.worker)
        self.loop = Loop(self.cfg, self.github, 'operator', output=lambda *_: None)
        self.github.timelines[1] = [dict(event='labeled', actor={'login': 'maintainer'},
            label={'name': 'ready'}, created_at=at(20))]

    def execute(self, expected, number=1):
        def run(command, cwd, env, run_dir, *args, **kwargs):
            self.assertEqual('outside-input' in self.github.item(number).labels, expected)
            history = self.loop.coordinator.history(number)
            context = json.loads(Path(env['UB_AGENTS_CONTEXT']).read_text())
            prompt = self.loop.prompt_for(self.loop.coordinator.plan(
                self.github.item(number), self.worker, self.cfg.stop_labels), history[-1], context, '')
            labels = prompt.split('Do not change workflow labels', 1)[1].split('\n', 1)[0]
            self.assertEqual('"outside-input"' in labels, self.loop.approvals == 'on')
            self.loop.coordinator.report(history[-1], 'success', 'Done', outcome='done')
            return 0
        with patch('ub_agents.assignment_run.supervise', side_effect=run) as process:
            self.assertTrue(self.loop.tick())
        process.assert_called_once()
        self.assertEqual(self.loop.coordinator.history(number)[-2]['result'], 'success')

    def test_each_outside_assignment_source_is_labelled_before_execution(self):
        for kind in ('issue', 'pr'):
            for source in ('author', 'title', 'body', 'comments', 'reviews', 'review_comments'):
                if kind == 'issue' and source in ('reviews', 'review_comments'):
                    continue
                with self.subTest(kind=kind, source=source):
                    self.setUp()
                    if kind == 'pr':
                        self.github.items[1] = replace(pr(), number=1, labels=frozenset({'ready'}))
                    if source == 'author':
                        self.github.content_histories[1] = {'author': {'login': 'outsider'}}
                        if kind == 'pr':
                            self.github.review_store[1] = [feedback(200, 'maintainer', second=15,
                                state='APPROVED', commit_id='a' * 40)]
                    elif source == 'title':
                        self.github.timelines[1].append(dict(event='renamed', actor={'login': 'outsider'},
                            rename={'from': 'Old', 'to': 'Requirements'}, created_at=at(10)))
                    elif source == 'body':
                        self.github.content_histories[1] = {'lastEditedAt': at(10), 'edits': [
                            dict(editedAt=at(10), editor={'login': 'outsider'}, deletedAt=None)]}
                    else:
                        store = {'comments': self.github.store, 'reviews': self.github.review_store,
                                 'review_comments': self.github.review_comment_store}[source]
                        store[1] = [feedback(100, second=10)]
                    self.execute(True)

    def test_trusted_withheld_and_bot_feedback_do_not_label(self):
        self.loop.config = replace(self.cfg, trusted_bots=('helper[bot]',))
        self.github.store[1] = [feedback(100, 'operator', second=10),
                                feedback(101, 'maintainer', second=10),
                                feedback(102, second=25), feedback(103, 'helper[bot]', second=10) |
                                {'user': {'login': 'helper[bot]', 'type': 'Bot'}}]
        self.execute(False)

    def test_failed_write_spends_no_attempt_and_next_poll_retries(self):
        self.github.content_histories[1] = {'author': {'login': 'outsider'}}
        with patch.object(self.github, 'add_labels', side_effect=GitHubError('POST', 'labels', 'Unavailable')), \
                patch('ub_agents.assignment_run.supervise') as process:
            self.assertTrue(self.loop.tick())
        process.assert_not_called()
        history = self.loop.coordinator.history(1)
        self.assertEqual((history[0]['state'], history[0]['started']), ('withdrawn', False))
        self.assertEqual(attempts(history, self.worker.name, timestamp()), [])
        self.execute(True)

    def test_failed_withdrawal_after_label_failure_still_spends_no_attempt_on_expiry(self):
        self.github.content_histories[1] = {'author': {'login': 'outsider'}}
        update = self.loop.coordinator.update
        def fail_withdrawal(lease, **changes):
            if changes.get('state') == 'withdrawn':
                raise GitHubError('PATCH', 'lease', 'Unavailable')
            return update(lease, **changes)
        with patch.object(self.github, 'add_labels', side_effect=GitHubError('POST', 'labels', 'Unavailable')), \
                patch.object(self.loop.coordinator, 'update', side_effect=fail_withdrawal), \
                patch('ub_agents.assignment_run.supervise') as process, self.assertRaises(GitHubError):
            self.loop.tick()
        process.assert_not_called()
        history = self.loop.coordinator.history(1)
        self.assertFalse(history[0]['started'])
        self.assertEqual(attempts(history, self.worker.name, timestamp() + 120), [])

    def test_conflict_routing_failure_after_labelling_spends_attempt_on_expiry(self):
        self.worker = replace(self.worker, kind='pr', on_conflict='done')
        self.cfg = config(self.root, self.worker)
        self.loop = Loop(self.cfg, self.github, 'operator', output=lambda *_: None)
        self.github.items[1] = replace(pr(1, labels=('ready',)), mergeable=False)
        self.github.store[1] = [feedback(100, second=10)]
        with patch.object(self.loop.coordinator, 'report',
                          side_effect=GitHubError('POST', 'outcome', 'Unavailable')), \
                patch('ub_agents.assignment_run.supervise') as process, self.assertRaises(GitHubError):
            self.loop.tick()
        process.assert_not_called()
        self.assertIn('outside-input', self.github.item(1).labels)
        lease, = history = self.loop.coordinator.history(1)
        self.assertEqual((lease['state'], lease['started']), ('running', True))
        self.assertEqual(attempts(history, self.worker.name, timestamp() + 120), [lease])

    def test_removal_holds_until_outside_text_is_input_again(self):
        self.github.store[1] = [feedback(100, second=10)]
        self.execute(True)
        self.github.remove_label(1, 'outside-input')
        self.github.store[1] = [row for row in self.github.store[1] if row['id'] != 100]
        self.github.change(1, labels=frozenset({'ready'}))
        self.loop = Loop(self.cfg, self.github, 'operator', output=lambda *_: None)
        self.execute(False)
        self.github.store[1].append(feedback(101, second=10))
        self.github.change(1, labels=frozenset({'ready'}))
        self.loop = Loop(self.cfg, self.github, 'operator', output=lambda *_: None)
        self.execute(True)

    def test_off_neither_adds_nor_lists_label_even_for_outside_author(self):
        self.loop.config = replace(self.cfg, approvals='off')
        self.github.content_histories[1] = {'author': {'login': 'outsider'}}
        with patch.object(self.github, 'issue_content', side_effect=AssertionError('new read')), \
                patch.object(self.github, 'timeline', side_effect=AssertionError('new read')):
            self.execute(False)

    def test_visibility_resolves_label_behavior(self):
        for visibility in ('public', 'private', 'internal'):
            with self.subTest(visibility=visibility):
                self.setUp()
                self.loop.config = replace(self.cfg, approvals=None)
                self.github.repository_visibility = visibility
                self.github.content_histories[1] = {'author': {'login': 'outsider'}}
                self.execute(visibility == 'public')

    def test_reading_another_item_does_not_label_either_item(self):
        self.github.items[2] = issue(2)
        self.github.content_histories[2] = {'author': {'login': 'outsider'}}
        before = self.github.writes[:]
        filter_input(self.github, self.github.item(2), 'on', ('ready',), read_only=True)
        self.assertEqual(self.github.writes, before)
        self.execute(False)
