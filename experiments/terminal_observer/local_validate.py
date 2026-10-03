"""Offline launcher-local evidence. No runtime CLI, remote logs, or live GitHub."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from textual.widgets import RichLog, Static, Tree
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from ub_agents.coordination import Plan
from tests.support import agent, config, issue

from .demo import View
from .harness import SessionRunner, make_loop, run_session
from .local import LatestFile, LocalObserver, Projection, TapLoop, MAX_ROWS, MAX_RECENT, MAX_SNAPSHOT, fixture_observer
from .logs import Buffer, Tail, MAX_BATCH_ENTRIES, MAX_ENTRIES, MAX_RECORD, MAX_TEXT, READ_BUDGET

ROOT = Path.cwd()
EVIDENCE = ROOT / 'experiments/terminal_observer/evidence'
FINDINGS = {}


def test_session(loop, runner, **options):
    # asyncio treats KeyboardInterrupt as an event-loop interrupt. Capture it
    # at the owned thread boundary so durable cleanup can be asserted.
    try:
        return run_session(loop, runner, **options)
    except KeyboardInterrupt:
        return 'interrupted'


async def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('fixture timed out')
        await asyncio.sleep(.02)


def shape(calls):
    # Wire payload IDs/UUIDs/times legitimately differ; endpoints and order must match.
    return [(c[c.index('--method') + 1], urlsplit(c[c.index('--include') + 1]).path) for c in calls]


class MutationView(View):
    def on_tabbed_content_tab_activated(self, event):
        super().on_tabbed_content_tab_activated(event)
        if self.is_mounted:
            self.defect_transport.request('repos/org/project/issues/1')


class Evidence(unittest.IsolatedAsyncioTestCase):
    async def test_single_launcher_off_on_and_details(self):
        counts = []
        traces = []
        outcomes = []
        for enabled in (False, True):
            with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                folder = Path(directory)
                publisher = LatestFile(folder / 'snapshot.json') if enabled else None
                projection = Projection('org/project', publisher) if enabled else None
                loop, runner, finish = make_loop(folder, projection=projection, seconds=20)
                accepted_gate = __import__('threading').Event()
                task = asyncio.create_task(asyncio.to_thread(test_session, loop, runner, pause_after_report=accepted_gate))
                detail_runner = SessionRunner()
                detail_transport = GitHub('org/project', runner=detail_runner)
                observer = LocalObserver(folder / 'snapshot.json', detail_reader=lambda row:
                    detail_transport.request(f'repos/org/project/issues/{row["number"]}'))
                try:
                    await wait_for(runner.process_started.is_set)
                    if enabled:
                        await wait_for(lambda: publisher.path.exists() and json.loads(publisher.path.read_text())['current']
                            and json.loads(publisher.path.read_text())['current'].get('lease', {}).get('log_dir'))
                        before = len(runner.calls)
                        app = View(observer)
                        async with app.run_test(size=(110, 32)) as pilot:
                            await pilot.pause(.5)
                            self.assertTrue(app.selected['local_current'])
                            self.assertEqual(app.tail.path, Path(app.selected['lease']['log_dir']) / 'process.log')
                            self.assertTrue(any('tool result id=synthetic-long' in e.text for e in app.tail.buffer.entries))
                            self.assertFalse(projection.pass_complete)
                            self.assertEqual([r['number'] for r in projection.rows.values()], [1])
                            self.assertTrue(observer.detail_meta[1]['complete'])
                            await pilot.press('2', 'o', '3', '1', 'u', 'f', 'r')
                            tree = app.query_one(Tree)
                            current = next(n for n in tree.root.children if str(n.label) == 'Current work')
                            tree.select_node(current.children[0])
                            await pilot.resize_terminal(72, 24)
                            await pilot.pause(.3)
                            self.assertEqual(len(runner.calls) - before, 0)
                            self.assertEqual(detail_runner.calls, [])  # loaded details never fetch
                            app.raw = False; app.seen = -1; app.show_log()
                            await pilot.pause(.2)
                            app.query_one(RichLog).scroll_home(animate=False)
                            await pilot.pause(.2)
                            app.save_screenshot('local-live-narrow.svg', path=str(EVIDENCE))
                            await pilot.resize_terminal(110, 32)
                            await pilot.press('1', 'u')
                            app.raw = False; app.seen = -1; app.show_log()
                            await pilot.pause(.2)
                            app.query_one(RichLog).scroll_home(animate=False)
                            await pilot.pause(.2)
                            app.save_screenshot('local-live-claude.svg', path=str(EVIDENCE))
                            # Deliberately elide an already collected field in a fixture
                            # projection to exercise the allowed missing-detail branch.
                            observer.details.pop(1)
                            observer.detail_meta[1] = {'complete': False, 'observed_at': time.time(), 'source': 'fixture elision'}
                            gate = __import__('threading').Event()
                            reader = observer.detail_reader
                            def delayed(row):
                                gate.wait(5)
                                return reader(row)
                            observer.detail_reader = delayed
                            await pilot.press('2', 'o')
                            await wait_for(lambda: 1 in observer.loading)
                            self.assertIn('Loading', str(app.query_one('#issue_text', Static).render()))
                            app.save_screenshot('local-detail-loading.svg', path=str(EVIDENCE))
                            # Worker and view keep responding while the optional read waits.
                            await pilot.press('3', '1')
                            self.assertFalse(runner.finished.is_set())
                            gate.set()
                            await wait_for(lambda: 1 not in observer.loading)
                            await pilot.press('2', 'o', '3', '1', '2', 'o')
                            await pilot.pause(.2)
                            self.assertEqual(len(detail_runner.calls), 1)
                            self.assertEqual(shape(detail_runner.calls), [('GET', 'repos/org/project/issues/1')])
                            app.save_screenshot('local-detail-cached.svg', path=str(EVIDENCE))
                            finish.touch()
                            await wait_for(runner.reported.is_set)
                            await wait_for(lambda: json.loads(publisher.path.read_text())['current']['outcome'] is not None)
                            observer.refresh(); app.populate()
                            self.assertFalse(app.selected['outcome']['accepted'])
                            await pilot.press('3')
                            app.save_screenshot('local-unaccepted.svg', path=str(EVIDENCE))
                            accepted_gate.set()
                            await task
                            await wait_for(lambda: json.loads(publisher.path.read_text())['recent'])
                            observer.refresh(); app.populate()
                            recent = next(n for n in tree.root.children if str(n.label) == 'Local recent activity')
                            tree.select_node(recent.children[0]); await pilot.pause(.2)
                            self.assertTrue(app.selected['outcome']['accepted'])
                            self.assertTrue(app.selected['outcome']['transition_complete'])
                            app.save_screenshot('local-accepted.svg', path=str(EVIDENCE))
                            await pilot.press('q')
                    else:
                        finish.touch()
                        accepted_gate.set()
                        await task
                    history = loop.coordinator.history(1)
                    outcomes.append([(r['kind'], r.get('state'), r.get('result'), r.get('accepted')) for r in history])
                    counts.append(len(runner.calls))
                    traces.append(shape(runner.calls))
                    if enabled:
                        self.assertEqual(len(projection.recent), 1)
                        self.assertIsNone(projection.current)
                        self.assertIsNone(projection.error)
                        self.assertLess(publisher.path.stat().st_size, MAX_SNAPSHOT)
                finally:
                    accepted_gate.set(); finish.touch(); loop.interrupt_event.set()
                    await task
                    if publisher:
                        publisher.close()
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(outcomes[0], outcomes[1])
        FINDINGS['single_launcher'] = {'source': 'actual Loop.tick/claim/supervise/finalize/release; recording GitHub; owned Python worker',
            'ui_off_on_transport_calls': counts, 'wire_method_endpoint_order_identical': True,
            'navigation_startup_timer_tail_resize_extra_worker_calls': 0,
            'observed_plan_numbers': [1], 'remaining_29_triggered_plans_not_evaluated': True,
            'explicit_missing_detail_calls': 1, 'revisit_loaded_cached_detail_calls': 0,
            'loading_does_not_block_worker_or_navigation': True,
            'unaccepted_report_then_accepted_transition_distinguished': True,
            'production_runtime_integration': 'unverified'}

    async def test_navigation_audit_rejects_defect(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.write_text('recorded output\n')
            runner = SessionRunner(); transport = GitHub('org/project', runner=runner)
            app = MutationView(fixture_observer(ROOT, path))
            app.defect_transport = transport
            async with app.run_test() as pilot:
                await pilot.pause(.2)
                before = len(runner.calls)
                await pilot.press('2', '3', '1'); await pilot.pause(.2)
                extra = len(runner.calls) - before
                with self.assertRaises(AssertionError):
                    self.assertEqual(extra, 0)
                await pilot.press('q')
        FINDINGS['negative_control'] = {'navigation_read_defect_calls': extra,
            'zero_background_call_assertion_rejects_mutation': True}

    async def test_detail_errors_rate_limit_no_retry(self):
        runner = SessionRunner(); transport = GitHub('org/project', runner=runner)
        projection = Projection('org/project')
        role = agent(ROOT)
        for n in (1, 2, 3):
            projection.observe(Plan(issue(n), role, None, 'ready', 'fixture', 1))
        observer = LocalObserver(projection=projection, detail_reader=lambda row:
            transport.request(f'repos/org/project/issues/{row["number"]}'))
        observer.refresh()
        for meta in observer.detail_meta.values():
            meta['complete'] = False
        runner.next_detail_error = ('HTTP/2 429 Too Many Requests\nRetry-After: 60\n\n'
                                    '{"message":"secondary rate limit (fixture)"}')
        observer.open_details(observer.rows[0])
        self.assertGreater(observer.detail_wait_until, time.time())
        self.assertIn('error', observer.detail_meta[1])
        calls = len(runner.calls)
        for row in observer.rows:
            observer.open_details(row)
        self.assertEqual(len(runner.calls), calls)
        observer.detail_wait_until = 0  # fixture time advancement; still no automatic retry
        runner.next_detail_error = 'HTTP/2 500 Internal Server Error\n\n{"message":"fixture outage"}'
        observer.open_details(observer.rows[1])
        observer.open_details(observer.rows[1])
        self.assertEqual(len(runner.calls), 2)
        for _ in range(20):
            observer.refresh()
        self.assertEqual(len(runner.calls), 2)
        # New ordinary observations may evict the observed-detail cache, but
        # explicit successes/errors stay cached for the whole bounded session.
        for n in range(4, 80):
            projection.observe(Plan(issue(n), role, None, 'ready', 'fixture', 1))
        observer.refresh()
        observer.open_details({'number': 1})
        observer.open_details({'number': 2})
        self.assertEqual(len(runner.calls), 2)
        capped = LocalObserver(detail_reader=lambda row: transport.request(f'repos/org/project/issues/{row["number"]}'))
        cap_before = len(runner.calls)
        for number in range(1, 22):
            capped.open_details({'number': number})
        self.assertEqual(len(runner.calls) - cap_before, 20)
        self.assertIn('exhausted', capped.detail_state({'number': 21}))
        capped.open_details({'number': 1})
        self.assertEqual(len(runner.calls) - cap_before, 20)
        FINDINGS['detail_errors'] = {'rate_limit_response_requests': 1, 'during_cooldown_extra_requests': 0,
            'ordinary_error_requests': 1, 'automatic_retry_requests': 0, 'failure_cached_for_bounded_session': True, 'session_allowance_requests': 20, 'request_after_allowance': 0}

    async def test_bounds_flood_and_responsiveness(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.touch()
            app = View(fixture_observer(ROOT, path))
            ticks = []
            original = app.tick
            def timed():
                start = time.perf_counter(); original(); ticks.append(time.perf_counter() - start)
            app.tick = timed
            tracemalloc.start()
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.2)
                baseline, _ = tracemalloc.get_traced_memory()
                # 2MiB: real bytes, synthetic human lines; small enough for this check.
                block = b'command result: ' + b'x' * 70 + b'\n'
                raw = block * 24000
                with path.open('ab') as stream:
                    stream.write(raw)
                del raw
                heartbeats = []
                async def heartbeat():
                    previous = time.monotonic()
                    for _ in range(100):
                        await asyncio.sleep(.02)
                        now = time.monotonic(); heartbeats.append(now - previous); previous = now
                beat = asyncio.create_task(heartbeat())
                start = time.monotonic()
                key_delays = []
                sent = [None]
                tab_action = app.action_tab
                def measured_tab(tab):
                    if sent[0] is not None:
                        key_delays.append(time.monotonic() - sent[0])
                    tab_action(tab)
                app.action_tab = measured_tab
                await pilot.press('pageup')
                for key in ('2', '3', '1'):
                    sent[0] = time.monotonic()
                    await pilot.press(key)
                sent[0] = None
                await pilot.press('f')
                await pilot.resize_terminal(72, 24)
                await pilot.pause(.3)
                navigation_seconds = time.monotonic() - start
                self.assertTrue(app.follow)
                self.assertGreater(app.tail.offset, 0)
                self.assertLessEqual(len(app.tail.buffer.entries), MAX_ENTRIES)
                self.assertLess(len(app.tail.buffer.pending), MAX_RECORD + READ_BUDGET)
                self.assertLessEqual(len(app.query_one(RichLog).lines), 400)
                await beat
                retained, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                app.save_screenshot('local-flood-narrow.svg', path=str(EVIDENCE))
                self.assertLess(max(ticks), 1.0, 'one bounded UI callback froze for >1s')
                self.assertLess(max(heartbeats), 1.0, 'event loop frozen for >1s')
                self.assertGreater(app.tail.buffer.discarded, 0)
                lag = path.stat().st_size - app.tail.offset + len(app.tail.buffer.pending)
                app.tail.buffer.reset_record()  # test moves to controlled append after flood
                app.tail.offset = path.stat().st_size
                await pilot.resize_terminal(110, 32)
                await pilot.pause(.2)
                await pilot.press('pageup'); await pilot.pause(.2)
                paused = app.query_one(RichLog).scroll_y
                with path.open('ab') as stream:
                    stream.write(b'new data while paused\n')
                await pilot.pause(.2)
                self.assertEqual(app.query_one(RichLog).scroll_y, paused)
                app.save_screenshot('local-paused.svg', path=str(EVIDENCE))
                await pilot.press('f'); await pilot.pause(.2)
                self.assertEqual(app.query_one(RichLog).scroll_y, app.query_one(RichLog).max_scroll_y)
                await pilot.press('q')
            FINDINGS['flood'] = {'source': 'headless Textual pilot; synthetic 24000 human lines',
                'raw_bytes': len(block) * 24000, 'tick_count': len(ticks), 'max_tick_seconds': max(ticks),
                'median_tick_seconds': statistics.median(ticks), 'max_20ms_heartbeat_gap_seconds': max(heartbeats),
                'multi_key_navigation_and_resize_seconds': navigation_seconds,
                'tab_key_dispatch_seconds': key_delays,
                'measurement_overhead': 'tracemalloc + asyncio debug + headless pilot idle waits; not terminal latency',
                'traced_retained_growth_bytes': retained - baseline, 'traced_peak_bytes': peak,
                'backlog_bytes_at_checkpoint': lag, 'backlog_is_lossless_in_raw_file': True,
                'paused_scroll_stable_with_append': True, 'follow_resumed': True,
                'sustained_realtime_consumption_above_read_budget': 'disproved; bounded lag, raw fallback',
                'target_terminal_usability': 'unverified'}
        # An adversarial tiny-line burst needs bounded parsing too.
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.write_bytes(b'\n' * READ_BUDGET)
            tail = Tail(path); ticks = 0; largest = 0
            while tail.offset < path.stat().st_size or tail.buffer.ready():
                before = tail.buffer.total
                tail.poll(); ticks += 1
                largest = max(largest, tail.buffer.total - before)
                self.assertLessEqual(tail.buffer.total - before, MAX_BATCH_ENTRIES)
            self.assertEqual(tail.buffer.total, READ_BUDGET)
            self.assertEqual(largest, MAX_BATCH_ENTRIES)
            FINDINGS['tiny_lines'] = {'raw_bytes': READ_BUDGET, 'polls': ticks, 'maximum_records_per_poll': largest}

    async def test_projection_bound_failure_isolation_and_publisher(self):
        role = agent(ROOT)
        projection = Projection('org/project')
        for number in range(100):
            projection.observe(Plan(issue(number + 1), role, None, 'ready', 'fixture', 1))
        self.assertEqual(len(projection.rows), MAX_ROWS)
        self.assertEqual(len(projection.details), MAX_ROWS)
        self.assertEqual(projection.evicted, 36)
        for number in range(25):
            projection.begin(Plan(issue(number + 1), role, None, 'ready', '', 1))
            projection.record(lease={'assignment': number + 1, 'agent': 'worker', 'state': 'released', 'run': str(number), 'result': 'blocked'})
            projection.finish()
        self.assertEqual(len(projection.recent), MAX_RECENT)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            publisher = LatestFile(folder / 'not-created' / 'snapshot.json')
            failed = Projection('org/project', publisher)
            loop, runner, _ = make_loop(folder, projection=failed, seconds=.01)
            try:
                self.assertTrue(await asyncio.to_thread(test_session, loop, runner))
            finally:
                publisher.close()
            self.assertIsNotNone(publisher.error)
            history = loop.coordinator.history(1)
            self.assertTrue(next(r for r in history if r['kind'] == 'outcome')['accepted'])
            # Failed capture callback also cannot veto worker execution.
            loop, runner, _ = make_loop(folder, projection=Projection('org/project'), seconds=.01)
            with patch.object(loop.projection, 'observe', side_effect=ValueError('tap failed')):
                self.assertTrue(await asyncio.to_thread(test_session, loop, runner))
            self.assertIn('tap failed', loop.projection.error)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            publisher = LatestFile(folder / 'snapshot.json')
            slow = Projection('org/project', publisher)
            gate = __import__('threading').Event()
            entered = __import__('threading').Event()
            original_write = Path.write_bytes
            def delayed_write(path, data):
                if path == publisher.path.with_suffix('.pending'):
                    entered.set(); gate.wait(5)
                return original_write(path, data)
            timings = []
            with patch.object(Path, 'write_bytes', delayed_write):
                try:
                    slow.changed()
                    await wait_for(entered.is_set)
                    for number in range(100):
                        item = replace(issue(number + 1), body='synthetic collected context ' * 160)
                        start = time.perf_counter()
                        slow.observe(Plan(item, role, None, 'ready', '', 1))
                        timings.append(time.perf_counter() - start)
                    self.assertGreater(publisher.coalesced, 0)
                    self.assertEqual(publisher.pending.qsize(), 1)
                    self.assertLess(max(timings), .1)
                finally:
                    gate.set(); publisher.close()
            snapshot = json.loads(publisher.path.read_text())
            self.assertEqual(snapshot['generation'], slow.generation)
        FINDINGS['projection'] = {'max_observed_rows': MAX_ROWS, 'max_recent_runs': MAX_RECENT,
            'max_observed_details': MAX_ROWS, 'max_explicit_detail_reads_cached': 20, 'max_detail_chars': 8192, 'file_cap_bytes': MAX_SNAPSHOT,
            'publisher_pending_snapshots': 1, 'coalesced_under_blocked_writer': publisher.coalesced,
            'max_bounded_observe_callback_seconds': max(timings),
            'median_bounded_observe_callback_seconds': statistics.median(timings),
            'slow_disk_does_not_block_callbacks': True, 'publisher_disk_failure_worker_success': True,
            'tap_callback_failure_worker_success': True}

    async def test_drain_interrupt_and_view_closure(self):
        cases = {}
        for enabled in (False, True):
            for mode in ('drain', 'interrupt'):
                with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                    folder = Path(directory)
                    projection = Projection('org/project') if enabled else None
                    loop, runner, finish = make_loop(folder, projection=projection, seconds=20)
                    task = asyncio.create_task(asyncio.to_thread(test_session, loop, runner))
                    try:
                        await wait_for(runner.process_started.is_set)
                        if enabled:
                            app = View(LocalObserver(projection=projection))
                            async with app.run_test() as pilot:
                                await pilot.pause(.2)
                                await pilot.press('q')
                            self.assertFalse(loop.stop_event.is_set())
                            self.assertFalse(loop.interrupt_event.is_set())
                            self.assertFalse(task.done())
                        if mode == 'drain':
                            loop.stop_gracefully()  # same callback used by SIGTERM
                            self.assertTrue(loop.stop_event.is_set())
                            self.assertFalse(loop.interrupt_event.is_set())
                            self.assertFalse(task.done())
                            finish.touch()
                            self.assertTrue(await task)
                        else:
                            loop.interrupt_event.set()  # same event used by SIGINT/SIGHUP
                            self.assertEqual(await task, 'interrupted')
                        history = loop.coordinator.history(1)
                        lease = next(r for r in history if r['kind'] == 'lease')
                        outcome = next(r for r in history if r['kind'] == 'outcome')
                        self.assertEqual(lease['state'], 'released')
                        from ub_agents.execution import group_members
                        self.assertEqual(group_members(lease['process_group']), [])
                        cases[f'{enabled}:{mode}'] = [lease['state'], lease['result'], outcome['accepted']]
                    finally:
                        finish.touch(); loop.interrupt_event.set()
                        try:
                            await task
                        except KeyboardInterrupt:
                            pass
        self.assertEqual(cases['False:drain'], cases['True:drain'])
        self.assertEqual(cases['False:interrupt'], cases['True:interrupt'])
        self.assertTrue(cases['True:drain'][2])
        self.assertFalse(cases['True:interrupt'][2])
        FINDINGS['lifecycle'] = {'source': 'owned Python process + actual Loop callbacks and durable fixture transport',
            'ui_off_on_durable_results': cases, 'view_close_leaves_worker_running': True,
            'owned_groups_empty_after_completion': True, 'actual_os_launcher_signals': 'unverified in this harness; repository suite covers signals',
            'attached_foreground_ui': 'unverified; not prototyped'}

    async def test_current_codex_human_projection(self):
        source = (EVIDENCE / 'codex-production/process.log').read_text()
        text = source.split('BEGIN tool output')[0] + 'single command result BEGIN ' + 'x' * 42000 + ' END\n'
        text += source[source.index('ERROR:'):] + 'ordinary mixed diagnostic\n'
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.write_text(text)
            app = View(fixture_observer(ROOT, path))
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.3)
                await wait_for(lambda: app.tail.offset == path.stat().st_size and not app.tail.buffer.ready())
                self.assertTrue(all(e.kind == 'text' for e in app.tail.buffer.entries))
                long = next(e for e in app.tail.buffer.entries if 'single command result BEGIN' in e.text)
                self.assertIn('display shortened', long.text)
                self.assertIn('END', long.text)
                self.assertLessEqual(len(long.text), MAX_TEXT)
                self.assertTrue(any('ERROR:' in e.text for e in app.tail.buffer.entries))
                self.assertTrue(all(e.capture is None and e.event is None for e in app.tail.buffer.entries))
                app.save_screenshot('local-codex-human.svg', path=str(EVIDENCE))
                await pilot.press('q')
        FINDINGS['codex_current'] = {'source': 'synthetic human text derived from existing production fixture; no CLI probe',
            'raw_bytes': len(text.encode()), 'max_record_bytes': max(map(len, text.encode().splitlines())),
            'compact_lines': True, 'long_line_shortening_visible': True, 'errors_and_ordinary_text_visible': True,
            'producer_timestamps': 'unavailable', 'real_current_format_recording': 'unverified'}

    async def test_separate_local_view_pty(self):
        import fcntl
        import pty
        import select
        import struct
        import subprocess
        import termios
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            publisher = LatestFile(folder / 'snapshot.json')
            projection = Projection('org/project', publisher)
            loop, runner, finish = make_loop(folder, projection=projection, seconds=20)
            task = asyncio.create_task(asyncio.to_thread(test_session, loop, runner))
            modes = {}
            try:
                await wait_for(runner.process_started.is_set)
                await wait_for(publisher.path.exists)
                before = len(runner.calls)
                for mode, key in [('q', b'q'), ('ctrl-c', b'\x03')]:
                    ready = folder / f'ready-{mode}'
                    code = ('from pathlib import Path\nfrom experiments.terminal_observer.demo import View\n'
                            'from experiments.terminal_observer.local import LocalObserver\n'
                            'class ReadyView(View):\n'
                            ' async def on_mount(self):\n'
                            '  await super().on_mount()\n'
                            f'  Path({str(ready)!r}).touch()\n'
                            f'ReadyView(LocalObserver(Path({str(publisher.path)!r}))).run()\n')
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    process = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, stdin=slave,
                        stdout=slave, stderr=slave, env=os.environ | {'TERM': 'xterm-256color'}, start_new_session=True)
                    os.close(slave)
                    def drain():
                        if select.select([master], [], [], 0)[0]:
                            try:
                                os.read(master, 65536)
                            except OSError:
                                pass
                    try:
                        deadline = time.monotonic() + 8
                        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                            drain(); await asyncio.sleep(.02)
                        self.assertTrue(ready.exists(), 'local snapshot view did not mount')
                        await asyncio.sleep(.2)
                        os.write(master, key)
                        deadline = time.monotonic() + 5
                        while process.poll() is None and time.monotonic() < deadline:
                            drain(); await asyncio.sleep(.02)
                        self.assertEqual(process.poll(), 0, 'view did not close successfully')
                        self.assertFalse(task.done())
                        self.assertFalse(loop.stop_event.is_set())
                        self.assertFalse(loop.interrupt_event.is_set())
                        modes[mode] = {'view_exit': process.returncode, 'launcher_worker_still_running': True}
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=5)
                        os.close(master)
                self.assertEqual(len(runner.calls), before)
                finish.touch()
                self.assertTrue(await task)
            finally:
                finish.touch(); loop.interrupt_event.set(); await task; publisher.close()
        FINDINGS['local_pty'] = {'source': 'separate OS process, isolated owned PTY, launcher projection, recording transport',
            'closure_modes': modes, 'additional_worker_transport_calls': 0,
            'actual_operator_terminal_or_herdr': 'unverified'}

    async def test_recorded_replay_and_ownership(self):
        files = {}
        for runtime in ('claude', 'codex'):
            source = EVIDENCE / f'{runtime}-sanitized.jsonl'
            with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                path = Path(directory) / 'process.log'; path.write_bytes(source.read_bytes())
                observer = fixture_observer(ROOT, path)
                app = View(observer)
                async with app.run_test(size=(110, 32)) as pilot:
                    await pilot.pause(.3)
                    self.assertTrue(any('SPIKE97_OBSERVER_OK' in e.text for e in app.tail.buffer.entries))
                    self.assertTrue(all(e.capture is None for e in app.tail.buffer.entries))
                    app.save_screenshot(f'local-{runtime}-recorded.svg', path=str(EVIDENCE))
                    files[runtime] = {'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'records': app.tail.buffer.total, 'provenance': 'curated sanitized first-run real recording; flags in probes.json',
                        'retained_record_shape_matches_current_format': runtime == 'claude', 'original_flags_match_production': False, 'complete_original_stream': False}
                    tree = app.query_one(Tree)
                    ownership = next(n for n in tree.root.children if str(n.label) == 'Observed ownership')
                    tree.select_node(ownership.children[0]); await pilot.pause(.1)
                    self.assertIsNone(app.tail)
                    await pilot.press('q')
        FINDINGS['recordings'] = files


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Evidence)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / 'local-validation.json').write_text(json.dumps(FINDINGS, indent=2) + '\n')
    sys.exit(not result.wasSuccessful())
