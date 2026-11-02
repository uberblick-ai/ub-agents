from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from ub_agents.coordination import Coordinator
from ub_agents.errors import RecordError
from ub_agents.records import MARKER, _validate_recovery, body, payload, records
from tests.support import FakeGitHub, agent, issue, pr


class CompactRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root, outcomes={
            'done': {'add': ('needs-review',), 'remove': ()},
            'cleaned': {'add': (), 'remove': ('old',)}})
        self.github = FakeGitHub(issue(), pr())
        self.co = Coordinator(self.github, 'operator', lambda: 1000)

    def claim(self):
        return self.co.claim(self.co.plan(self.github.item(1), self.worker, ('needs-human',)),
                             ('needs-human',))

    def test_new_lease_and_outcome_hold_only_their_own_fields(self):
        lease = self.claim()
        self.assertEqual(MARKER, '<!-- ub-agents:v4 -->')
        self.assertTrue(self.github.comments(1)[0]['body'].startswith(MARKER))
        self.assertEqual(lease['triggers'], ['ready'])
        self.assertEqual(set(json.loads(body(payload(lease)).split('```json\n')[1].split('\n```')[0])), {
            'kind', 'run', 'agent', 'assignment', 'runtime', 'triggers', 'created', 'expires',
            'state', 'attempt', 'started', 'attempt_effect'})
        self.co.update(lease, state='running', started=True, host='launcher-host')
        outcome = self.co.report(lease, 'success', 'Done', handoff=2, outcome='done')
        for key in ('run', 'runtime', 'host', 'assignment_sha', 'action'):
            self.assertNotIn(key, outcome)
        self.assertEqual(outcome['transition'], {'add': ['needs-review'], 'started': False})
        self.co.update_outcome(lease, outcome, transition=outcome['transition'] | {'started': True},
                               transition_complete=True)
        self.assertEqual(outcome['transition'], {'add': ['needs-review'], 'started': True})
        self.assertTrue(outcome['transition_complete'])
        self.co.accept(lease, outcome)
        copied, = self.co.history(2)
        self.assertEqual(payload(copied), payload(outcome))
        self.assertEqual(set(json.loads(body(payload(outcome)).split('```json\n')[1].split('\n```')[0])), {
            'kind', 'agent', 'assignment', 'lease_id', 'created', 'status', 'summary', 'handoff',
            'candidate_sha', 'accepted', 'outcome', 'transition', 'transition_complete'})

    def test_pr_lease_keeps_its_original_assignment_head(self):
        plan = self.co.plan(self.github.item(2), self.worker, ('needs-human',))
        lease = self.co.claim(plan, ('needs-human',))
        self.assertEqual(lease['assignment_sha'], self.github.item(2).head)
        self.assertNotIn('branch', lease)
        outcome = self.co.report(lease, 'success', 'Done', outcome='done')
        self.assertNotIn('assignment_sha', outcome)

    def test_explicit_removals_are_preserved_without_implied_triggers(self):
        outcome = self.co.report(self.claim(), 'success', 'Cleaned', outcome='cleaned')
        self.assertEqual(outcome['transition'], {'add': [], 'remove': ['old'], 'started': False})

    def test_reset_needs_no_run_runtime_or_head(self):
        reset = dict(kind='reset', agent='worker', assignment=1, created='2026-10-24T13:42:27Z',
                     summary='Maintainer resolved the blocker')
        comment = self.github.create_comment(1, body(reset))
        self.assertEqual(set(payload(records([comment])[0])) - {'actor'}, set(reset))
        self.assertIn('worker reset — Maintainer resolved', comment['body'])

    def test_older_markers_are_not_parsed(self):
        for version in (1, 2, 3):
            text = f'<!-- ub-agents:v{version} -->\nmalformed old data'
            self.assertEqual(records([dict(id=1, body=text, user={'login': 'operator'})]), [])

    def test_invalid_transitions_fail_closed(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'success', 'Done', outcome='done')
        for transition in ({'add': [], 'started': 1}, {'add': [], 'started': False, 'remove': 'ready'},
                           {'add': [], 'started': False, 'stop_labels': []},
                           {'add': [], 'remove': ['ready'], 'triggers': ['ready'],
                            'stop_labels': [], 'started': False}):
            with self.subTest(transition=transition):
                forged = deepcopy(outcome) | {'transition': transition}
                with self.assertRaises(RecordError):
                    records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])

    def test_every_lease_requires_attempt_effect_and_claim_triggers(self):
        lease = self.claim()
        for recovery in (False, True):
            for field in ('attempt_effect', 'triggers'):
                with self.subTest(recovery=recovery, field=field):
                    forged = payload(lease)
                    if recovery:
                        forged.update(mode='recovery', recovered_lease_id=1, recovered_run='source')
                    forged.pop(field)
                    with self.assertRaises(RecordError):
                        records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])

    def test_recovery_lease_names_the_original_lease_and_run(self):
        lease = self.claim()
        self.co.report(lease, 'success', 'Done', outcome='done')
        self.co.clock = lambda: 1061
        plan = self.co.plan(self.github.item(1), self.worker, ('needs-human',))
        self.assertEqual(plan.state, 'recover')
        recovered = self.co.claim(plan, ('needs-human',), recovery=True)
        self.assertEqual(recovered['attempt_effect'], 'pending')
        self.assertEqual(recovered['recovered_lease_id'], lease['id'])
        self.assertEqual(recovered['recovered_run'], lease['run'])
        self.assertNotIn('outcomes', recovered)

    def test_recovery_validator_checks_identity_before_operator_reason(self):
        recovered = {'mode': 'recovery', 'recovered_lease_id': 1, 'recovered_run': 'source',
                     'recovery_reason': 'Operator confirmed termination'}
        self.assertIsNone(_validate_recovery(recovered))
        for fields, message in (
                ({'recovered_lease_id': True, 'recovery_reason': ''}, 'invalid recovered lease identity'),
                ({'recovered_run': ''}, 'invalid recovered lease identity'),
                ({'recovery_reason': ' '}, 'invalid operator recovery reason'),
                ({'mode': 'execution'}, 'invalid operator recovery reason')):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                _validate_recovery(recovered | fields)
