"""Order queue plans by priority and milestone without advancing approval early."""

from collections import deque
from dataclasses import replace

from .records import seconds


def rank(plan, priority):
    existing = plan.item.kind == "pr" or plan.state in {"owned", "recover"}
    return (priority.labels.index(plan.priority) if plan.priority is not None else len(priority.labels),
            0 if existing else 1,
            0 if existing else plan.milestone_rank,
            seconds(plan.item.created_at), plan.item.number)


def milestone_plans(plans, milestones, priority, *, on_wait, on_skips, approve=None):  # noqa: C901
    # Eligibility must be known before choosing a milestone. Merge the
    # milestone-first queue with unlisted issues one choice at a time: the
    # exception compares against the next eligible milestone issue, not a
    # milestone's highest priority before unavailable work is filtered out.
    plan_rank = lambda plan: rank(plan, priority)
    assigned, unlisted, other = [], [], []
    for plan in plans:
        if plan.item.kind == "issue" and plan.state == "ready":
            (assigned if plan.milestone in milestones else unlisted).append(plan)
        else:
            other.append(plan)
    assigned = deque(sorted(assigned, key=lambda plan: (plan.milestone_rank, plan_rank(plan))))
    unlisted = deque(sorted(unlisted, key=plan_rank))
    other = deque(sorted(other, key=plan_rank))
    evaluate = approve or (lambda plan: plan)

    def approved_head():
        while assigned:
            assigned[0] = evaluate(assigned[0])
            if assigned[0].state == "ready":
                return assigned[0]
            yield assigned.popleft()
        return None

    # Both priority exceptions need the next approved milestone issue, even
    # when an unlisted issue or existing work will ultimately go first.
    head = yield from approved_head()
    if approve is not None:
        milestone_diagnostics((head,) if head else (), milestones, on_wait=on_wait, on_skips=on_skips)
    while assigned or unlisted:
        queue = (unlisted if unlisted and
                 (not assigned or plan_rank(unlisted[0])[0] < plan_rank(assigned[0])[0]) else assigned)
        next_issue = evaluate(queue.popleft())
        if next_issue.state != "ready":
            yield next_issue
            continue
        # Existing work keeps priority order and wins equal-priority ties
        # against the new issue selected by the milestone policy.
        while other and plan_rank(other[0])[0] <= plan_rank(next_issue)[0]:
            yield evaluate(other.popleft())
        yield next_issue
        if queue is assigned:
            yield from approved_head()
    for plan in other:
        yield evaluate(plan)


def milestone_diagnostics(plans=(), milestones=(), *, on_wait, on_skips):
    on_wait(None)
    eligible = {p.milestone for p in plans if p.item.kind == "issue" and p.state == "ready"}
    first = next((rank for rank, number in enumerate(milestones) if number in eligible), len(milestones))
    on_skips(tuple(f"Milestone #{number} has no eligible issues for this launcher"
                   for number in milestones[:first]))


def strict_milestone_plans(plans, milestones, *, on_wait, approve=None):
    active = next(iter(milestones), None)
    eligible = False
    for plan in plans:
        if (active is not None and plan.item.kind == "issue"
                and plan.state == "ready" and plan.milestone != active):
            plan = replace(plan, state="parked", runtime=None,
                           reason=f"Waiting for active milestone #{active}", approval_gate=None)
        elif approve is not None:
            plan = approve(plan)
        if plan.item.kind == "issue" and plan.state == "ready":
            eligible = True
        yield plan
    if active is not None and not eligible:
        on_wait(f"Waiting for milestone #{active} ({milestones[active]}) "
                "to close: no eligible issues")
