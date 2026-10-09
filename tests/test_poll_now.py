from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.errors import GitHubError, LostOwnership
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

    def test_repeated_poll_now_passes_bypass_debt_then_idle_wait_repays_it(self):
        starts, waits = [], []
        event = threading.Event

        def poll():
            starts.append(self.now)
            if len(starts) == 4:
                self.loop.stop_event.set()
                return False
            self.github.quota_requests += 100
            return False

        def wake_event():
            wake = event()

            def wait(delay):
                if len(waits) < 2:
                    self.now += 10
                    self.loop.request_poll()
                else:
                    self.now += delay
                return wake.is_set()

            wake.wait = wait
            return wake

        regular = self.loop.poll_now.wait

        def wait(stop, delay, update=None, on_request=None):
            result = regular(stop, delay, update, on_request)
            waits.append(delay)
            return result

        with patch('ub_agents.poll_now.threading.Event', side_effect=wake_event), \
                patch.object(self.loop.poll_now, 'wait', side_effect=wait), \
                patch.object(self.loop, '_discovery_tick', side_effect=poll), \
                patch('ub_agents.poll_now.monotonic', side_effect=lambda: self.now):
            self.loop.launch()
        self.assertEqual(starts[:3], [1000, 1010, 1020])
        self.assertAlmostEqual(starts[3], 3534.4)
        self.assertAlmostEqual(waits[-1], 2514.4)
        self.assertAlmostEqual(self.loop.discovery_budget.balance, 1)

    def test_waiter_status_clears_after_request_deadline_cancellation_and_failure(self):
        event = threading.Event
        for ending in ('request', 'deadline', 'cancel', 'failure'):
            with self.subTest(ending=ending):
                self.setUp()
                wake = event()

                def wait(delay):
                    self.assertTrue(self.memory.snapshots[-1]['poll_now']['waiting'])
                    if ending == 'request':
                        self.loop.request_poll()
                        self.assertTrue(wake.is_set())
                    elif ending == 'deadline':
                        self.now += 30
                    elif ending == 'cancel':
                        self.loop.stop_event.set()
                    else:
                        raise RuntimeError('Wait failed')
                    return wake.is_set()

                with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                        patch.object(wake, 'wait', side_effect=wait):
                    if ending == 'failure':
                        with self.assertRaisesRegex(RuntimeError, 'Wait failed'):
                            self.loop.poll_now.wait(self.loop.stop_event, 30)
                    else:
                        self.assertEqual(self.loop.poll_now.wait(self.loop.stop_event, 30), ending == 'cancel')
                self.assertFalse(self.memory.snapshots[-1]['poll_now']['waiting'])
                self.assertIsNone(self.loop.poll_now.waiter)

    def test_ending_waiter_does_not_clear_replacement_waiter_status(self):
        wake = threading.Event()
        replacement = (threading.Event(), threading.Event())

        def wait(delay):
            self.loop.poll_now.waiter = replacement
            self.loop.poll_now._status()
            self.loop.stop_event.set()
            return False

        with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                patch.object(wake, 'wait', side_effect=wait):
            self.assertTrue(self.loop.poll_now.wait(self.loop.stop_event, 30))
        self.assertIs(self.loop.poll_now.waiter, replacement)
        self.assertTrue(self.memory.snapshots[-1]['poll_now']['waiting'])
        self.loop.request_poll()
        self.assertTrue(replacement[1].is_set())

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

    def test_owned_rate_limit_keeps_actual_reset_and_rejects_polls_between_slices(self):
        lease = {'id': 'owned-run'}
        self.loop.github.lease = lease
        wake = threading.Event()
        # The read-only planning worker can be waiting while the owned client
        # waits for a rate limit. Requests must remain blocked between slices.
        self.loop.poll_now.waiter = (threading.Event(), wake)

        def protected():
            self.assertEqual(self.memory.snapshots[-1]['poll_now']['rate_limit_until'], iso(2800))
            self.loop.request_poll()
            self.assertFalse(wake.is_set())

        def deadline(current):
            self.assertIs(current, lease)
            protected()
            return self.now + 60  # Renewal keeps the lease live through the wait.

        def wait(delay):
            protected()
            self.now += delay

        with patch.object(self.loop.coordinator, 'deadline', side_effect=deadline), \
                patch.object(self.loop.interrupt_event, 'wait', side_effect=wait) as waits:
            self.loop.wait_rate_limit(GitHubError('GET', 'comments', 'rate limit',
                                                rate_limited=True, reset_at=2800), lease)
        self.assertEqual([call.args[0] for call in waits.call_args_list], [60] * 30)
        self.assertEqual(self.now, 2800)
        limits = [s['poll_now']['rate_limit_until'] for s in self.memory.snapshots if 'poll_now' in s]
        self.assertEqual(limits[0], iso(2800))
        self.assertTrue(all(value == iso(2800) for value in limits[:-1]))
        self.assertIsNone(limits[-1])
        self.assertEqual(self.loop.poll_now.next_allowed, 0)
        self.assertEqual(self.github.writes, [])

    def test_owned_rate_limit_status_clears_on_interruption_or_ownership_loss(self):
        for failure in (KeyboardInterrupt, LostOwnership):
            with self.subTest(failure=failure):
                with patch.object(self.loop.coordinator, 'deadline', side_effect=failure), \
                        self.assertRaises(failure):
                    self.loop.wait_rate_limit(GitHubError('GET', 'comments', 'rate limit',
                                                        rate_limited=True, reset_at=2800), {'id': 'owned-run'})
                self.assertEqual(self.loop.poll_now.limits, {})
                self.assertIsNone(self.memory.snapshots[-1]['poll_now']['rate_limit_until'])

    def test_cancelled_planning_waiter_does_not_replace_new_idle_waiter(self):
        idle = (threading.Event(), threading.Event())
        self.loop.poll_now.waiter = idle
        # Cancellation happens after the initial check, before acquiring the
        # lock where the main loop has already installed its next idle waiter.
        with patch.object(self.loop.stop_event, 'is_set', side_effect=(False, True)):
            self.assertTrue(self.loop.poll_now.wait(self.loop.stop_event, 30))
        self.assertIs(self.loop.poll_now.waiter, idle)
        self.loop.request_poll()
        self.assertTrue(idle[1].is_set())

    def test_poll_wait_checks_update_banner_once_per_second(self):
        wake = threading.Event()
        updates = []

        def wait(delay):
            self.now += 0.25
            return False

        with patch('ub_agents.poll_now.threading.Event', return_value=wake), \
                patch.object(wake, 'wait', side_effect=wait):
            self.loop.poll_now.wait(self.loop.stop_event, 2.5, lambda: updates.append(self.now))
        self.assertEqual(updates, [1001, 1002])
