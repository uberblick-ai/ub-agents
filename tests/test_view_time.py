from datetime import datetime, timedelta, timezone
import unittest

from ub_agents.view_time import deadline, plan_pauses, remaining_time, runtime_pauses


class ViewTimeTests(unittest.TestCase):
    def test_remaining_time_rounds_up_and_keeps_two_nonzero_units(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        cases = ((1, '1m'), (60, '1m'), (61, '2m'), (12 * 60, '12m'),
                 (4 * 3600 + 12 * 60, '4h 12m'), (24 * 3600, '1d'),
                 (28 * 3600, '1d 4h'), (28 * 3600 + 1, '1d 5h'),
                 (24 * 3600 + 60, '1d 1m'), (24 * 3600 - 1, '1d'),
                 (48 * 3600 - 1, '2d'))
        for seconds, label in cases:
            with self.subTest(seconds=seconds):
                until = now + timedelta(seconds=seconds)
                self.assertEqual(remaining_time(until, now), label)
                self.assertEqual(remaining_time(until.astimezone(timezone(timedelta(hours=9))), now), label)

    def test_only_future_aware_deadlines_and_pause_held_plans_count(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        until = now + timedelta(minutes=35)
        for value in (None, {}, 'bad', now.isoformat(), (now - timedelta(seconds=1)).isoformat(),
                      until.replace(tzinfo=None).isoformat()):
            with self.subTest(value=value):
                self.assertIsNone(deadline(value, now))
        pauses = runtime_pauses({'runtime_pauses': {'claude': until.isoformat(), 'codex': 'bad'}}, now)
        self.assertEqual(pauses, {'claude': until})
        self.assertEqual(plan_pauses({'state': 'waiting', 'paused_clis': ['claude']}, pauses), pauses)
        for plan in ({'state': 'waiting'}, {'state': 'waiting', 'paused_clis': ['claude', 'codex']},
                     {'state': 'ready', 'paused_clis': ['claude']}, {'state': 'waiting', 'paused_clis': 'claude'}):
            self.assertEqual(plan_pauses(plan, pauses), {})
        self.assertEqual(runtime_pauses({'runtime_pauses': {'claude': until.isoformat()}}, until), {})
        self.assertEqual(runtime_pauses({'runtime_pauses': {'opencode': until.isoformat()}}, now), {'opencode': until})
