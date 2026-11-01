from dataclasses import replace
import unittest

from ub_agents.config import Priority
from ub_agents.coordination import Plan
from ub_agents.queue_order import milestone_diagnostics, milestone_plans, strict_milestone_plans
from tests.support import issue


def plan(number, milestone):
    return Plan(issue(number, milestone=milestone), None, None, "ready", "", 1,
                milestone=milestone, milestone_rank=milestone)


class QueueOrderTests(unittest.TestCase):
    def test_gate_diagnostics_follow_the_approved_head_without_reaching_later_plans(self):
        events = []
        skipped = ("Milestone #10 has no eligible issues for this launcher",)

        def approve(candidate):
            events.append(("approve", candidate.item.number))
            return replace(candidate, state="parked") if candidate.item.number == 1 else candidate

        ordered = milestone_plans([plan(1, 10), plan(2, 20), plan(3, 20)], (10, 20), Priority(),
                                  approve=approve,
                                  on_wait=lambda value: events.append(("wait", value)),
                                  on_skips=lambda value: events.append(("skips", value)))
        self.assertEqual(events, [])
        self.assertEqual((next(ordered).item.number, events), (1, [("approve", 1)]))
        self.assertEqual(next(ordered).item.number, 2)
        self.assertEqual(events, [("approve", 1), ("approve", 2), ("wait", None),
                                  ("skips", skipped), ("approve", 2)])
        ordered.close()
        self.assertNotIn(("approve", 3), events)

    def test_strict_wait_is_published_only_on_exhaustion(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust):
                events = []

                def approve(candidate):
                    events.append(("approve", candidate.item.number))
                    return replace(candidate, state="parked")

                ordered = strict_milestone_plans(iter([plan(1, 10), plan(2, 20)]), {10: "v10", 20: "v20"},
                                                approve=approve,
                                                on_wait=lambda value: events.append(("wait", value)))
                self.assertEqual(events, [])
                self.assertEqual(next(ordered).state, "parked")
                self.assertEqual(next(ordered).reason, "Waiting for active milestone #10")
                self.assertEqual(events, [("approve", 1)])
                if exhaust:
                    with self.assertRaises(StopIteration):
                        next(ordered)
                    self.assertEqual(events, [("approve", 1),
                                              ("wait", "Waiting for milestone #10 (v10) to close: no eligible issues")])
                else:
                    ordered.close()
                    self.assertEqual(events, [("approve", 1)])

    def test_diagnostics_clear_wait_before_reading_plans_and_publish_skips_afterward(self):
        events = []

        def plans():
            events.append(("read", 1))
            yield plan(1, 20)

        milestone_diagnostics(plans(), (10, 20),
                              on_wait=lambda value: events.append(("wait", value)),
                              on_skips=lambda value: events.append(("skips", value)))
        self.assertEqual(events, [("wait", None), ("read", 1),
                                  ("skips", ("Milestone #10 has no eligible issues for this launcher",))])
