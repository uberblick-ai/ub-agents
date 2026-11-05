import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tests.parallel import discover, main, ROOT, supervise
from tests.parallel_worker import process_table, signal_process, stop_process_trees


class ParallelTests(unittest.TestCase):
    def run_runner(self, *names, timeout=2, jobs=1, environment=None, worker_mode=None):
        command = [sys.executable, '-m', 'tests.parallel_fixtures', worker_mode] if worker_mode else [
            sys.executable, '-m', 'tests']
        started = time.monotonic()
        result = subprocess.run(command + ['-j', str(jobs), '--timeout', str(timeout), '--durations', '1', *names],
                                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)
        self.assertLess(time.monotonic() - started, 10)
        return result

    def dispatched_names(self, names, *options):
        empty = {'run': 0, 'failures': [], 'errors': [], 'skipped': 0,
                 'unexpected_successes': [], 'seconds': 0}
        dispatched = []

        def start(_directory, number, events):
            worker = Mock(complete=False, process=Mock(pid=number + 100, poll=lambda: None),
                          closed=False, since=time.monotonic())
            worker.name = None

            def send(kind, **data):
                if kind == 'discover':
                    events.put((worker, {'kind': 'discovered', 'names': names, 'outcome': empty.copy()}))
                elif kind == 'run':
                    dispatched.append(data['name'])
                    events.put((worker, {'kind': 'finished', 'name': data['name'],
                                         'outcome': empty | {'run': 1}}))
                elif kind == 'stop':
                    events.put((worker, {'kind': 'stopped'}))
            worker.send = send
            events.put((worker, {'kind': 'ready'}))
            return worker

        with patch('tests.parallel.Worker.start', side_effect=start), \
                patch('tests.parallel.stop_workers'), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['-j', '2', *options]), 0)
        return dispatched

    def test_shards_partition_every_discovered_test_in_sorted_order(self):
        names, outcome = discover([])
        self.assertFalse(outcome['errors'])
        self.assertTrue(names)
        names.reverse()
        self.assertEqual(self.dispatched_names(names), names)
        for count in (1, 2, 3):
            with self.subTest(count=count):
                shards = [self.dispatched_names(names, '--shard', f'{index}/{count}')
                          for index in range(1, count + 1)]
                self.assertCountEqual([name for shard in shards for name in shard], names)
                for index, shard in enumerate(shards):
                    self.assertEqual(shard, sorted(names)[index::count])
        self.assertEqual(self.dispatched_names(names[:1], '--shard', '2/2'), [])

    def test_named_tests_are_sharded_in_sorted_order(self):
        names = [f'tests.parallel_fixtures.Results.test_{name}' for name in ('stderr_noise', 'skip', 'pass')]
        for shard, selected in (('1/2', [names[2], names[0]]), ('2/2', [names[1]]), ('4/4', [])):
            with self.subTest(shard=shard):
                result = self.run_runner('--shard', shard, '--durations', '10', *names)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'Ran {len(selected)} tests', result.stderr)
                for name in names:
                    self.assertEqual(name in result.stderr, name in selected)

    def test_invalid_shards_fail_before_starting_a_worker(self):
        for value in ('0/2', '3/2', 'x', '1/0', '-1/2', '1/-2', '1/2/3', '1/', '/2'):
            with self.subTest(value=value), patch('tests.parallel.Worker.start') as start, \
                    contextlib.redirect_stderr(io.StringIO()) as output, \
                    self.assertRaises(SystemExit) as raised:
                main(['--shard', value])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('usage:', output.getvalue())
            start.assert_not_called()

    def assert_stopped(self, pids):
        # Orphan zombies can briefly await init's reaper; they hold no pipes and
        # cannot run. Inspect live processes instead of kill(pid, 0) for these.
        result = subprocess.run(['ps', '-axo', 'pid=,stat='], capture_output=True, text=True,
                                timeout=3, check=True)
        live = {int(pid) for pid, status in (line.split() for line in result.stdout.splitlines())
                if not status.startswith('Z')}
        self.assertFalse(set(pids) & live, f'Worker descendants still alive: {set(pids) & live}')

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('Timed out waiting for runner fixture')
            time.sleep(.01)

    def kill_runner(self, names, phases, sig, *, group=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = subprocess.Popen(
                [sys.executable, '-m', 'tests', '-j', str(len(names)), '--timeout', '30', *names],
                cwd=ROOT, env=os.environ.copy() | {'TEST_RUNNER_RECORD': directory},
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pids = []
            try:
                markers = [root / phase / marker for phase, marker in phases.items()]
                records = [root / phase / f'children-{detached}'
                           for phase in phases for detached in (False, True)]
                self.wait_for(lambda: all(marker.exists() for marker in markers)
                              and all(record.exists() and record.stat().st_size for record in records))
                for phase in phases:
                    pids.append(int((root / phase / 'worker').read_text()))
                    for detached in (False, True):
                        pids.extend(json.loads((root / phase / f'children-{detached}').read_text()))
                signal_process(runner.pid, sig, group=group)
                runner.communicate(timeout=5)
                self.assertEqual(runner.returncode, -sig)
                # The EOF watcher cleans asynchronously after the runner dies.
                self.wait_for(lambda: not set(pids) & {
                    pid for pid, _parent, _group, state in process_table() if not state.startswith('Z')})
                self.assert_stopped(pids)
            finally:
                # Keep even a failing regression from leaking its fixtures.
                roots = [runner.pid]
                for record in root.glob('*/worker'):
                    roots.append(int(record.read_text()))
                for record in root.glob('*/children-*'):
                    if record.stat().st_size:
                        roots.extend(json.loads(record.read_text()))
                stop_process_trees(roots)
                runner.communicate(timeout=5)

    def test_killing_runner_group_stops_finished_and_running_worker_trees(self):
        for sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.kill_runner(
                    ['tests.parallel_fixtures.Hanging.test_parent_finished',
                     'tests.parallel_fixtures.Hanging.test_parent_running'],
                    {'finished': 'stopped', 'running': 'worker'}, sig)

    def test_killing_only_runner_stops_workers_during_interpreter_shutdown(self):
        self.kill_runner(['tests.parallel_fixtures.Hanging.test_parent_shutdown'],
                         {'shutdown': 'shutting-down'}, signal.SIGKILL, group=False)

    def test_timeout_reports_test_all_threads_and_stops_detached_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy() | {'TEST_RUNNER_RECORD': directory}
            result = self.run_runner('tests.parallel_fixtures.Results.test_failure',
                                     'tests.parallel_fixtures.Hanging.test_hang', environment=environment)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('TIMEOUT: tests.parallel_fixtures.Hanging.test_hang', result.stderr)
            self.assertIn('waiting_thread', result.stderr)
            self.assertIn('Current thread', result.stderr)
            self.assertIn('Thread 0x', result.stderr)
            self.assertIn('fixture failure', result.stderr)
            self.assertIn('buffered failure output', result.stderr)
            self.assertIn('Ran 2 tests', result.stderr)
            self.assertIn('failures=1, errors=1', result.stderr)
            root = Path(directory)
            pids = [int((root / 'worker').read_text())]
            for detached in (False, True):
                pids.extend(json.loads((root / f'children-{detached}').read_text()))
            self.assert_stopped(pids)

    def test_shutdown_dumps_all_workers(self):
        result = self.run_runner('tests.parallel_fixtures.Hanging.test_shutdown',
                                 'tests.parallel_fixtures.Hanging.test_shutdown', jobs=2)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('RUNNER TIMEOUT: worker shutdown', result.stderr)
        self.assertEqual(result.stderr.count(' (worker shutdown):'), 2)

    def test_timeout_diagnostics_exclude_stderr_from_earlier_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_runner('tests.parallel_fixtures.Results.test_stderr_noise',
                                     'tests.parallel_fixtures.Hanging.test_hang',
                                     environment=os.environ.copy() | {'TEST_RUNNER_RECORD': directory})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('waiting_thread', result.stderr)
            self.assertNotIn('stderr from an earlier test', result.stderr)

    def test_discovery_stall_is_bounded_without_naming_a_test(self):
        result = self.run_runner('tests.parallel_fixtures.discovery_hang')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('RUNNER TIMEOUT: test discovery', result.stderr)
        self.assertIn('no test in flight', result.stderr)
        self.assertIn('__getattr__', result.stderr)

    def test_startup_stall_is_bounded_without_naming_a_test(self):
        result = self.run_runner(timeout=.5, worker_mode='startup')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('RUNNER TIMEOUT: worker startup', result.stderr)
        self.assertIn('no test in flight', result.stderr)

    def test_blocked_protocol_reads_and_writes_do_not_block_the_deadline(self):
        for mode, phase in (('blocked_write', 'test discovery'), ('partial_result', 'worker startup')):
            with self.subTest(mode=mode):
                # Exceed a pipe buffer to exercise a blocked command writer.
                result = self.run_runner('x' * 100_000, worker_mode=mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f'RUNNER TIMEOUT: {phase}', result.stderr)

    def test_shutdown_stall_preserves_finished_results_and_stacks(self):
        result = self.run_runner('tests.parallel_fixtures.Hanging.test_shutdown')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('RUNNER TIMEOUT: worker shutdown', result.stderr)
        self.assertIn('waiting_thread', result.stderr)
        self.assertIn('Ran 1 tests', result.stderr)

    def test_successful_shutdown_also_stops_detached_children(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_runner('tests.parallel_fixtures.Hanging.test_children_finish',
                                     environment=os.environ.copy() | {'TEST_RUNNER_RECORD': directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(directory)
            pids = [int((root / 'worker').read_text())]
            for detached in (False, True):
                pids.extend(json.loads((root / f'children-{detached}').read_text()))
            self.assert_stopped(pids)

    def test_every_test_over_the_deadline_is_reported_including_late_results(self):
        empty = {'run': 0, 'failures': [], 'errors': [], 'skipped': 0,
                 'unexpected_successes': [], 'seconds': 0}
        for late_result in (False, True):
            with self.subTest(late_result=late_result):
                def start(_directory, number, events):
                    worker = Mock(name=None, since=3, phase='worker startup', complete=False,
                                  closed=False, process=Mock(pid=number + 100, poll=lambda: None))
                    # Mock's name constructor argument names the mock itself.
                    worker.name = None

                    def send(kind, **data):
                        worker.phase = kind
                        if kind == 'discover':
                            events.put((worker, {'kind': 'discovered', 'names': ['first', 'second'],
                                                 'outcome': empty.copy()}))
                        elif kind == 'run':
                            events.put((worker, {'kind': 'started', 'name': data['name'], 'since': 0}))
                            if late_result and data['name'] == 'first':
                                outcome = empty.copy() | {'run': 1, 'seconds': 2.5, 'errors': []}
                                events.put((worker, {'kind': 'finished', 'name': data['name'],
                                                     'outcome': outcome}))
                    worker.send = send
                    events.put((worker, {'kind': 'ready'}))
                    return worker

                outcomes, durations = [], []
                with patch('tests.parallel.Worker.start', side_effect=start), \
                        patch('tests.parallel.time.monotonic', return_value=3), \
                        patch('tests.parallel.dump_workers') as dump, patch('tests.parallel.stop_workers') as stop, \
                        contextlib.redirect_stderr(io.StringIO()):
                    supervise([], 2, 2, outcomes, durations, Path('.'))
                errors = [name for outcome in outcomes for name, _message in outcome['errors']]
                self.assertCountEqual(errors, ['first', 'second'])
                dump.assert_called_once()
                stop.assert_called_once()

    def test_finished_results_keep_existing_reporting(self):
        result = self.run_runner(*[f'tests.parallel_fixtures.Results.test_{name}' for name in
                                  ('pass', 'failure', 'error', 'skip', 'unexpected_success')])
        self.assertEqual(result.returncode, 1, result.stderr)
        for message in ('fixture failure', 'fixture error', 'UNEXPECTED SUCCESS:', 'Slowest tests:',
                        'Ran 5 tests', 'failures=1, errors=1, skipped=1, unexpected successes=1'):
            self.assertIn(message, result.stderr)
        result = self.run_runner('tests.parallel_fixtures.Results.test_pass')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK', result.stderr)

    def test_late_finished_test_is_counted_once_and_still_fails(self):
        result = self.run_runner('tests.parallel_fixtures.Results.test_late_result')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('TIMEOUT: tests.parallel_fixtures.Results.test_late_result', result.stderr)
        self.assertIn('Ran 1 tests', result.stderr)
        self.assertIn('FAILED (errors=1)', result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('coverage'), 'coverage is only installed in the coverage CI job')
    def test_coverage_retains_code_executed_in_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Isolate nested measurement from a surrounding coverage run.
            environment = {key: value for key, value in os.environ.items() if not key.startswith('COVERAGE_')}
            environment['COVERAGE_FILE'] = str(root / '.coverage')

            def coverage(*arguments):
                result = subprocess.run([sys.executable, '-m', 'coverage', *arguments], cwd=ROOT,
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            coverage('run', '-m', 'tests', '-j', '2', 'tests.test_github_http.GitHubHTTPTests')
            coverage('combine')
            report = root / 'coverage.json'
            coverage('json', '-o', str(report))
            data = json.loads(report.read_text())['files']['src/ub_agents/github_http.py']
            # Importing the module during discovery cannot cover this operation.
            self.assertGreater(data['functions']['Transport.send']['summary']['covered_lines'], 0)

    def test_failed_import_and_worker_crash_fail_the_run(self):
        result = self.run_runner('tests.runner_missing_fixture')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('ModuleNotFoundError', result.stderr)
        result = self.run_runner('tests.parallel_fixtures.Hanging.test_crash')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('exited with status 7', result.stderr)
        self.assertIn('tests.parallel_fixtures.Hanging.test_crash', result.stderr)

    def test_help_and_invalid_timeouts(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('--timeout SECONDS', output.getvalue())
        self.assertIn('--shard K/N', output.getvalue())
        self.assertIn('default: 300s', output.getvalue())
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                main(['--timeout', value])
            self.assertEqual(raised.exception.code, 2)

    def test_zombie_only_groups_are_gone_but_live_permission_failures_are_errors(self):
        with patch('tests.parallel_worker.os.killpg', side_effect=PermissionError), \
                patch('tests.parallel_worker.process_table', return_value=[(123, 1, 123, 'Z')]):
            signal_process(123, signal.SIGKILL, group=True)
        with patch('tests.parallel_worker.os.killpg', side_effect=PermissionError), \
                patch('tests.parallel_worker.process_table', return_value=[(123, 1, 123, 'S')]), \
                self.assertRaises(PermissionError):
            signal_process(123, signal.SIGKILL, group=True)
        with patch('tests.parallel_worker.os.kill', side_effect=PermissionError), \
                self.assertRaises(PermissionError):
            signal_process(123, signal.SIGKILL)
