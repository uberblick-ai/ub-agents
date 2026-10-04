from datetime import datetime, timezone
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
from unittest.mock import Mock, patch

from tests.support import (FakeGitHub, MemoryPublisher, RecordingUpdateRunner, config,
                           isolate_runtime_state)
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.updates import DAY, Updates, installation, release_age, release_banner


def release(tag='v0.1.12', **values):
    return {'tag_name': tag, 'published_at': '2026-10-02T00:00:00Z',
            'draft': False, 'prerelease': False} | values


def eventually(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        isolate_runtime_state(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def checker(self, method, runner, clock=None):
        update = Updates(self.root, detect=lambda _: method, runner=runner,
                         **({'clock': clock} if clock else {}))
        self.addCleanup(update.close)
        update.start()
        return update

    def test_installation_detection_and_checkout_scope(self):
        source = self.root / 'src/ub_agents/updates.py'
        source.parent.mkdir(parents=True)
        (self.root / 'pyproject.toml').touch()
        dist = Mock()
        dist.read_text.return_value = json.dumps({'url': self.root.as_uri(), 'dir_info': {'editable': True}})
        self.assertEqual(installation(self.root, source, dist), 'checkout')
        self.assertIsNone(installation(self.root / 'other-project', source, dist))
        # Metadata for a different install must never turn source into a release.
        dist.read_text.return_value = None
        self.assertIsNone(installation(self.root, source, dist))
        for path, expected in (
                ('Cellar/ub-agents/0.1.11/libexec/lib/python3.11/site-packages/ub_agents/updates.py', 'brew'),
                ('Cellar/python@3.11/3.11/lib/python3.11/site-packages/ub_agents/updates.py', 'pip'),
                ('venv/lib/python3.11/site-packages/ub_agents/updates.py', 'pip')):
            self.assertEqual(installation(self.root, self.root / path, dist), expected)
        # URL decoding and symlinked control paths preserve the checkout identity.
        spaced = self.root / 'source checkout'
        spaced.mkdir()
        dist.read_text.return_value = json.dumps({'url': spaced.as_uri(), 'dir_info': {'editable': True}})
        link = self.root / 'control-link'
        link.symlink_to(spaced, target_is_directory=True)
        self.assertEqual(installation(link, spaced / 'src/ub_agents/updates.py', dist), 'checkout')
        dist.read_text.return_value = 'invalid json'
        self.assertIsNone(installation(self.root, source, dist))

    def test_strict_release_comparison_commands_and_age(self):
        for tag in ('v0.1.11', '0.1.10', 'v0.0.99'):
            self.assertIsNone(release_banner(release(tag), 'brew', running='0.1.11'))
        for method, command in (('brew', 'brew upgrade ub-agents'), ('pip', 'pip install -U ub-agents')):
            banner = release_banner(release('v0.1.100'), method, running='0.1.11')
            self.assertEqual(banner['text'], f'⬆ ub-agents 0.1.100 is available · you run 0.1.11 · '
                                            f'{command}, then restart the launcher')
        for values in ({'tag_name': 'untrusted\nversion'}, {'tag_name': 'v0.1.12-rc1'},
                       {'prerelease': True}, {'draft': True}, {'published_at': 'bad'},
                       {'published_at': '2026-10-02T00:00:00'}):
            with self.assertRaises(ValueError):
                release_banner(release(**values), 'pip')
        stamp = '2026-10-02T00:00:00Z'
        self.assertEqual(release_age(stamp, datetime(2026, 10, 4, tzinfo=timezone.utc)), 'released 2 days ago')
        self.assertEqual(release_age(stamp, datetime(2026, 10, 3, tzinfo=timezone.utc)), 'released 1 day ago')
        self.assertEqual(release_age(stamp, datetime(2026, 10, 2, tzinfo=timezone.utc)), 'released today')
        self.assertEqual(release_age('bad'), '')

    def test_release_is_one_rest_call_at_start_and_daily_failures_retain_result(self):
        now = [0]
        runner = RecordingUpdateRunner(json.dumps(release()), OSError('offline'),
                                       'bad json', json.dumps(release('0.1.13')))
        update = self.checker('pip', runner, lambda: now[0])
        self.assertTrue(eventually(lambda: update.banner is not None))
        first = update.banner
        self.assertEqual(runner.calls[0][:7], ['gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                                             'repos/uberblick-ai/ub-agents/releases/latest'])
        for _ in range(10):
            update.wake.set()
        time.sleep(0.05)
        self.assertEqual(len(runner.calls), 1)
        now[0] = DAY - 1
        update.wake.set()
        time.sleep(0.05)
        self.assertEqual(len(runner.calls), 1)
        for count in (2, 3):
            now[0] += DAY
            update.wake.set()
            self.assertTrue(runner.wait_calls(count))
            self.assertEqual(update.banner, first)
        now[0] += DAY
        update.wake.set()
        self.assertTrue(eventually(lambda: update.banner and '0.1.13' in update.banner['text']))
        self.assertEqual(len(runner.calls), 4)

    def test_unrelated_checkout_never_runs_a_request(self):
        runner = RecordingUpdateRunner()
        update = self.checker(None, runner)
        update.thread.join(1)
        self.assertFalse(update.thread.is_alive())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(update.banner)

    def test_checkout_waits_for_normal_fetch_and_keeps_started_revision(self):
        runner = RecordingUpdateRunner('a' * 40, '2', OSError('slow disk'), '3')
        update = self.checker('checkout', runner)
        self.assertTrue(eventually(lambda: update.started == 'a' * 40))
        self.assertIsNone(update.banner)
        self.assertEqual(len(runner.calls), 1)
        update.fetched('other-branch', 'a' * 40, 'b' * 40)
        time.sleep(0.03)
        self.assertEqual(len(runner.calls), 1)
        update.fetched('main', 'a' * 40, 'b' * 40)
        self.assertTrue(eventually(lambda: update.banner is not None))
        self.assertEqual(update.banner['text'], '⬆ This launcher runs code 2 commits behind origin/main · '
                                               'restart the launcher')
        first = update.banner
        update.fetched('main', 'b' * 40, 'c' * 40)
        self.assertTrue(runner.wait_calls(3))
        self.assertEqual(update.banner, first)
        update.fetched('main', 'c' * 40, 'd' * 40)
        self.assertTrue(eventually(lambda: update.banner and '3 commits' in update.banner['text']))
        self.assertEqual(runner.calls[-1][-1], 'a' * 40 + '..' + 'd' * 40)
        self.assertFalse(any('fetch' in call or 'gh' in call for call in runner.calls))

    def test_first_fetch_during_startup_preserves_pre_merge_head(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def delayed_head():
            entered.set()
            finish.wait(3)
            return 'b' * 40  # HEAD has already been fast-forwarded.
        runner = RecordingUpdateRunner(delayed_head, '1')
        update = self.checker('checkout', runner)
        self.assertTrue(entered.wait(1))
        update.fetched('main', 'a' * 40, 'b' * 40)
        finish.set()
        self.assertTrue(eventually(lambda: update.banner is not None))
        self.assertEqual(update.started, 'a' * 40)
        self.assertEqual(runner.calls[-1][-1], 'a' * 40 + '..' + 'b' * 40)

    def test_slow_check_does_not_block_launch_or_poll_and_plain_text_prints_once(self):
        finish, entered = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def delayed_release():
            entered.set()
            finish.wait(3)
            return json.dumps(release())
        runner = RecordingUpdateRunner(delayed_release)
        update = self.checker('brew', runner)
        self.assertTrue(entered.wait(1))
        lines = []
        cfg = config(self.root)
        memory = MemoryPublisher()
        observer = Observations(cfg, 'operator', None, memory)
        loop = Loop(cfg, FakeGitHub(), 'operator', output=lines.append, observer=observer)
        loop.updates = update
        started = time.monotonic()
        loop.launch(once=True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(observer.state['update'])
        finish.set()
        self.assertTrue(eventually(lambda: update.banner is not None))
        for _ in range(10):
            loop._poll_updates()
        self.assertEqual(len([line for line in lines if line.startswith('⬆')]), 1)
        self.assertEqual(memory.snapshots[-1]['update'], update.banner)
        self.assertNotIn('\n', lines[-1])
        update.banner = None
        loop._poll_updates()
        self.assertIsNone(memory.snapshots[-1]['update'])

    def test_result_before_observer_start_still_reaches_snapshot_without_repeat_output(self):
        cfg = config(self.root)
        lines = []
        loop = Loop(cfg, FakeGitHub(), 'operator', output=lines.append)
        loop.updates = Mock(banner=release_banner(release(), 'pip'))
        loop._poll_updates()
        self.assertEqual(len(lines), 1)
        memory = MemoryPublisher()
        loop.observer = Observations(cfg, 'operator', None, memory)
        loop._poll_updates()
        self.assertEqual(memory.snapshots[-1]['update'], loop.updates.banner)
        self.assertEqual(len(lines), 1)

    def test_hung_owned_request_is_cancelled_and_reaped(self):
        shim = self.root / 'gh'
        pid = self.root / 'request.pid'
        shim.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, signal, time
pathlib.Path(__file__).with_name('request.pid').write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True: time.sleep(1)
''')
        shim.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            update = Updates(self.root, detect=lambda _: 'pip')
            self.addCleanup(update.close)
            update.start()
            self.assertTrue(eventually(pid.exists))
            owned = int(pid.read_text())
            try:
                started = time.monotonic()
                update.close()
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(update.thread.is_alive())
                with self.assertRaises(ProcessLookupError):
                    os.kill(owned, 0)
            finally:
                try:
                    os.kill(owned, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertIsNone(update.banner)

    def test_request_timeout_keeps_previous_result_and_reaps_command(self):
        shim, pid = self.root / 'gh', self.root / 'request.pid'
        shim.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, time
pathlib.Path(__file__).with_name('request.pid').write_text(str(os.getpid()))
while True: time.sleep(1)
''')
        shim.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}), \
                patch('ub_agents.updates.CHECK_SECONDS', 0.5):
            update = Updates(self.root, detect=lambda _: 'pip')
            update.banner = previous = release_banner(release(), 'pip')
            self.addCleanup(update.close)
            update.start()
            self.assertTrue(eventually(pid.exists))
            owned = int(pid.read_text())
            def gone():
                try:
                    os.kill(owned, 0)
                    return False
                except ProcessLookupError:
                    return True
            try:
                self.assertTrue(eventually(gone))
                self.assertEqual(update.banner, previous)
            finally:
                try:
                    os.kill(owned, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_killed_launcher_leaves_no_owned_update_request(self):
        shim, pid = self.root / 'gh', self.root / 'request.pid'
        shim.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, time
pathlib.Path(__file__).with_name('request.pid').write_text(str(os.getpid()))
while True: time.sleep(1)
''')
        shim.chmod(0o700)
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        parent = subprocess.Popen([sys.executable, '-P', '-c', '''
import sys
from pathlib import Path
from ub_agents.updates import Updates
updates = Updates(Path(sys.argv[1]), detect=lambda _: 'pip')
updates.start()
sys.stdin.read()
''', str(self.root)], env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owned = None
        try:
            self.assertTrue(eventually(pid.exists))
            owned = int(pid.read_text())
            parent.kill()
            parent.wait(timeout=2)
            def gone():
                try:
                    os.kill(owned, 0)
                    return False
                except ProcessLookupError:
                    return True
            self.assertTrue(eventually(gone), 'Update request survived launcher death')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=2)
            parent.stdin.close()
            if owned is not None:
                try:
                    os.kill(owned, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main()
