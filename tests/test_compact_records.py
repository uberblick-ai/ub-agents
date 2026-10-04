from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from ub_agents.coordination import Coordinator
from ub_agents.errors import RecordError
from ub_agents.records import MARKER, body, declared_transition, payload, records
from tests.support import FakeGitHub, agent, issue


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
        self.assertEqual(MARKER, '<!-- ub-agents:v3 -->')
        self.assertTrue(self.github.comments(1)[0]['body'].startswith(MARKER))
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

    def test_five_outcome_snapshot_shrinks_by_more_than_half(self):
        self.worker = agent(self.root, triggers=('needs-preparation',), outcomes={
            name: {'add': labels, 'remove': ()} for name, labels in {
                'prepared': ('ready',), 'needs-human': ('needs-human',),
                'duplicate': (), 'invalid': ('invalid',), 'needs-info': ('needs-info',)}.items()})
        self.github.change(1, labels=frozenset({'needs-preparation'}))
        lease = self.claim()
        compact = {key: lease[key] for key in ('outcomes', 'declared_triggers', 'stop_labels')}
        expanded = {'outcomes': {name: declared_transition(lease, name) for name in lease['outcomes']}}
        self.assertLess(len(json.dumps(compact)), len(json.dumps(expanded)) / 2)
        self.assertLess(len(json.dumps(compact)), 300)

    def test_invalid_compact_snapshots_and_transitions_fail_closed(self):
        lease = self.claim()
        outcome = self.co.report(lease, 'success', 'Done', outcome='done')
        for key, value in (('declared_triggers', 'ready'), ('stop_labels', [None]),
                           ('stop_labels', None), ('outcomes', {'done': {'remove': []}}),
                           ('outcomes', {'done': ['']}),
                           ('outcomes', {'done': {'add': [], 'triggers': ['ready']}}),
                           ('outcomes', {'done': {'add': [], 'remove': ['ready'],
                                                  'triggers': ['ready'], 'stop_labels': []}})):
            with self.subTest(key=key, value=value):
                forged = payload(lease) | {key: value}
                with self.assertRaises(RecordError):
                    records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])
        for transition in ({'add': [], 'started': 1}, {'add': [], 'started': False, 'remove': 'ready'},
                           {'add': [], 'started': False, 'stop_labels': []},
                           {'add': [], 'remove': ['ready'], 'triggers': ['ready'],
                            'stop_labels': [], 'started': False}):
            with self.subTest(transition=transition):
                forged = deepcopy(outcome) | {'transition': transition}
                with self.assertRaises(RecordError):
                    records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])

    def test_every_lease_requires_attempt_effect_and_shared_context(self):
        lease = self.claim()
        for recovery in (False, True):
            for field in ('attempt_effect', 'declared_triggers', 'stop_labels'):
                with self.subTest(recovery=recovery, field=field):
                    forged = payload(lease)
                    if recovery:
                        forged.pop('outcomes')
                        forged.update(mode='recovery', recovered_lease_id=1, recovered_run='source')
                    forged.pop(field)
                    with self.assertRaises(RecordError):
                        records([{'id': 1, 'body': body(forged), 'user': {'login': 'operator'}}])

    def test_recovery_lease_also_writes_attempt_effect_and_shared_context(self):
        lease = self.claim()
        self.co.report(lease, 'success', 'Done', outcome='done')
        self.co.clock = lambda: 1061
        plan = self.co.plan(self.github.item(1), self.worker, ('needs-human',))
        self.assertEqual(plan.state, 'recover')
        recovered = self.co.claim(plan, ('needs-human',), recovery=True)
        self.assertEqual(recovered['attempt_effect'], 'pending')
        self.assertEqual(recovered['declared_triggers'], ['ready', 'needs-changes'])
        self.assertEqual(recovered['stop_labels'], ['needs-human'])
