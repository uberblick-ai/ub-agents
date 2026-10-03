"""Reproducible evidence; separate from the package's production test suite."""
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from textual.widgets import RichLog, TabbedContent, Tree
from ub_agents.cli import status_rows
from ub_agents.execution import supervise, group_members
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from tests.support import DiscoveryCostRunner, agent, config

from .demo import View
from .logs import Buffer, Tail, MAX_ENTRIES, MAX_RECORD, READ_BUDGET
from .model import Observer, GROUPS, group

ROOT = Path.cwd()
EVIDENCE = ROOT / 'experiments/terminal_observer/evidence'
FINDINGS = {}


class Evidence(unittest.IsolatedAsyncioTestCase):
    async def test_formats_bounds_and_capture(self):
        buffer = Buffer()
        record = json.dumps({'type': 'stream_event', 'timestamp': '2026-10-01T10:00:00Z',
                             'event': {'delta': {'text': 'Claude café'}}}).encode() + b'\n'
        buffer.feed(record[:20], 'capture-A')
        self.assertEqual(buffer.total, 0)
        self.assertIsNotNone(buffer.preview())
        buffer.feed(record[20:], 'capture-B')
        entry = buffer.entries[-1]
        self.assertEqual((entry.capture, entry.event, entry.text), ('capture-A', '2026-10-01T10:00:00Z', 'Claude café'))
        buffer.feed(b'{"type":"item.completed","item":{"type":"command_execution","command":"cat file","aggregated_output":"done"}}\n')
        self.assertIsNone(buffer.entries[-1].event)
        self.assertIn('done', buffer.entries[-1].text)
        buffer.feed(b'{"type":"future.event","timestamp":"not-a-time","payload":"unknown"}\n')
        self.assertIsNone(buffer.entries[-1].event)
        self.assertIn('unknown', buffer.entries[-1].raw)
        buffer.feed(b'{"type":"error","message":"test failure"}\n')
        self.assertEqual(buffer.entries[-1].text, 'test failure')
        buffer.feed(b'{"type":"assistant","message":[]}\n')
        self.assertIn('unfamiliar shape', buffer.entries[-1].kind)
        format_cases = {}
        for runtime, event in (
            ('claude', {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'readable output'}]}}),
            ('codex', {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'readable output'}}),
        ):
            parsed = Buffer()
            raw = json.dumps(event).encode() + b'\n'
            parsed.feed(raw[:17], 'first fragment')
            self.assertEqual(parsed.total, 0)
            self.assertIsNotNone(parsed.preview())
            parsed.feed(raw[17:], 'second fragment')
            self.assertIn('readable output', parsed.entries[-1].text)
            long_event = json.loads(json.dumps(event).replace('readable output', 'L' * 40000))
            raw_long = json.dumps(long_event).encode() + b'\n'
            with tempfile.TemporaryDirectory(dir=ROOT) as folder:
                path = Path(folder) / 'process.log'
                path.write_bytes(raw_long)
                tail = Tail(path)
                while tail.offset < len(raw_long):
                    tail.poll()
                    self.assertLess(len(tail.buffer.pending), MAX_RECORD)
                self.assertEqual(path.read_bytes(), raw_long)
                self.assertGreater(tail.buffer.total, 1)
                self.assertTrue(all(len(e.text) <= 2048 for e in tail.buffer.entries))
            parsed.feed(b'{"type":"future.event","payload":"unknown"}\n')
            self.assertIn('unknown', parsed.entries[-1].raw)
            parsed.feed(b'{"type":"error","message":"test failure"}\n')
            self.assertIn('test failure', parsed.entries[-1].text)
            format_cases[runtime] = {'partial': True, 'long_raw_bytes_preserved': len(raw_long),
                                     'long_records_fragmented': True, 'unknown_and_error_visible': True}
        long_bytes = b'x' * (MAX_RECORD * 20 + 11)
        for start in range(0, len(long_bytes), READ_BUDGET):
            buffer.feed(long_bytes[start:start + READ_BUDGET], 'live')
        self.assertLess(len(buffer.pending), MAX_RECORD)
        self.assertLessEqual(max(len(e.text) for e in buffer.entries), 2048)
        buffer.feed(b'\n')
        for i in range(1000):
            buffer.feed(f'line {i}\n'.encode(), 'live')
        self.assertEqual(len(buffer.entries), MAX_ENTRIES)
        self.assertGreater(buffer.discarded, 800)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'
            path.write_bytes(b'historical no timestamp\n')
            tail = Tail(path)
            tail.poll()
            self.assertIsNone(tail.buffer.entries[-1].capture)
            with path.open('ab') as stream:
                stream.write(b'live appended\n')
            tail.poll()
            self.assertIsNotNone(tail.buffer.entries[-1].capture)
        FINDINGS['logs'] = {'format_cases': format_cases, 'partial_visible_before_newline': True, 'runtime_time_separate_from_first_fragment_capture': True,
                            'historical_capture': None, 'buffer_entries': len(buffer.entries),
                            'discarded': buffer.discarded, 'pending_bytes': len(buffer.pending),
                            'max_text_chars': max(len(e.text) for e in buffer.entries)}

    async def test_sanitized_runtime_replay(self):
        for runtime in ('claude', 'codex'):
            path = EVIDENCE / f'{runtime}-sanitized.jsonl'
            self.assertTrue(path.exists())
            app = View(Observer(ROOT), path)
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.3)
                self.assertTrue(any('SPIKE97_OBSERVER_OK' in e.text for e in app.tail.buffer.entries))
                self.assertTrue(all(e.capture is None and e.event is None for e in app.tail.buffer.entries))
                app.save_screenshot(f'{runtime}-sanitized.svg', path=str(EVIDENCE))

    async def test_refresh_request_audit(self):
        runner = DiscoveryCostRunner()
        gh = GitHub('org/project', runner=runner)
        loop = Loop(config(ROOT, agent(ROOT, kind='issue')), gh, 'operator', output=lambda *_: None)
        counts = []
        for _ in range(2):
            before = len(runner.calls)
            rows = status_rows(loop)
            counts.append(len(runner.calls) - before)
            self.assertEqual(len(rows), 30)
        cached_counts = []
        for _ in range(2):
            before = len(runner.calls)
            list(loop.iter_plans())
            cached_counts.append(len(runner.calls) - before)
        before = len(runner.calls)
        for _ in range(100):
            for row in rows:
                group(row)
                json.dumps(row)
        self.assertEqual(len(runner.calls), before)
        # GraphQL is a POST transport for a read query; inspect its payload too.
        methods = Counter(c[c.index('--method') + 1] for c in runner.calls)
        self.assertEqual(set(methods), {'GET', 'POST'})
        self.assertTrue(all(c[c.index('--include') + 1] == 'graphql' for c in runner.calls if c[c.index('--method') + 1] == 'POST'))
        FINDINGS['refresh_audit'] = {'fixture': '45 open items, 30 triggered, three shared commenters',
                                    'gh_calls_per_status_refresh': counts, 'gh_calls_cached_planner_pass': cached_counts, 'rest_total': gh.rest_requests,
                                    'all_call_methods': dict(methods), 'redraw_calls': len(runner.calls) - before,
                                    'network_requests': 0, 'transport': 'recording runner; real GitHub request construction'}

    async def test_navigation_growth_quota_and_narrow(self):
        observer = Observer(ROOT)
        writes = None
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.touch()
            app = View(observer, path, replay=True)
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.3)
                writes = list(observer.github.writes)
                self.assertEqual(set(group(r) for r in observer.rows), set(GROUPS))
                tree = app.query_one(Tree)
                recent = next(n for n in tree.root.children if str(n.label) == 'Recent activity')
                self.assertFalse(recent.is_expanded)
                total_before = app.tail.buffer.total
                await pilot.press('2'); self.assertEqual(app.query_one(TabbedContent).active, 'issue')
                await pilot.press('3'); self.assertEqual(app.query_one(TabbedContent).active, 'runs')
                # Expand recent activity and inspect its accepted source row.
                recent.expand()
                tree.select_node(recent.children[0])
                await pilot.pause()
                self.assertEqual(app.selected['number'], 6)
                app.save_screenshot('recent-accepted.svg', path=str(EVIDENCE))
                needs = next(n for n in tree.root.children if str(n.label) == 'Needs attention')
                tree.select_node(needs.children[0]); await pilot.pause()
                await pilot.press('2')
                app.save_screenshot('human-needed.svg', path=str(EVIDENCE))
                running = next(n for n in tree.root.children if str(n.label) == 'Running')
                tree.move_cursor(running.children[1]); await pilot.press('enter'); await pilot.pause()
                await pilot.press('1')
                self.assertFalse(app.local_selected())
                app.save_screenshot('remote-owned.svg', path=str(EVIDENCE))
                tree.move_cursor(running.children[0]); await pilot.press('enter'); await pilot.pause()
                for i in range(250):
                    with path.open('ab') as stream:
                        stream.write(f'Long output line {i}: evidence from a local tool\n'.encode())
                await pilot.pause(.3)
                await pilot.press('pageup'); await pilot.pause()
                log = app.query_one(RichLog)
                self.assertFalse(app.follow)
                self.assertLess(log.scroll_y, log.max_scroll_y)
                paused_scroll = log.scroll_y
                with path.open('ab') as stream:
                    stream.write(b'new while paused\n')
                await pilot.pause(.2)
                self.assertEqual(log.scroll_y, paused_scroll)
                app.save_screenshot('paused-scroll.svg', path=str(EVIDENCE))
                await pilot.press('f'); await pilot.pause()
                self.assertTrue(app.follow)
                self.assertEqual(log.scroll_y, log.max_scroll_y)
                await pilot.press('w')
                calls = observer.github.rest_requests
                await pilot.press('r', '2', '3', '1')
                self.assertEqual(observer.github.rest_requests, calls)
                self.assertEqual(observer.github.writes, writes)
                self.assertGreater(app.tail.buffer.total, total_before)
                app.save_screenshot('quota-wait.svg', path=str(EVIDENCE))
                with path.open('ab') as stream:
                    stream.write(b'{"type":"stream_event","event":{"delta":{"text":"partial')
                app.replay = False
                await pilot.pause(.2)
                self.assertIsNotNone(app.tail.buffer.preview())
                app.save_screenshot('partial-record.svg', path=str(EVIDENCE))
                with path.open('ab') as stream:
                    stream.write(b' record completed"}}}\n')
                await pilot.pause(.2)
                await pilot.resize_terminal(72, 24)
                await pilot.pause(.3)
                app.save_screenshot('narrow-72x24.svg', path=str(EVIDENCE))
                self.assertGreater(log.size.width, 20)
                self.assertLessEqual(log.virtual_size.width, log.scrollable_content_region.width)
                await pilot.press('q')
        FINDINGS['ui'] = {'terminal_sizes': [[110, 32], [72, 24]], 'navigation_with_growth': True,
                          'paused_scroll_stable': True, 'follow_returns_to_end': True,
                          'quota_refresh_and_navigation_requests': 0, 'runtime_output_workflow_writes': 0,
                          'log_widget_max_lines': 400, 'remote_log_allowed': False}

    async def test_closing_view_does_not_stop_owned_supervision(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            stop = threading.Event()
            script = 'import time; print("started",flush=True); time.sleep(2); print("finished",flush=True)'
            task = asyncio.create_task(asyncio.to_thread(supervise, [sys.executable, '-c', script], folder,
                                                        os.environ.copy(), folder, 5, stop))
            app = View(Observer(ROOT), folder / 'process.log')
            async with app.run_test() as pilot:
                for _ in range(10):
                    if (folder / 'pid').exists():
                        break
                    await pilot.pause(.05)
                await pilot.press('q')
            app.tick()  # A queued timer callback during teardown must be harmless.
            app.show_log()
            app.footer_state()
            try:
                self.assertFalse(stop.is_set())
                self.assertFalse(task.done())
                pid = int((folder / 'pid').read_text())
                self.assertTrue(group_members(pid))
                self.assertEqual(await task, 0)
                self.assertIn('finished', (folder / 'process.log').read_text())
                self.assertEqual(group_members(pid), [])
            finally:
                if not task.done():
                    stop.set()
                    try:
                        await task
                    except KeyboardInterrupt:
                        pass
        FINDINGS['lifecycle'] = {'close_view_worker_keeps_running': True, 'natural_exit_code': 0,
                                 'owned_group_empty_after_supervise': True}


if __name__ == '__main__':
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Evidence)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for svg in EVIDENCE.glob('*.svg'):
        svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    FINDINGS['validation'] = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)}
    (EVIDENCE / 'validation.json').write_text(json.dumps(FINDINGS, indent=2) + '\n')
    sys.exit(not result.wasSuccessful())
