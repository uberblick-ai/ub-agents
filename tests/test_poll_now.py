from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.observations import Observations
from ub_agents.records import iso
from tests.support import MemoryPublisher, PollGitHub, config, stub_refresh


class PollNowTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = replace(config(Path(temp.name)), poll_seconds=30)
        self.github = PollGitHub()
        self.memory = MemoryPublisher()
        self.observer = Observations(self.cfg, 'operator', None, self.memory)
        self.loop = Loop(self.cfg, self.github, 'operator', observer=self.observer,
                         output=lambda *_: None, interrupt_event=threading.Event())
        self.now = 1000
        self.loop.coordinator.clock = lambda: self.now
        clock = patch('ub_agents.loop.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.loop.enable_poll_now()

    def test_idle_request_starts_pass_resets_schedule_and_second_press_is_ignored(self):
        starts, wakes = [], []
        tick = self.loop.tick
        event = threading.Event

        def poll():
            starts.append(self.now)
            # In-flight presses are dropped, with no queued extra pass.
            self.loop.request_poll()
            result = tick()
            if len(starts) == 3:
                self.loop.stop_event.set()
            return result

        def wake_event():
            wake = event()
            wakes.append(wake)

            def wait(delay):
                if len(wakes) == 1:
                    self.now += 2
                    self.loop.request_poll()
                    self.assertTrue(wake.is_set())
                else:
                    self.now += 3
                    self.loop.request_poll()
                    self.assertFalse(wake.is_set())
                    status = self.memory.snapshots[-1]['poll_now']
                    self.assertEqual(status['cooldown_until'], iso(1012))
                    self.now = 1032
                return wake.is_set()

            wake.wait = wait
            return wake

        with patch('ub_agents.poll_now.threading.Event', side_effect=wake_event), \
                patch.object(self.loop, 'tick', side_effect=poll):
            self.loop.launch()
        self.assertEqual(starts, [1000, 1002, 1032])
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.cfg.poll_seconds, 30)

    def test_request_works_again_at_ten_seconds_and_keeps_no_pending_press(self):
        event = threading.Event
        for at, accepted in ((1000, True), (1009, False), (1010, True)):
            self.now = at
            wake = event()

            def wait(delay):
                self.loop.request_poll()
                self.assertEqual(wake.is_set(), accepted)
                if not accepted:
                    self.now += 1
                return wake.is_set()

            with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                    patch.object(wake, 'wait', side_effect=wait):
                self.loop._wait(self.loop.stop_event, 1, 'next poll or runtime pause')

    def test_rate_limit_and_poll_retry_do_not_wake_or_queue_requests(self):
        for reason in ('rate-limit reset', 'poll retry or runtime pause'):
            with self.subTest(reason=reason):
                calls = []

                def wait(delay):
                    calls.append(delay)
                    self.loop.request_poll()
                    self.assertFalse(self.loop.stop_event.is_set())
                    self.assertFalse(self.loop.interrupt_event.is_set())
                    if reason == 'rate-limit reset':
                        self.assertEqual(self.memory.snapshots[-1]['poll_now']['rate_limit_until'], iso(1030))

                with patch.object(self.loop.stop_event, 'wait', side_effect=wait):
                    if reason == 'rate-limit reset':
                        self.loop.wait_rate_limit(GitHubError('GET', 'items', 'rate limit',
                                                            rate_limited=True, reset_at=1030))
                    else:
                        self.loop._wait(self.loop.stop_event, 30, reason)
                self.assertEqual(calls, [30])
                self.assertIsNone(self.loop.poll_now.waiter)
                self.assertEqual(self.loop.poll_now.next_allowed, 0)

    def test_no_ui_request_has_no_effect(self):
        self.loop.poll_now = None
        self.loop.request_poll()
        with patch.object(self.loop.stop_event, 'wait') as wait:
            self.loop._wait(self.loop.stop_event, 30, 'next poll or runtime pause')
        wait.assert_called_once_with(30)
        self.assertNotIn('poll_now', self.memory.snapshots[-1])
