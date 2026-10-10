"""Every post-exit GitHub request can fail without discarding an agent report."""

from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import AgentError, GitHubError, LostOwnership
from ub_agents.github import GitHub
from ub_agents.loop import Loop, POLL_FAILURE_LIMIT
from ub_agents.notices import ACTION_MARKER
from ub_agents.rate_limits import READS
from ub_agents.records import MARKER, body, payload, records, timestamp
from tests.support import FakeGitHub, RecordingRunner, agent, config, issue, pr, stub_refresh


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def scenario(self, status='success', fail_at=None, *, once=False, failure=None,
                 lost_response=False, handoff=False, recovering=False, trace=None, after_write=None):
        github = FakeGitHub(issue(), pr(296, labels=() if handoff else ('needs-review',)))
        worker = agent(self.root, name='reviewer', kind='issue' if handoff else 'pr',
                       triggers=('ready',) if handoff else ('needs-review',),
                       outcomes={'changes-requested': {'add': ('needs-changes',),
                                                       'remove': ('ready',) if handoff else ('needs-review',)}})
        number = 1 if handoff else 296
        lines = []
        loop = Loop(config(self.root, worker), github, 'operator', output=lines.append,
                    interrupt_event=threading.Event())
        now = [timestamp()]
        loop.coordinator.clock = lambda: now[0]
        exited, calls, writes, passes = [], [], [], []
        runner = RecordingRunner(self.root)
        methods = {'create_comment': 'POST', 'update_comment': 'PATCH',
                   'add_labels': 'POST', 'remove_label': 'DELETE',
                   'delete_comment': 'DELETE', 'minimize_comment': 'POST'}

        def request_error(name):
            method = methods.get(name, 'GET')
            endpoint = f'repos/org/project/{name}'
            command = ('gh', 'api', '--hostname', 'github.com', '--method', method, '-H',
                       'Accept: application/vnd.github+json', '--include', endpoint)
            runner.responses[command] = subprocess.CompletedProcess([], 1, '', 'unexpected end of JSON input')
            with self.assertRaises(GitHubError) as raised:
                GitHub('org/project', runner).request(endpoint, method=method, array=True)
            self.assertTrue(raised.exception.retryable)
            return raised.exception

        def wrap(name, operation):
            def call(*args, **kwargs):
                active = bool(exited) and len(passes) == 1
                if active:
                    calls.append(name)
                record = None
                if name in {'create_comment', 'update_comment'} and exited and args[-1].startswith(MARKER):
                    record, = records([{'id': 0, 'body': args[-1], 'user': {'login': 'operator'}}])
                    writes.append(record)
                if (active and loop.github._finalization is not None and trace is not None
                        and (name == 'item' or name in methods)):
                    trace.append((name, args[0], record))
                if active and (fail_at == len(calls) - 1 or fail_at == name):
                    if lost_response:
                        operation(*args, **kwargs)
                    raise failure or request_error(name)
                result = operation(*args, **kwargs)
                if active and name in methods and after_write is not None:
                    after_write(loop)
                return result
            return call

        history = loop.coordinator._history
        def read_history(number):
            if exited and len(passes) == 1 and loop.github._finalization is not None and trace is not None:
                trace.append(('history', number, None))
            return history(number)

        def report(lease):
            return loop.coordinator.report(lease, status, 'Recorded review result',
                handoff=296 if handoff else None,
                outcome='changes-requested' if status == 'success' else None,
                action='Maintainer: resolve the review blocker.' if status == 'blocked' else None)

        def execute(*args, **kwargs):
            lease = loop.coordinator.history(number)[0]
            report(lease)
            exited.append(True)
            return 0

        if recovering:
            source = loop.coordinator.claim(loop.plans()[0])
            loop.coordinator.update(source, state='running', started=True)
            report(source)
            now[0] += 61
            exited.append(True)

        tick = loop.tick
        def pass_once():
            passes.append(True)
            self.assertLessEqual(len(passes), 2, 'Recovery must finish on the next pass')
            result = tick()
            if len(passes) == 2:
                loop.stop_event.set()
            return result

        def wait(delay):
            for lease in loop.coordinator.history(number):
                if lease['kind'] == 'lease' and lease['state'] in {'running', 'claiming'}:
                    self.assertNotIn('result', lease)
                    self.assertEqual(lease['attempt_effect'], 'pending')
            now[0] += 61  # Expire the lease before the one later recovery pass.

        with ExitStack() as stack:
            stack.enter_context(patch.object(loop.coordinator, '_history', side_effect=read_history))
            if trace is not None:
                # Display refresh belongs to the next discovery phase.
                stack.enter_context(patch.object(loop, '_replan_finished_item'))
            for name in sorted(READS | methods.keys()):
                operation = getattr(github, name, None)
                if operation is not None:
                    stack.enter_context(patch.object(github, name, side_effect=wrap(name, operation)))
            stack.enter_context(patch.object(loop, 'tick', side_effect=pass_once))
            stack.enter_context(patch.object(loop.stop_event, 'wait', side_effect=wait))
            stack.enter_context(patch('ub_agents.loop.collect_denials', return_value={'denials': []}))
            supervise = stack.enter_context(patch('ub_agents.loop.supervise', side_effect=execute))
            error = None
            try:
                loop.launch(once=once)
            except AgentError as exc:
                error = exc
        return github, loop, calls, writes, lines, supervise.call_count, error

    def test_completion_shares_history_and_items_until_each_write(self):
        for status, handoff, recovering in (('success', True, False), ('success', False, False),
                                            ('retry', False, False), ('blocked', False, False),
                                            ('success', True, True), ('success', False, True),
                                            ('retry', False, True), ('blocked', False, True)):
            with self.subTest(status=status, handoff=handoff, recovering=recovering):
                trace = []
                github, loop, _, _, _, _, error = self.scenario(status, once=True, handoff=handoff,
                                                               recovering=recovering, trace=trace)
                self.assertIsNone(error)
                assignment = 1 if handoff else 296
                reads, released = set(), False
                for name, number, record in trace:
                    if name in {'history', 'item'}:
                        self.assertNotIn((name, number), reads, trace)
                        reads.add((name, number))
                    else:
                        # Advisory presentation writes after release keep their
                        # existing semantics; durable writes require ownership.
                        if not released:
                            self.assertIn(('history', assignment), reads, trace)
                        reads.clear()
                        if record and record.get('state') == 'released':
                            released = True
                self.assertTrue(released)
                lease = [r for r in loop.coordinator.history(assignment) if r['kind'] == 'lease'][-1]
                self.assertEqual((lease['state'], lease['result']), ('released', status))
                if status == 'success':
                    outcome = loop.coordinator.history(assignment)[1]
                    self.assertTrue(outcome['accepted'])
                    if handoff:
                        self.assertTrue(loop.coordinator.history(296)[0]['accepted'])
                else:
                    self.assertEqual(lease['attempt_effect'], 'failure' if status == 'retry' else 'unchanged')

    def test_ownership_lost_between_writes_stops_before_second_write(self):
        for handoff in (False, True):
            baseline = []
            self.assertIsNone(self.scenario(once=True, handoff=handoff, trace=baseline)[-1])
            durable = []
            for name, _, record in baseline:
                if name not in {'history', 'item'}:
                    durable.append(name)
                if record and record.get('state') == 'released':
                    break
            for index in range(len(durable) - 1):
                with self.subTest(handoff=handoff, after=durable[index], index=index):
                    trace, completed = [], []
                    def lose_ownership(loop):
                        completed.append(True)
                        if len(completed) != index + 1:
                            return
                        lease = loop.github.lease
                        # Another writer revokes the lease between this pair of
                        # durable writes, outside the launcher's read window.
                        for comment in loop.github.github.store[lease['assignment']]:
                            if comment['id'] == lease['id']:
                                comment['body'] = body(payload(lease) | {'state': 'withdrawn'})
                    result = self.scenario(once=True, handoff=handoff, trace=trace, after_write=lose_ownership)
                    self.assertIsInstance(result[-1], LostOwnership)
                    self.assertEqual([name for name, _, _ in trace if name not in {'history', 'item'}],
                                     durable[:index + 1], trace)
                    assignment = 1 if handoff else 296
                    self.assertEqual(len([name for name, number, _ in trace
                                          if name == 'history' and number == assignment]), index + 2, trace)
                    self.assertIsNone(result[1].github._finalization)

    def assert_finished(self, scenario, status, *, recovering=False, handoff=False):
        github, loop, calls, writes, lines, executions, error = scenario
        self.assertIsNone(error)
        self.assertEqual(executions, 0 if recovering else 1)
        number = 1 if handoff else 296
        history = loop.coordinator.history(number)
        leases = [r for r in history if r['kind'] == 'lease']
        self.assertEqual(leases[-1]['state'], 'released')
        self.assertEqual(leases[-1]['result'], status)
        if status == 'success':
            for record in leases + [r for r in writes if r['kind'] == 'lease']:
                self.assertNotEqual(record.get('result'), 'blocked')
                self.assertNotEqual(record.get('attempt_effect'), 'failure')
            outcome = next(r for r in history if r.get('outcome') == 'changes-requested')
            self.assertTrue(outcome['accepted'])
            self.assertTrue(outcome['transition_complete'])
            self.assertEqual(github.item(296).labels, frozenset({'needs-changes'}))
            if handoff:
                self.assertFalse(github.item(1).labels)
                copy = next(r for r in loop.coordinator.history(296) if r['kind'] == 'outcome')
                self.assertTrue(copy['accepted'])
        else:
            self.assertEqual(leases[-1]['attempt_effect'], 'unchanged')
            notices = [c for c in github.comments(number) if c['body'].startswith(ACTION_MARKER)]
            self.assertEqual(len(notices), 1)
            self.assertIn('Maintainer: resolve the review blocker.', notices[0]['body'])

    def test_every_post_exit_request_recovers_in_one_later_pass(self):
        for status, handoff, recovering in (('success', False, False), ('blocked', False, False),
                                            ('success', True, False), ('success', True, True),
                                            ('blocked', False, True)):
            baseline = self.scenario(status, once=True, handoff=handoff, recovering=recovering)
            self.assert_finished(baseline, status, handoff=handoff, recovering=recovering)
            for index, name in enumerate(baseline[2]):
                with self.subTest(status=status, handoff=handoff, recovering=recovering,
                                  call=index, request=name):
                    result = self.scenario(status, index, handoff=handoff, recovering=recovering)
                    self.assert_finished(result, status, handoff=handoff, recovering=recovering)
                    self.assertEqual(result[2][index], name)
                    # Advisory presentation errors may finish the pass normally.
                    self.assertTrue(any(line.startswith(('Skipped GitHub poll:', 'Advisory '))
                                        for line in result[4]))

    def test_lost_patch_label_and_notice_responses_remain_idempotent(self):
        for status, handoff, recovering in (('success', False, False), ('blocked', False, False),
                                            ('success', True, False), ('blocked', False, True)):
            baseline = self.scenario(status, once=True, handoff=handoff, recovering=recovering)
            for index, name in enumerate(baseline[2]):
                if name not in {'create_comment', 'update_comment', 'add_labels', 'remove_label'}:
                    continue
                with self.subTest(status=status, handoff=handoff, recovering=recovering,
                                  call=index, request=name):
                    result = self.scenario(status, index, lost_response=True,
                                           handoff=handoff, recovering=recovering)
                    self.assert_finished(result, status, handoff=handoff, recovering=recovering)

    def test_once_and_nonretryable_finalization_fail_on_first_error(self):
        for once, error in ((True, None),
                            (False, GitHubError('PATCH', 'outcome', 'permission denied'))):
            with self.subTest(once=once):
                result = self.scenario(fail_at='update_comment', once=once, failure=error)
                self.assertIsInstance(result[-1], AgentError)
                history = result[1].coordinator.history(296)
                self.assertEqual(history[0]['state'], 'running')
                self.assertNotIn('result', history[0])
                self.assertEqual(result[5], 1)
                self.assertFalse(any(line.startswith('Skipped') for line in result[4]))

    def test_finalization_errors_count_toward_existing_poll_limit(self):
        loop = Loop(config(self.root), FakeGitHub(issue()), 'operator', output=lambda line: None)
        failure = GitHubError('PATCH', 'outcome', 'unexpected end of JSON input', retryable=True)
        def tick():
            loop._poll_complete = loop._finalizing = True
            raise failure
        with patch.object(loop, 'tick', side_effect=tick) as passes, \
                patch.object(loop.stop_event, 'wait') as waits, self.assertRaisesRegex(AgentError, 'retries exhausted'):
            loop.launch()
        self.assertEqual(passes.call_count, POLL_FAILURE_LIMIT)
        self.assertEqual([c.args[0] for c in waits.call_args_list], [5, 10, 20, 40, 60])
