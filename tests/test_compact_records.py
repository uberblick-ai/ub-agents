from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from ub_agents.coordination import Coordinator
from ub_agents.errors import RecordError
from ub_agents.loop import Loop
from ub_agents.records import body, declared_transition, payload, records
from tests.support import FakeGitHub, agent, config, issue, write_legacy_records


class CompactRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root, outcomes={
            'done': {'add': ('needs-review',), 'remove': ()},
            'cleaned': {'add': (), 'remove': ('old',)}})
        self.github = FakeGitHub(issue())
        self.co = Coordinator(self.github, 'operator', lambda: 1000)

    def claim(self):
        return self.co.claim(self.co.plan(self.github.item(1), self.worker, ('needs-human',)),
                             ('needs-human',))

    def test_new_lease_and_outcomes_only_store_shared_context_once(self):
        lease = self.claim()
        self.assertEqual(lease['triggers'], ['ready'])
        self.assertEqual(lease['declared_triggers'], ['ready', 'needs-changes'])
        self.assertEqual(lease['stop_labels'], ['needs-human'])
        self.assertEqual(lease['outcomes'], {'done': ['needs-review'], 'cleaned': {'add': [], 'remove': ['old']}})
        outcome = self.co.report(lease, 'success', 'Done', outcome='done')
        self.assertEqual(outcome['transition'], {'add': ['needs-review'], 'started': False})
        self.co.update_outcome(lease, outcome, transition=outcome['transition'] | {'started': True},
                               transition_complete=True)
        self.assertEqual(outcome['transition'], {'add': ['needs-review'], 'started': True})
        self.assertTrue(outcome['transition_complete'])

    def test_explicit_removals_are_preserved_without_implied_triggers(self):
        outcome = self.co.report(self.claim(), 'success', 'Cleaned', outcome='cleaned')
        self.assertEqual(outcome['transition'], {'add': [], 'remove': ['old'], 'started': False})

    def test_upgraded_reporter_writes_compact_outcome_for_a_legacy_lease(self):
        write_legacy_records(self.github)
        lease = self.claim()
        self.assertNotIn('declared_triggers', lease)
        # Restore the upgraded writer after simulating the old claim.
        self.github.create_comment = FakeGitHub.create_comment.__get__(self.github)
        outcome = self.co.report(lease, 'success', 'Cleaned', outcome='cleaned')
        self.assertEqual(outcome['transition'], {'add': [], 'remove': ['old'], 'started': False})
        loop = Loop(config(self.root, self.worker), self.github, 'operator')
        self.assertEqual(loop.validate_report(outcome)['remove'], ['needs-changes', 'old', 'ready'])

    def test_five_outcome_snapshot_shrinks_by_more_than_half(self):
        self.worker = agent(self.root, triggers=('needs-preparation',), outcomes={
            name: {'add': labels, 'remove': ()} for name, labels in {
                'prepared': ('ready',), 'needs-human': ('needs-human',),
                'duplicate': (), 'invalid': ('invalid',), 'needs-info': ('needs-info',)}.items()})
        self.github.change(1, labels=frozenset({'needs-preparation'}))
        lease = self.claim()
        compact = {key: lease[key] for key in ('outcomes', 'declared_triggers', 'stop_labels')}
        legacy = {'outcomes': {name: declared_transition(lease, name) for name in lease['outcomes']}}
        self.assertLess(len(json.dumps(compact)), len(json.dumps(legacy)) / 2)
        self.assertLess(len(json.dumps(compact)), 300)

    def test_invalid_compact_snapshots_and_transitions_fail_closed(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'success', 'Done', outcome='done')
        for key, value in (('declared_triggers', 'ready'), ('stop_labels', [None]),
                           ('stop_labels', None), ('outcomes', {'done': {'remove': []}}),
                           ('outcomes', {'done': ['']}),
                           ('outcomes', {'done': {'add': [], 'triggers': ['ready']}})):
            with self.subTest(key=key, value=value):
                forged = payload(lease) | {key: value}
                with self.assertRaises(RecordError):
                    records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])
        for transition in ({'add': [], 'started': 1}, {'add': [], 'started': False, 'remove': 'ready'},
                           {'add': [], 'started': False, 'stop_labels': []}):
            with self.subTest(transition=transition):
                forged = deepcopy(outcome) | {'transition': transition}
                with self.assertRaises(RecordError):
                    records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])
