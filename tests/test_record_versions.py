from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.approvals import ApprovalCheck
from ub_agents.coordination import Coordinator
from ub_agents.errors import RecordError
from ub_agents.records import MARKER, attempts, body, iso, records
from tests.support import FakeGitHub, agent, issue, pr


class RecordVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root)
        self.github = FakeGitHub(issue(), pr())
        self.co = Coordinator(self.github, 'operator', lambda: 1000)

    def claim(self):
        return self.co.claim(self.co.plan(self.github.item(1), self.worker, ()))

    def old_body(self, text, version):
        marker = f'<!-- ub-agents:v{version} -->'
        if version == 1:
            data = text.split('```json\n', 1)[1].split('\n```', 1)[0]
            return f'{marker}\nOld record\n\n```json\n{data}\n```\n'
        return text.replace(MARKER, marker, 1)

    def test_older_versions_have_no_coordination_authority(self):
        for version in (1, 2):
            for state, result, effect, expires in (
                    ('claiming', None, 'pending', 1060),
                    ('running', None, 'pending', 1060),
                    ('running', None, 'pending', 999),
                    ('released', 'retry', 'failure', 999),
                    ('released', 'blocked', 'unchanged', 999),
                    ('released', 'success', 'reset', 999)):
                with self.subTest(version=version, state=state, result=result, expires=expires):
                    self.setUp()
                    lease = self.claim()
                    self.co.update(lease, state='running', started=True)
                    outcome = self.co.report(lease, 'success', 'Old feedback', handoff=2, outcome='done')
                    self.co.accept(lease, outcome)
                    self.co.update(lease, state=state, result=result, attempt_effect=effect,
                                   expires=iso(expires))
                    marker = f'<!-- ub-agents:v{version} -->'
                    for comments in self.github.store.values():
                        for comment in comments:
                            comment['body'] = self.old_body(comment['body'], version)
                    self.github.create_comment(1, self.old_body(body({
                        'kind': 'reset', 'run': 'old-reset', 'agent': 'worker',
                        'runtime': 'operator', 'created': iso(1000), 'assignment': 1,
                        'summary': 'Old reset'}), version))
                    # Payload, author and routing metadata are never inspected.
                    self.github.store[1].extend([
                        {'id': 90, 'body': marker + '\nunreadable record'},
                        {'id': 91, 'body': marker + '\n```json\n{}\n```',
                         'user': {'login': 'operator'}, 'issue_url': 'not an assignment URL'}])
                    self.github.next_id = 100
                    self.assertEqual(self.co.history(1), [])
                    self.assertEqual(self.co.history(2), [])
                    self.assertEqual(self.co.repository_history(), ([], set()))
                    self.assertEqual(attempts(self.co.history(1), 'worker', 1000), [])
                    self.assertIsNone(self.co.outcome(lease))
                    self.assertIsNone(self.co.pending_completion(self.co.history(1), 'worker', 1000))
                    self.assertEqual(self.co.feedback(self.github.item(2), 'implementer'), [])
                    self.assertEqual(self.co.notices.comments(1), [])
                    plan = self.co.plan(self.github.item(1), self.worker, ())
                    self.assertEqual((plan.state, plan.attempt), ('ready', 1))
                    fresh = self.co.claim(plan)
                    self.assertIsNotNone(fresh)
                    self.co.assert_owned(fresh)
                    self.co.notices.superseded(1, 'worker', fresh['run'])
                    self.assertFalse(any(w[0] == 'minimize' for w in self.github.writes))

    def test_older_versions_do_not_set_approval_notice_epoch(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.setUp()
                self.claim()
                comment = self.github.store[1][0]
                comment['body'] = self.old_body(comment['body'], version)
                check = ApprovalCheck(False, 'Approval required', gate='start', gate_key='gate')
                self.co.notices.approval(1, check, ('needs-human',), ('ready',))
                self.assertTrue(self.github.store[1][-1]['body'].startswith(
                    '<!-- ub-agents:action-needed approval-gate-0 -->'))

    def test_older_versions_are_skipped_before_author_trust(self):
        comments = [{'body': f'<!-- ub-agents:v{version} -->\nmalformed'} for version in (1, 2)]
        with patch('ub_agents.records.validate', side_effect=AssertionError('validation')):
            self.assertEqual(records(comments, trusted=lambda _: self.fail('author lookup')), [])

    def test_malformed_current_version_still_blocks_its_item(self):
        lease = self.claim()
        self.github.update_comment(lease['id'], MARKER + '\nunreadable record')
        with self.assertRaises(RecordError):
            self.co.history(1)
        self.assertEqual(self.co.repository_history(), ([], {1}))
