"""Run the unit suite on every core.

    python -m tests [-j N] [--durations N] [--timeout SECONDS] [name ...]

Each test runs in one of N worker processes (default: every available core),
so the suite takes about as long as its share per core or its slowest test.
Names are the same dotted names `python -m unittest` takes; without names the
whole suite under tests/ runs. Each test has a 300-second limit, configurable
with --timeout SECONDS. Worker startup, discovery and shutdown use the same
limit when no test is in flight. A timeout prints worker thread stacks, stops
workers and their child processes, and fails the run. Workers start in fresh
Python interpreters on every platform, so Linux runs behave like macOS.
"""

import argparse
import contextlib
import io
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest

from tests.parallel_worker import Worker, dump_workers, stop_workers

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 300


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def summary(result, seconds=0.0):
    """Plain data for the parent: test results cannot cross the worker protocol."""
    return {
        'run': result.testsRun,
        'failures': [(str(test), text) for test, text in result.failures],
        'errors': [(str(test), text) for test, text in result.errors],
        'skipped': len(result.skipped),
        'unexpected_successes': [str(test) for test in result.unexpectedSuccesses],
        'seconds': seconds,
    }


def run(name):
    """Run one test in a worker; output written by the test is kept with failures."""
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromName(name)
    # The buffered result also echoes a failing test's output to the stdout and
    # stderr it started with; the failure report already carries that output.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, buffer=True).run(suite)
    return summary(result, time.monotonic() - started)


def mark(outcome):
    if outcome['errors']:
        return 'E'
    if outcome['failures'] or outcome['unexpected_successes']:
        return 'F'
    if outcome['skipped']:
        return 's'
    return '.'


def discover(requested):
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(requested) if requested else
             loader.discover('tests', top_level_dir=str(ROOT)))
    tests = list(flatten(suite))
    # Modules that failed to import cannot be named again in a worker; their
    # placeholder tests report the import error during discovery.
    broken = [test for test in tests if isinstance(test, unittest.loader._FailedTest)]
    names = [test.id() for test in tests if not isinstance(test, unittest.loader._FailedTest)]
    outcome = summary(unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.TestSuite(broken)))
    return names, outcome


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('timeout must be a finite positive number of seconds')
    return seconds


def supervise(requested, jobs, timeout, outcomes, durations, directory):
    events = queue.Queue()
    workers = []
    pending = None
    aborting = False
    timeout_outcomes = {}

    def report_timeout(worker, now):
        if worker.name:
            message = f'TIMEOUT: {worker.name} exceeded {timeout:g}s'
        else:
            message = f'RUNNER TIMEOUT: {worker.phase} exceeded {timeout:g}s (no test in flight)'
        sys.stderr.write(message + '\n')
        outcome = {'run': int(worker.name is not None), 'failures': [],
                   'errors': [(worker.name or worker.phase, message)], 'skipped': 0,
                   'unexpected_successes': [], 'seconds': now - worker.since}
        outcomes.append(outcome)
        if worker.name:
            timeout_outcomes[worker.process.pid] = outcome

    def dispatch(worker):
        if aborting:
            return
        if pending:
            worker.send('run', name=pending.pop(), timeout=timeout)
        else:
            worker.send('stop')

    def accept(worker, event):
        nonlocal pending, jobs, aborting
        kind = event['kind']
        if kind == 'ready':
            worker.ready = True
            if pending is None:
                worker.send('discover', names=requested)
            else:
                dispatch(worker)
        elif kind == 'discovered':
            if event['outcome']['run']:
                outcomes.append(event['outcome'])
            pending = list(reversed(event['names']))
            jobs = max(1, min(jobs, len(pending)))
            for number in range(1, jobs):
                workers.append(Worker.start(directory, number, events))
            dispatch(worker)
        elif kind == 'started':
            worker.name = event['name']
            worker.since = event['since']
            worker.phase = 'test execution'
        elif kind == 'finished':
            outcome = event['outcome']
            previous = timeout_outcomes.pop(worker.process.pid, None)
            if previous is not None:
                # The test may finish during the diagnostic grace period. Keep
                # its actual result and timeout error, counting the attempt once.
                outcomes.remove(previous)
                outcome['errors'].extend(previous['errors'])
            elif outcome['seconds'] >= timeout:
                message = f"TIMEOUT: {event['name']} exceeded {timeout:g}s"
                sys.stderr.write('\n' + message + '\n')
                outcome['errors'].append((event['name'], message))
                aborting = True
                worker.name = None
                worker.phase = 'test completed after timeout'
                worker.since = time.monotonic()
            worker.name = None
            outcomes.append(outcome)
            durations.append((outcome['seconds'], event['name']))
            sys.stderr.write(mark(outcome))
            sys.stderr.flush()
            dispatch(worker)
        elif kind == 'closed':
            worker.closed = True
        elif kind == 'stopped':
            worker.complete = True
        else:
            raise RuntimeError(f"Worker protocol error: {event.get('error', kind)}")

    def drain():
        # Keep checking deadlines even if fast tests continually refill the queue.
        for _ in range(100):
            try:
                worker, event = events.get_nowait()
            except queue.Empty:
                break
            accept(worker, event)

    try:
        workers.append(Worker.start(directory, 0, events))
        while True:
            try:
                worker, event = events.get(timeout=.05)
            except queue.Empty:
                pass
            else:
                accept(worker, event)
            drain()
            active = [worker for worker in workers if not worker.complete]
            for worker in workers:
                code = worker.process.poll()
                if worker.closed and code is not None and not worker.complete:
                    raise RuntimeError(f'Worker {worker.process.pid} exited with status {code} '
                                       f'during {worker.name or worker.phase}')
            if not active:
                break
            now = time.monotonic()
            timed_out = [worker for worker in active if now - worker.since >= timeout]
            if aborting or timed_out:
                aborting = True
                sys.stderr.write('\n')
                for worker in timed_out:
                    report_timeout(worker, now)
                dump_workers(workers)
                break
    except (OSError, RuntimeError):
        dump_workers(workers)
        raise
    finally:
        stopped_at = time.monotonic()
        stop_workers(workers)
        aborting = True
        # Preserve results already written before cleanup, without dispatching
        # more work after a timeout or interruption.
        while True:
            try:
                _worker, event = events.get_nowait()
            except queue.Empty:
                break
            if event['kind'] == 'finished':
                accept(_worker, event)
            elif event['kind'] == 'started':
                accept(_worker, event)
            elif event['kind'] == 'discovered' and event['outcome']['run']:
                outcomes.append(event['outcome'])
        for worker in workers:
            if (worker.name and worker.process.pid not in timeout_outcomes
                    and stopped_at - worker.since >= timeout):
                report_timeout(worker, stopped_at)
    return jobs


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m tests', description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=len(os.sched_getaffinity(0))
                        if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1,
                        help='worker processes (default: available cores)')
    parser.add_argument('--durations', type=int, default=0, metavar='N',
                        help='list the N slowest tests')
    parser.add_argument('--timeout', type=positive_seconds, default=DEFAULT_TIMEOUT, metavar='SECONDS',
                        help='per-test time limit; also bounds idle worker startup, discovery and shutdown '
                             f'(default: {DEFAULT_TIMEOUT}s)')
    parser.add_argument('names', nargs='*', help='test modules, classes or methods (default: all)')
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    started = time.monotonic()
    outcomes = []
    durations = []
    jobs = max(1, args.jobs)
    try:
        with tempfile.TemporaryDirectory(prefix='test-workers-') as directory:
            jobs = supervise(args.names, jobs, args.timeout, outcomes, durations, Path(directory))
    except KeyboardInterrupt:
        sys.stderr.write('\nInterrupted\n')
        return 130
    except (OSError, RuntimeError) as error:
        outcomes.append({'run': 0, 'failures': [], 'errors': [('test runner', str(error))],
                         'skipped': 0, 'unexpected_successes': []})
    elapsed = time.monotonic() - started

    sys.stderr.write('\n')
    for kind in ('errors', 'failures'):
        for outcome in outcomes:
            for test, text in outcome[kind]:
                sys.stderr.write('=' * 70 + f'\n{kind[:-1].upper() if kind == "errors" else "FAIL"}: {test}\n'
                                 + '-' * 70 + f'\n{text}\n')
    for outcome in outcomes:
        for test in outcome['unexpected_successes']:
            sys.stderr.write(f'UNEXPECTED SUCCESS: {test}\n')
    if args.durations:
        sys.stderr.write('\nSlowest tests:\n')
        for seconds, name in sorted(durations, reverse=True)[:args.durations]:
            sys.stderr.write(f'{seconds:7.2f}s {name}\n')

    total = sum(outcome['run'] for outcome in outcomes)
    failures = sum(len(outcome['failures']) for outcome in outcomes)
    errors = sum(len(outcome['errors']) for outcome in outcomes)
    skipped = sum(outcome['skipped'] for outcome in outcomes)
    unexpected = sum(len(outcome['unexpected_successes']) for outcome in outcomes)
    sys.stderr.write('-' * 70 + f'\nRan {total} tests in {elapsed:.3f}s ({jobs} workers)\n\n')
    details = [f'{label}={count}' for label, count in
               (('failures', failures), ('errors', errors), ('skipped', skipped),
                ('unexpected successes', unexpected)) if count]
    ok = not (failures or errors or unexpected)
    sys.stderr.write(('OK' if ok else 'FAILED') + (f' ({", ".join(details)})' if details else '') + '\n')
    return 0 if ok else 1
