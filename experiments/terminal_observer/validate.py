"""Offline evidence; separate from the package suite. Starts no agent runtime."""
import asyncio
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from textual.widgets import RichLog, Static, TabbedContent, Tree
from ub_agents.cli import status_rows
from ub_agents.config import load_config
from ub_agents.execution import command_for, supervise, group_members
from ub_agents.github import GitHub
from ub_agents.loop import Loop
from tests.support import DiscoveryCostRunner, agent, config, issue

from .demo import View
from .logs import Buffer, Tail, MAX_ENTRIES, MAX_RECORD, MAX_TEXT, READ_BUDGET
from .model import Observer, GROUPS, group
from .replay import write_fixtures

ROOT = Path.cwd()
EVIDENCE = ROOT / 'experiments/terminal_observer/evidence'
FINDINGS = {}


class QuotaRunner(DiscoveryCostRunner):
    limited = False

    def __call__(self, command, **kwargs):
        if not self.limited:
            return super().__call__(command, **kwargs)
        self.calls.append(command)
        text = ('HTTP/2 403 Forbidden\nX-RateLimit-Remaining: 0\nX-RateLimit-Limit: 5000\n'
                f'X-RateLimit-Reset: {int(time.time() + 60)}\nX-RateLimit-Resource: core\n\n'
                '{"message":"API rate limit exceeded (fixture)"}')
        return subprocess.CompletedProcess(command, 1, text, 'gh: API rate limit exceeded')


class NavigationRefreshMutation(View):
    """Deliberate defect: the navigation audit must detect these requests."""
    def on_tabbed_content_tab_activated(self, event):
        super().on_tabbed_content_tab_activated(event)
        self.observer.refresh()


class Evidence(unittest.IsolatedAsyncioTestCase):
    async def test_current_human_blocker_and_completed_history(self):
        observer = Observer(ROOT)
        observer.refresh()
        row = next(r for r in observer.rows if r['number'] == 6)
        self.assertEqual(row['state'], 'parked')
        self.assertIn('Stop label needs-human', row['reason'])
        self.assertEqual(group(row), 'Needs attention')  # Fails with the reviewed ordering.
        self.assertTrue(row['outcome']['accepted'])
        self.assertTrue(row['outcome']['transition_complete'])
        # A completed transition with no trigger/unfinished claim/stop label disappears.
        loop, gh = observer.loop, observer.github
        gh.items[7] = issue(7)
        worker = replace(loop.config.agents[0], outcomes={'finished': {'add': (), 'remove': ('ready',)}})
        loop.config = replace(loop.config, agents=(worker,))
        plan = next(p for p in loop.plans() if p.item.number == 7)
        lease = loop.coordinator.claim(plan)
        report = loop.coordinator.report(lease, 'success', 'Finished fixture step', outcome='finished')
        loop.finalize(lease, plan, report, 'fixture completion')
        loop.coordinator.release(lease, 'success', 'Finished fixture step')
        self.assertNotIn(7, [r['number'] for r in status_rows(loop)])
        FINDINGS['work_model'] = {'human_blocker_6_group': group(row), 'accepted_step_retained_in_runs': True,
                                 'completed_item_without_trigger_disappears': True, 'complete_history': 'unverified'}

    async def test_truncation_clears_unfinished_record_and_time(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'
            path.touch()
            tail = Tail(path)
            path.write_bytes(b'old partial record')
            tail.poll()
            self.assertIsNotNone(tail.buffer.capture)
            path.write_bytes(b'new\n')
            tail.poll()
            self.assertEqual(tail.buffer.entries[-1].text, 'new')  # Old code: old partial recordnew.
            self.assertIsNone(tail.buffer.entries[-1].capture)
            self.assertEqual(tail.buffer.pending, b'')
            # Also distinguish file replacement from the old file's pending bytes.
            with path.open('ab') as stream:
                stream.write(b'old pending')
            tail.poll()
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(b'replacement longer than the previous generation\n')
            replacement.replace(path)
            tail.poll()
            self.assertEqual(tail.buffer.entries[-1].text, 'replacement longer than the previous generation')
            self.assertIsNone(tail.buffer.entries[-1].capture)
        FINDINGS['truncation'] = {'unfinished_bytes_and_time_reset': True, 'inode_replacement_reset': True}

    async def test_formats_bounds_and_capture(self):
        buffer = Buffer()
        record = json.dumps({'type': 'assistant', 'timestamp': '2026-10-01T10:00:00Z',
                             'message': {'content': [{'type': 'text', 'text': 'Claude café'}]}}).encode() + b'\n'
        buffer.feed(record[:20], 'capture-A')
        self.assertEqual(buffer.total, 0)
        self.assertIsNotNone(buffer.preview())
        buffer.feed(record[20:], 'capture-B')
        entry = buffer.entries[-1]
        self.assertEqual((entry.capture, entry.event, entry.text), ('capture-A', '2026-10-01T10:00:00Z', 'Claude café'))
        buffer.feed(b'{"type":"future.event","timestamp":"not-a-time","payload":"unknown"}\n')
        self.assertIsNone(buffer.entries[-1].event)
        self.assertIn('unknown', buffer.entries[-1].raw)
        buffer.feed(b'{"type":"error","message":"test failure"}\n')
        self.assertEqual(buffer.entries[-1].text, 'test failure')
        buffer.feed(b'{"type":"assistant","message":[]}\n')
        self.assertIn('unfamiliar shape', buffer.entries[-1].kind)
        buffer.feed(b'\x1b[31m[bold]plain output\n')
        self.assertNotIn('\x1b', buffer.entries[-1].text)
        # A newline-complete Codex human line stays compact; not mislabeled partial.
        self.assertEqual(buffer.entries[-1].kind, 'text')
        self.assertEqual(buffer.entries[-1].display().count('\n'), 0)
        paths = write_fixtures(EVIDENCE)
        cases = {}
        for runtime, path in paths.items():
            raw = path.read_bytes()
            tail = Tail(path)
            while tail.offset < len(raw) or tail.buffer.ready():
                tail.poll()
                self.assertLess(len(tail.buffer.pending), MAX_RECORD)
            self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(all(e.capture is None and e.event is None for e in tail.buffer.entries))
            if runtime == 'claude':
                long = next(e for e in tail.buffer.entries if 'BEGIN tool output' in e.text)
                self.assertEqual(long.kind, 'user')
                self.assertIn('tool result id=synthetic-long', long.text)
                self.assertIn('END tool output', long.text)
                self.assertIn('display shortened', long.text)
                self.assertTrue(any('tool result id=synthetic-failed [error]' in e.text for e in tail.buffer.entries))
                self.assertTrue(any(e.kind == 'rate_limit_event' for e in tail.buffer.entries))
                self.assertFalse(any(e.kind.startswith('oversized') for e in tail.buffer.entries))
                self.assertGreater(max(map(len, raw.splitlines())), 16 * 1024)
            self.assertTrue(any('SPIKE97_OBSERVER_OK' in e.text for e in tail.buffer.entries))
            cases[runtime] = {'synthetic_production_format': True, 'raw_bytes': len(raw),
                              'max_record_bytes': max(map(len, raw.splitlines())), 'normalized_entries': tail.buffer.total}
        # Split a >16 KiB tool result over multiple reads; still decode one whole event.
        raw = json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'long', 'content': 'BEGIN' + 'L' * 42000 + 'END'}]}}).encode() + b'\n'
        partial = Buffer()
        partial.feed(raw[:17000], 'first-fragment')
        self.assertEqual(partial.total, 0)
        self.assertIsNotNone(partial.preview())
        partial.feed(raw[17000:], 'last-fragment')
        self.assertEqual(partial.total, 1)
        self.assertEqual(partial.entries[0].capture, 'first-fragment')
        self.assertIn('tool result id=long', partial.entries[0].text)
        self.assertIn('END', partial.entries[0].text)
        long_bytes = b'x' * (MAX_RECORD * 3 + 11)
        for start in range(0, len(long_bytes), READ_BUDGET):
            buffer.feed(long_bytes[start:start + READ_BUDGET], 'live')
            self.assertLess(len(buffer.pending), MAX_RECORD)
        buffer.feed(b'\n')
        self.assertTrue(any(e.kind.startswith('oversized') for e in buffer.entries))
        for i in range(1000):
            buffer.feed(f'line {i}\n'.encode(), 'live')
        self.assertEqual(len(buffer.entries), MAX_ENTRIES)
        self.assertGreater(buffer.discarded, 800)
        self.assertTrue(all(len(e.text) <= MAX_TEXT for e in buffer.entries))
        cfg = load_config(ROOT / 'ub-agent.yaml')
        commands = {a.name: command_for(a, a.runtimes[0]) for a in cfg.agents}
        self.assertNotIn('--json', commands['implementer'])
        self.assertNotIn('--include-partial-messages', commands['issue-preparer'])
        FINDINGS['logs'] = {'format_cases': cases, 'production_commands': commands,
            'partial_visible_before_newline': True, 'event_time_separate_from_capture': 'synthetic only',
            'historical_capture': None, 'max_record_bytes': MAX_RECORD, 'max_entries': MAX_ENTRIES,
            'max_text_chars': MAX_TEXT, 'read_bytes_per_tick': READ_BUDGET,
            'long_tool_result_decoded_once': True, 'above_cap': 'labeled raw fragments; readability unverified'}

    async def test_production_runtime_replay(self):
        for runtime, path in write_fixtures(EVIDENCE).items():
            app = View(Observer(ROOT, log_path=path))
            async with app.run_test(size=(110, 32)) as pilot:
                deadline = time.monotonic() + 4
                while (not app.tail or app.tail.offset < path.stat().st_size or app.tail.buffer.ready()) and time.monotonic() < deadline:
                    await pilot.pause(.1)
                self.assertEqual(app.tail.path, path.resolve())
                self.assertTrue(any('SPIKE97_OBSERVER_OK' in e.text for e in app.tail.buffer.entries))
                app.save_screenshot(f'{runtime}-production.svg', path=str(EVIDENCE))

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
        methods = Counter(c[c.index('--method') + 1] for c in runner.calls)
        self.assertEqual(set(methods), {'GET', 'POST'})
        self.assertTrue(all(c[c.index('--include') + 1] == 'graphql' for c in runner.calls if c[c.index('--method') + 1] == 'POST'))
        FINDINGS['fixture_refresh'] = {'fixture': '45 open issues, 30 triggered, three shared commenters; no ETags or PRs',
            'gh_calls_per_status_refresh': counts, 'gh_calls_cached_planner_pass': cached_counts,
            'all_call_methods': dict(methods), 'network_requests': 0}

    async def test_navigation_transport_and_detected_mutation(self):
        async def audit(app_class, quota=False):
            runner = QuotaRunner()
            gh = GitHub('org/project', runner=runner)
            loop = Loop(config(ROOT, agent(ROOT, kind='issue')), gh, 'operator', output=lambda *_: None)
            observer = Observer(ROOT, loop=loop)
            # Cooldown cannot hide a navigation defect; bypass it for the mutation audit.
            refresh = observer.refresh
            def without_cooldown():
                observer.wait_until = 0
                return refresh()
            observer.refresh = without_cooldown
            app = app_class(observer)
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.2)
                initial_cost = observer.last_requests
                self.assertEqual(initial_cost, 125)
                self.assertEqual(observer.last_quota, 95)
                self.assertEqual(observer.last_graphql, 30)
                before = len(runner.calls)
                await pilot.press('2', '3', '1')
                tree = app.query_one(Tree)
                bucket = next(n for n in tree.root.children if str(n.label) == 'Eligible')
                tree.move_cursor(bucket.children[1])
                await pilot.press('enter', 'u', 'f')
                await pilot.resize_terminal(72, 24)
                await pilot.pause(.2)
                delta = len(runner.calls) - before
                if quota:
                    observer.refresh = refresh
                    observer.wait_until = 0
                    runner.limited = True
                    previous_rows = observer.rows
                    await pilot.press('r'); await pilot.pause(.2)
                    self.assertIn('quota wait', observer.poll)
                    self.assertIs(observer.rows, previous_rows)
                    failed_refresh_calls = observer.last_requests
                    self.assertEqual(failed_refresh_calls, 1)
                    before_wait = len(runner.calls)
                    await pilot.press('r', '2', '3', '1')
                    await pilot.pause(.2)
                    self.assertEqual(len(runner.calls), before_wait)
                    app.save_screenshot('transport-quota-wait.svg', path=str(EVIDENCE))
                    FINDINGS['quota_transport'] = {'source': 'synthetic HTTP 403 through real GitHub transport',
                        'failed_refresh_calls': failed_refresh_calls, 'wait_navigation_and_refresh_calls': 0,
                        'snapshot_retained': True}
                await pilot.press('q')
            return delta, initial_cost
        good, initial = await audit(View, quota=True)
        self.assertEqual(good, 0)
        mutated, _ = await audit(NavigationRefreshMutation)
        with self.assertRaises(AssertionError):
            self.assertEqual(mutated, 0)
        self.assertGreater(mutated, 0)
        FINDINGS['navigation_transport'] = {'transport': 'GitHub(runner=DiscoveryCostRunner); no network',
            'initial_refresh_calls': initial, 'navigation_selection_resize_calls': good,
            'deliberate_navigation_refresh_calls': mutated, 'zero_request_assertion_rejects_mutation': True}

    async def test_lease_log_locator(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.write_text('fixture run linked by lease\n')
            other_path = Path(directory) / 'run-42' / 'process.log'
            other_path.parent.mkdir()
            other_path.write_text('distinct log for claimed run 42\n')
            observer = Observer(ROOT, log_path=path)
            observer.refresh()
            row = next(r for r in observer.rows if r['number'] == 1)
            remote = next(r for r in observer.rows if r['number'] == 2)
            self.assertEqual(observer.log_path(row), path.resolve())
            self.assertIsNone(observer.log_path(remote))
            # Attach a different issue/run number using the durable lease record.
            new_issue = issue(42)
            observer.github.items[42] = new_issue
            plan = next(p for p in observer.loop.plans() if p.item.number == 42)
            lease = observer.loop.coordinator.claim(plan)
            import socket
            observer.loop.coordinator.update(lease, state='running', started=True,
                host=socket.gethostname(), log_dir=str(other_path.parent.resolve()))
            app = View(observer)
            async with app.run_test() as pilot:
                await pilot.pause(.2)
                running = next(n for n in app.query_one(Tree).root.children if str(n.label) == 'Running')
                node = next(n for n in running.children if n.data['number'] == 42)
                app.query_one(Tree).select_node(node); await pilot.pause(.2)
                self.assertTrue(app.local_selected())
                self.assertEqual(app.selected['lease']['run'], lease['run'])
                self.assertEqual(app.tail.path, other_path.resolve())
                self.assertEqual(app.tail.buffer.entries[0].text, 'distinct log for claimed run 42')
                app.save_screenshot('lease-selected-log.svg', path=str(EVIDENCE))
                remote_node = next(n for n in running.children if n.data['number'] == 2)
                app.query_one(Tree).select_node(remote_node); await pilot.pause(.2)
                self.assertIsNone(app.tail)
                self.assertFalse(app.local_selected())
                app.query_one(Tree).select_node(node); await pilot.pause()
                observer.loop.coordinator.release(lease, 'success', 'Fixture run ended')
                observer.github.change(42, labels=frozenset())
                await app.action_snapshot()
                self.assertNotEqual(app.selected['number'], 42)
                self.assertNotEqual(app.tail.path if app.tail else None, other_path.resolve())
        FINDINGS['attachment'] = {'source': 'fixture coordinator lease record, real status_rows',
            'selected_issue': 42, 'resolved_from_host_and_log_dir': True, 'remote_has_no_tail': True,
            'real_claim_end_to_end': 'unverified'}

    async def test_navigation_growth_and_narrow(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'process.log'; path.touch()
            observer = Observer(ROOT, log_path=path)
            app = View(observer, replay=True)
            async with app.run_test(size=(110, 32)) as pilot:
                await pilot.pause(.3)
                writes = list(observer.github.writes)
                tree = app.query_one(Tree)
                self.assertEqual([str(n.label) for n in tree.root.children], list(GROUPS))
                recent = next(n for n in tree.root.children if str(n.label) == 'Recent activity')
                self.assertFalse(recent.is_expanded)
                self.assertFalse(recent.children)  # status_rows isn't complete history.
                total_before = app.tail.buffer.total
                await pilot.press('2', '3')
                needs = next(n for n in tree.root.children if str(n.label) == 'Needs attention')
                completed = next(n for n in needs.children if n.data['number'] == 6)
                self.assertTrue(needs.is_expanded)
                tree.select_node(completed); await pilot.pause()
                self.assertEqual(group(app.selected), 'Needs attention')
                app.save_screenshot('accepted-human-blocker.svg', path=str(EVIDENCE))
                tree.select_node(needs.children[0]); await pilot.pause()
                await pilot.press('2')
                app.save_screenshot('human-needed.svg', path=str(EVIDENCE))
                running = next(n for n in tree.root.children if str(n.label) == 'Running')
                tree.move_cursor(running.children[1]); await pilot.press('enter', '1'); await pilot.pause()
                self.assertFalse(app.local_selected())
                app.save_screenshot('remote-owned.svg', path=str(EVIDENCE))
                tree.move_cursor(running.children[0]); await pilot.press('enter'); await pilot.pause()
                app.replay = False
                with path.open('ab') as stream:
                    stream.write(b''.join(f'Output line {i}: local tool evidence\n'.encode() for i in range(500)))
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
                await pilot.press('w', 'r', '2', '3', '1')
                self.assertEqual(observer.github.writes, writes)
                self.assertGreater(app.tail.buffer.total, total_before)
                app.save_screenshot('quota-wait.svg', path=str(EVIDENCE))
                with path.open('ab') as stream:
                    stream.write(b'{"type":"assistant","message":{"content":[{"type":"text","text":"partial')
                await pilot.pause(.2)
                self.assertIsNotNone(app.tail.buffer.preview())
                app.save_screenshot('partial-record.svg', path=str(EVIDENCE))
                with path.open('ab') as stream:
                    stream.write(b' record completed"}]}}\n')
                await pilot.pause(.2)
                await pilot.resize_terminal(72, 24); await pilot.pause(.3)
                app.save_screenshot('narrow-72x24.svg', path=str(EVIDENCE))
                self.assertGreater(log.size.width, 20)
                self.assertLessEqual(log.virtual_size.width, log.scrollable_content_region.width)
                await pilot.press('q')
            app.tick(); app.show_log(); app.footer_state()  # queued callbacks after teardown
        FINDINGS['ui'] = {'terminal_sizes': [[110, 32], [72, 24]], 'navigation_with_growth': True,
            'paused_scroll_stable': True, 'follow_returns_to_end': True,
            'runtime_output_workflow_writes': 0, 'log_widget_max_lines': 400,
            'real_terminal_or_herdr': 'unverified'}

    async def test_separate_process_closure_leaves_owned_worker(self):
        # Real POSIX observer process, own PTY, separate from our owned dummy supervisor.
        import fcntl
        import pty
        import select
        import struct
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            folder = Path(directory)
            stop = threading.Event()
            finish = folder / 'finish'
            script = ('import pathlib,time; print("started",flush=True); '
                      f'p=pathlib.Path({str(finish)!r});\n'
                      'while not p.exists(): time.sleep(.05)\n'
                      'print("finished",flush=True)')
            def worker():
                try:
                    return supervise([sys.executable, '-c', script], folder,
                                     os.environ.copy(), folder, 30, stop)
                except KeyboardInterrupt:
                    return 'interrupted during test cleanup'
            task = asyncio.create_task(asyncio.to_thread(worker))
            modes = {}
            try:
                for mode, key in [('q', b'q'), ('ctrl-c', b'\x03')]:
                    ready = folder / f'ready-{mode}'
                    code = ('from pathlib import Path\nfrom experiments.terminal_observer.demo import View\n'
                            'from experiments.terminal_observer.model import Observer\n'
                            'class ReadyView(View):\n'
                            ' async def on_mount(self):\n'
                            '  await super().on_mount()\n'
                            f'  Path({str(ready)!r}).touch()\n'
                            f'ReadyView(Observer(Path.cwd(),log_path=Path({str(folder / "process.log")!r}))).run()\n')
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, __import__('termios').TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
                    process = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, stdin=slave,
                        stdout=slave, stderr=slave, env=os.environ | {'TERM': 'xterm-256color'}, start_new_session=True)
                    os.close(slave)
                    try:
                        deadline = time.monotonic() + 8
                        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                            if select.select([master], [], [], 0)[0]:
                                os.read(master, 65536)
                            await asyncio.sleep(.05)
                        self.assertTrue(ready.exists(), f'{mode}: observer did not mount')
                        await asyncio.sleep(.2)
                        os.write(master, key)
                        deadline = time.monotonic() + 5
                        while process.poll() is None and time.monotonic() < deadline:
                            if select.select([master], [], [], 0)[0]:
                                try:
                                    os.read(master, 65536)
                                except OSError:
                                    break
                            await asyncio.sleep(.05)
                        self.assertIsNotNone(process.poll(), f'{mode}: observer did not exit')
                        self.assertEqual(process.returncode, 0)
                        self.assertFalse(stop.is_set())
                        self.assertFalse(task.done())
                        pid = int((folder / 'pid').read_text())
                        self.assertTrue(group_members(pid))
                        modes[mode] = {'observer_exit': 0, 'worker_alive': True, 'supervisor_stop_unset': True}
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        os.close(master)
                finish.touch()
                self.assertEqual(await task, 0)
                self.assertEqual(group_members(pid), [])
            finally:
                if not task.done():
                    stop.set()
                    try:
                        await task
                    except KeyboardInterrupt:
                        pass
        FINDINGS['lifecycle'] = {'separate_observer_process_pty': modes,
            'dummy_worker_natural_exit': 0, 'owned_group_empty': True, 'attached_alternative': 'unverified'}


if __name__ == '__main__':
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Evidence)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for svg in EVIDENCE.glob('*.svg'):
        svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines()) + '\n')
    FINDINGS['validation'] = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors)}
    (EVIDENCE / 'validation.json').write_text(json.dumps(FINDINGS, indent=2) + '\n')
    sys.exit(not result.wasSuccessful())
