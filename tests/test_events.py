"""Project notifications observe committed lifecycle steps without controlling them."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from time import monotonic
import unittest
from unittest.mock import patch

from ub_agents.config import EventsHook
from ub_agents.errors import GitHubError, RetryableExecutionError
from ub_agents.events import EventHooks
from ub_agents.execution import Workspace, group_members
from ub_agents.loop import Loop
from ub_agents.records import body, payload, timestamp
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class EventTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.worker = agent(self.root, kind='issue', outcomes={'done': {'add': ('needs-review',), 'remove': ('old',)}})
        self.github = FakeGitHub(issue(labels=('ready', 'old')), pr(labels=()))
        self.now = timestamp()

    def loop(self, *, worker=None, hook=None):
        cfg = replace(config(self.root, worker or self.worker),
                      events=hook or EventsHook(('notify',), ('started', 'outcome')))
        lines, delivered = [], []
        loop = Loop(cfg, self.github, 'operator', output=lines.append)
        loop.coordinator.clock = lambda: self.now
        delivery = patch.object(loop.events, '_deliver', side_effect=lambda root, hook, data: delivered.append(json.loads(data)))
        delivery.start()
        self.addCleanup(delivery.stop)
        self.addCleanup(loop.events.close)
        return loop, delivered, lines

    def run_agent(self, loop, *, status='success', summary='Completed', action=None, option=None,
                  handoff=None, failure=None, report=True):
        def execute(command, cwd, env, *args, **kwargs):
            lease = loop.github.lease
            if report:
                loop.coordinator.report(lease, status, summary, handoff=handoff,
                                        outcome='done' if status == 'success' else None,
                                        action=action, option=option)
            if failure:
                raise failure
            return 0
        with patch('ub_agents.loop.supervise', side_effect=execute):
            return loop.tick()

    def test_started_precedes_workspace_and_outcome_follows_applied_transition(self):
        loop, delivered, _ = self.loop()
        prepare = Workspace.prepare
        def workspace(workspace):
            lease = workspace.lease
            self.assertTrue(lease['started'])
            self.assertEqual(lease['state'], 'running')
            self.assertEqual(emitted.call_args.args[2], 'started')
            return prepare(workspace)
        def deliver(root, hook, data):
            event = json.loads(data)
            if event['event'] == 'outcome':
                self.assertEqual(self.github.item(2).labels, {'needs-review'})
                self.assertEqual(loop.coordinator.history(1)[0]['state'], 'released')
            delivered.append(event)
        with patch.object(loop.events, 'emit', wraps=loop.events.emit) as emitted, \
                patch.object(loop.events, '_deliver', side_effect=deliver), \
                patch.object(Workspace, 'prepare', workspace):
            self.assertTrue(self.run_agent(loop, handoff=2))
            loop.events.close()
        lease = loop.coordinator.history(1)[0]
        base = dict(repository='org/project', run=lease['run'], agent='worker', assignment=1,
                    kind='issue', url='https://github.com/org/project/issues/1', attempt=1, mode=None)
        self.assertEqual(delivered, [base | {'event': 'started'}, base | dict(
            event='outcome', status='success', outcome='done', summary='Completed', actions=[], options=[],
            handoff=2, labels_added=['needs-review'], labels_removed=['needs-changes', 'old', 'ready'])])

    def test_retry_and_blocked_reports_preserve_asks_without_transition(self):
        for status in ('retry', 'blocked'):
            with self.subTest(status=status):
                self.github = FakeGitHub(issue())
                loop, delivered, _ = self.loop()
                self.run_agent(loop, status=status, summary='Waiting',
                               action=['Maintainer: repair access.'], option=['Maintainer: use another account.'])
                loop.events.close()
                event = delivered[-1]
                self.assertEqual(event['status'], status)
                self.assertEqual(event['summary'], 'Waiting')
                self.assertEqual(event['actions'], ['Maintainer: repair access.'])
                self.assertEqual(event['options'], ['Maintainer: use another account.'])
                self.assertIsNone(event['outcome'])
                self.assertIsNone(event['handoff'])
                self.assertEqual(event['labels_added'], [])
                self.assertEqual(event['labels_removed'], [])

    def test_launcher_verdicts_and_rejected_reports_emit_released_result(self):
        for failure, report, draft, status, summary in (
                (None, False, False, 'retry', 'without an explicit GitHub outcome'),
                (RetryableExecutionError('Execution timed out'), True, False, 'retry', 'Execution timed out'),
                (None, True, True, 'blocked', 'still a draft')):
            with self.subTest(summary=summary):
                self.github = FakeGitHub(issue(), pr(labels=(), draft=draft))
                loop, delivered, _ = self.loop()
                self.assertTrue(self.run_agent(loop, failure=failure, report=report, handoff=2 if draft else None))
                loop.events.close()
                self.assertEqual([e['event'] for e in delivered], ['started', 'outcome'])
                event = delivered[-1]
                self.assertEqual(event['status'], status)
                self.assertIn(summary, event['summary'])
                self.assertIsNone(event['outcome'])
                self.assertEqual(event['labels_added'], [])
                self.assertEqual(event['labels_removed'], [])

    def test_conflict_route_sends_started_and_outcome_for_pr(self):
        worker = replace(self.worker, kind='pr', on_conflict='done')
        self.github = FakeGitHub(replace(pr(1, labels=('ready',)), mergeable=False))
        loop, delivered, _ = self.loop(worker=worker)
        with patch('ub_agents.loop.supervise') as process:
            loop.launch(once=True)
        process.assert_not_called()
        self.assertEqual([e['event'] for e in delivered], ['started', 'outcome'])
        for event in delivered:
            self.assertEqual(event['kind'], 'pr')
            self.assertEqual(event['url'], 'https://github.com/org/project/pull/1')
        self.assertEqual(delivered[-1]['outcome'], 'done')

    def test_max_rounds_event_contains_substituted_stop_label(self):
        worker = replace(self.worker, outcomes={'done': {'add': ('needs-review',), 'remove': (), 'max-rounds': 1}})
        loop, delivered, _ = self.loop(worker=worker)
        for _ in range(2):
            self.github.change(1, labels=frozenset({'ready'}))
            self.assertTrue(self.run_agent(loop, action='Maintainer: inspect the item.'))
        loop.events.close()
        self.assertEqual([e['labels_added'] for e in delivered if e['event'] == 'outcome'],
                         [['needs-review'], ['needs-human']])
        self.assertEqual(delivered[-1]['outcome'], 'done')

    def test_recovery_emits_only_its_own_outcome_with_source_transition_and_asks(self):
        for status in ('success', 'retry', 'blocked'):
            with self.subTest(status=status):
                self.github = FakeGitHub(issue(), pr(labels=()))
                loop, delivered, _ = self.loop()
                source = loop.coordinator.claim(loop.plans()[0])
                loop.coordinator.update(source, state='running', started=True)
                loop.coordinator.report(source, status, 'Source summary', handoff=2 if status == 'success' else None,
                                        outcome='done' if status == 'success' else None,
                                        action='Maintainer: inspect the item.', option='Maintainer: retry later.')
                self.now += 61
                with patch('ub_agents.loop.supervise') as process:
                    loop.launch(once=True)
                process.assert_not_called()
                self.assertEqual(len(delivered), 1)
                event = delivered[0]
                recovery = [r for r in loop.coordinator.history(1) if r.get('mode') == 'recovery'][0]
                self.assertEqual((event['event'], event['mode'], event['run'], event['attempt']),
                                 ('outcome', 'recovery', recovery['run'], recovery['attempt']))
                self.assertNotEqual(event['run'], source['run'])
                self.assertEqual(event['status'], status)
                self.assertEqual(event['summary'], recovery['summary'])
                self.assertEqual(event['actions'], ['Maintainer: inspect the item.'])
                self.assertEqual(event['options'], ['Maintainer: retry later.'])
                self.assertEqual(event['labels_added'], ['needs-review'] if status == 'success' else [])
                self.assertEqual(event['outcome'], 'done' if status == 'success' else None)
                self.assertEqual(event['handoff'], 2 if status == 'success' else None)

    def test_withdrawn_claims_emit_nothing(self):
        for reason in ('election', 'approval', 'outside-label'):
            with self.subTest(reason=reason):
                self.github = FakeGitHub(issue())
                self.github.next_id = 10
                loop, delivered, _ = self.loop()
                if reason == 'election':
                    def competing(lease):
                        comment = self.github.store[1][0].copy()
                        comment.update(id=1, body=body(payload(lease) | {'run': 'competing'}))
                        self.github.store[1].append(comment)
                    loop.coordinator.on_claim = competing
                elif reason == 'approval':
                    claim = loop.coordinator.claim
                    def park(*args, **kwargs):
                        lease = claim(*args, **kwargs)
                        self.github.timelines[1] = []
                        return lease
                    self.enterContext(patch.object(loop.coordinator, 'claim', side_effect=park))
                else:
                    self.github.roles['outsider'] = 'read'
                    self.github.content_histories[1] = {'author': {'login': 'outsider'}}
                    self.enterContext(patch.object(self.github, 'add_labels',
                        side_effect=GitHubError('POST', 'labels', 'unavailable')))
                with patch('ub_agents.loop.supervise') as process:
                    loop.launch(once=True)
                process.assert_not_called()
                self.assertEqual(delivered, [])
                lease = next(r for r in loop.coordinator.history(1) if r.get('run') != 'competing')
                self.assertEqual(lease['state'], 'withdrawn')
                self.assertFalse(lease['started'])

    def test_one_subscription_and_duplicate_names_deliver_once(self):
        for subscribed in (('started',), ('outcome',), ('started', 'started')):
            with self.subTest(subscribed=subscribed):
                self.github = FakeGitHub(issue())
                loop, delivered, _ = self.loop(hook=EventsHook(('notify',), subscribed))
                self.run_agent(loop)
                loop.events.close()
                self.assertEqual([e['event'] for e in delivered], list(dict.fromkeys(subscribed)))


class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.lines = []
        self.events = EventHooks(self.lines.append)
        self.addCleanup(self.events.close)
        self.lease = dict(run='owned', agent='worker', assignment=1, assignment_sha=None, attempt=1)

    def cfg(self, script, timeout=2):
        return replace(config(self.root), events=EventsHook((sys.executable, '-c', script), ('started',), timeout))

    def test_real_command_receives_json_stdin_cwd_and_filtered_environment_in_order(self):
        script = ('import json, os, sys; from pathlib import Path; '
                  'event = json.load(sys.stdin); '
                  'event.update(cwd=os.getcwd(), private=[k for k in os.environ if k.startswith("UB_AGENTS_")], '
                  'inherited=os.environ.get("EVENT_TEST_VALUE")); '
                  'print(json.dumps(event), file=Path("received").open("a"))')
        cfg = self.cfg(script)
        with patch.dict(os.environ, {'UB_AGENTS_TEST_SECRET': 'private', 'EVENT_TEST_VALUE': 'inherited'}):
            self.events.emit(cfg, self.lease, 'started')
            self.lease['run'] = 'second'
            self.events.emit(cfg, self.lease, 'started')
            self.events.close()
        rows = [json.loads(line) for line in (self.root / 'received').read_text().splitlines()]
        self.assertEqual([row['run'] for row in rows], ['owned', 'second'])
        for row in rows:
            self.assertEqual(row['cwd'], str(self.root))
            self.assertEqual(row['private'], [])
            self.assertEqual(row['inherited'], 'inherited')
        self.assertEqual(self.lines, [])

    def test_missing_and_nonzero_commands_only_print_notices_and_continue_queue(self):
        cfg = replace(config(self.root), events=EventsHook(('./missing-command',), ('started',), 1))
        self.events.emit(cfg, self.lease, 'started')
        self.events.emit(self.cfg('raise SystemExit(7)'), self.lease, 'started')
        self.events.emit(self.cfg('from pathlib import Path; Path("continued").touch()'), self.lease, 'started')
        self.events.close()
        self.assertTrue((self.root / 'continued').exists())
        self.assertTrue(any('missing-command' in line for line in self.lines), self.lines)
        self.assertTrue(any('exited 7' in line for line in self.lines), self.lines)

    def test_timeout_stops_entire_process_group_and_does_not_block_enqueue(self):
        script = ('import json, os, signal, subprocess, sys, time; from pathlib import Path; '
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                  'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                  'Path("group").write_text(str(os.getpid())); time.sleep(60)')
        cfg = self.cfg(script, timeout=1)
        before = monotonic()
        self.events.emit(cfg, self.lease, 'started')
        self.assertLess(monotonic() - before, 0.5)
        self.events.close()
        group = int((self.root / 'group').read_text())
        self.assertEqual(group_members(group), [])
        self.assertTrue(any('timed out' in line for line in self.lines), self.lines)

    def test_successful_hook_cannot_leave_background_child(self):
        script = ('import os, subprocess, sys; from pathlib import Path; '
                  'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                  'Path("group").write_text(str(os.getpid()))')
        self.events.emit(self.cfg(script), self.lease, 'started')
        self.events.close()
        self.assertEqual(group_members(int((self.root / 'group').read_text())), [])

    def test_shutdown_has_one_budget_for_all_queued_hooks(self):
        cfg = self.cfg('import time; time.sleep(60)', timeout=0.5)
        for _ in range(10):
            self.events.emit(cfg, self.lease, 'started')
        before = monotonic()
        self.events.close()
        self.assertLess(monotonic() - before, 2)
        self.assertFalse(self.events._thread.is_alive())
        self.assertTrue(any('queued event discarded' in line for line in self.lines), self.lines)

    def test_start_failure_and_notice_failure_never_escape_to_loop(self):
        with patch('ub_agents.events.threading.Thread.start', side_effect=RuntimeError('cannot start')):
            self.events.emit(self.cfg('pass'), self.lease, 'started')
        self.assertTrue(any('cannot start' in line for line in self.lines), self.lines)
        self.events.output = lambda line: (_ for _ in ()).throw(OSError('closed output'))
        self.events.emit(self.cfg('raise SystemExit(7)'), self.lease, 'started')
        self.events.close()
