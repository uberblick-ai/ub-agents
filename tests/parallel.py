"""Run the unit suite on every core.

    python -m tests [-j N] [--durations N] [name ...]

Each test runs in one of N worker processes (default: every available core),
so the suite takes about as long as its share per core or its slowest test.
Names are the same dotted names `python -m unittest` takes; without names the
whole suite under tests/ runs. Workers start with the spawn method on every
platform, so Linux runs behave like macOS. The workers import this module, not
tests/__main__.py: spawn cannot import a package's __main__ in a child.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import contextlib
import io
import multiprocessing
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def summary(result, seconds=0.0):
    """Plain data for the parent: test results cannot be pickled."""
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m tests', description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=len(os.sched_getaffinity(0))
                        if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1,
                        help='worker processes (default: available cores)')
    parser.add_argument('--durations', type=int, default=0, metavar='N',
                        help='list the N slowest tests')
    parser.add_argument('names', nargs='*', help='test modules, classes or methods (default: all)')
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(args.names) if args.names else
             loader.discover('tests', top_level_dir=str(ROOT)))
    tests = list(flatten(suite))
    # Modules that failed to import cannot be named again in a worker; their
    # placeholder tests run here and report the import error.
    broken = [test for test in tests if isinstance(test, unittest.loader._FailedTest)]
    names = [test.id() for test in tests if not isinstance(test, unittest.loader._FailedTest)]
    jobs = max(1, min(args.jobs, len(names)))

    started = time.monotonic()
    outcomes = [summary(unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.TestSuite(broken)))
                ] if broken else []
    durations = []
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=jobs, mp_context=context) as pool:
        futures = {pool.submit(run, name): name for name in names}
        try:
            for future in as_completed(futures):
                outcome = future.result()
                outcomes.append(outcome)
                durations.append((outcome['seconds'], futures[future]))
                sys.stderr.write(mark(outcome))
                sys.stderr.flush()
        except KeyboardInterrupt:
            # Interrupted tests may still wait out their own timeouts; stop them.
            for process in list(pool._processes.values()):
                process.kill()
            pool.shutdown(wait=False, cancel_futures=True)
            sys.stderr.write('\nInterrupted\n')
            return 130
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
