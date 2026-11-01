from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.coordination import Plan
from ub_agents.loop import Loop
from ub_agents.pass_messages import PassMessages
from tests.support import FakeGitHub, agent, config, issue, stub_refresh


class PassMessagesTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.messages = PassMessages(self.lines.append)
        self.plan = Plan(issue(), agent(Path('.')), None, 'ready', '', 1)

    def test_refusals_keep_agent_order_and_flush_before_success_without_health_waits(self):
        finals = []
        self.messages.refusal_output = finals.append
        self.messages.begin_item()
        self.messages.item_refusal(' first\n refusal ', 'first')
        self.messages.item_refusal('health wait', 'health', on_success=False)
        self.messages.item_refusal('second refusal', 'second')
        self.messages.item_refusal('first replacement', 'first')
        self.assertEqual(self.lines, [])
        self.messages.flush_item_refusals()
        self.assertEqual(self.lines, ['first replacement', 'second refusal'])
        self.assertEqual(finals, [])
        self.messages.flush_item_refusals(final=True)
        self.assertEqual(finals, [])

        self.messages.item_refusal(' health\n wait ', 'health', on_success=False)
        self.messages.item_refusal('last refusal', 'last')
        self.messages.flush_item_refusals(final=True)
        self.assertEqual(finals, [('health wait', 'last refusal')])
        self.messages.end_item()
        self.messages.item_refusal(' outside\n targeted pass ')
        self.assertEqual(self.lines, ['first replacement', 'second refusal', 'outside targeted pass'])

    def test_released_blocker_suppresses_only_the_matching_next_blocked_line(self):
        blocked = replace(self.plan, state='blocked', reason='Last run blocked: Needs a decision')
        self.messages.released_blocker(self.plan, 'Needs a decision')
        self.messages.waiting(blocked)
        self.messages.waiting(blocked)
        self.assertEqual(self.lines, [])
        changed = replace(blocked, reason='A different blocker')
        self.messages.waiting(changed)
        self.assertEqual(self.lines, ['#1 worker: blocked — A different blocker'])
        self.messages.reached(self.plan)
        self.messages.waiting(blocked)
        self.assertEqual(self.lines[-1], '#1 worker: blocked — Last run blocked: Needs a decision')


class MessageLifecycleTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lines = []
        self.loop = Loop(config(self.root), FakeGitHub(issue()), 'operator', output=self.lines.append)
        self.messages = self.loop.pass_messages
        self.plan = Plan(issue(), agent(self.root), None, 'ready', '', 1)

    def test_decline_pass_finishes_on_error_and_an_empty_pass_resets_dedupe(self):
        self.loop.declined(self.plan, 'Head moved before claim')
        with self.assertRaisesRegex(ValueError, 'Interrupted discovery'):
            with self.loop.discovery_pass():
                self.loop.declined(self.plan, 'Head moved before claim')
                self.loop.declined(self.plan, 'Runtime changed before claim')
                raise ValueError('Interrupted discovery')
        self.assertIsNone(self.messages._pass_declines)
        self.assertEqual(self.messages._declines, {(1, 'worker'): ('declined', 'Runtime changed before claim')})
        with self.loop.discovery_pass():
            self.loop.declined(self.plan, 'Runtime changed before claim')
        with self.loop.discovery_pass():
            pass
        self.loop.declined(self.plan, 'Runtime changed before claim')
        self.assertEqual([line for line in self.lines if line.startswith('#')], [
            '#1 worker: declined — Head moved before claim',
            '#1 worker: declined — Runtime changed before claim',
            '#1 worker: declined — Runtime changed before claim',
        ])

    def test_tick_prunes_messages_only_after_exhausting_plans(self):
        waiting = replace(self.plan, state='owned', reason='An unexpired assignment owns this work item')
        other = replace(self.plan, item=issue(2))
        self.messages.waiting(waiting)
        self.messages.released_blocker(other, 'Needs a decision')
        with patch.object(self.loop, 'iter_plans', return_value=iter([other])), \
                patch.object(self.loop, 'execute', return_value=True):
            self.assertTrue(self.loop.tick())
        self.assertEqual(self.messages._shown, {(1, 'worker'): ('owned', waiting.reason)})
        self.assertEqual(self.messages._released_blockers, {(2, 'worker'): 'Needs a decision'})
        with patch.object(self.loop, 'iter_plans', return_value=iter([waiting])):
            self.assertFalse(self.loop.tick())
        self.assertEqual(self.messages._shown, {(1, 'worker'): ('owned', waiting.reason)})
        self.assertEqual(self.messages._released_blockers, {})
        self.assertEqual(self.lines.count('#1 worker: owned — ' + waiting.reason), 1)
        with patch.object(self.loop, 'iter_plans', return_value=iter([])):
            self.assertFalse(self.loop.tick())
        self.assertEqual(self.messages._shown, {})

    def test_targeted_exception_discards_refusals_and_restores_immediate_output(self):
        def fail(*args):
            self.messages.item_refusal('Buffered refusal', 'worker')
            raise ValueError('Interrupted discovery')

        with patch.object(self.loop, '_discovery_tick_item', side_effect=fail), \
                self.assertRaisesRegex(ValueError, 'Interrupted discovery'):
            self.loop.tick_item(1)
        self.assertIsNone(self.messages._item_refusals)
        self.assertIsNone(self.messages._pass_declines)
        self.assertFalse(self.loop._targeted_pass)
        self.assertFalse(any('Buffered refusal' in line for line in self.lines))
        redirected = []
        self.loop.output = redirected.append
        self.messages.item_refusal('After interruption')
        self.assertEqual(redirected, ['After interruption'])
