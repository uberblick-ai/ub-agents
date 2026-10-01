"""One observed open-issue graph for dependency gates and priority inheritance."""

from collections import deque


class Dependencies:
    def __init__(self, github, items, priority):
        issues = {i.number: i for i in items if i.kind == "issue" and i.state == "open"}
        self.blockers = {}
        local = {}
        for number in issues:
            blockers = [b for b in github.blocked_by(number) if b.state == "open"]
            self.blockers[number] = tuple(sorted(
                {b.reference(github.repository) for b in blockers}))
            local[number] = {b.number for b in blockers
                             if b.repository.casefold() == github.repository.casefold()
                             and b.number in issues}
        own = {n: priority.rank(i.labels) for n, i in issues.items()}
        effective = {n: (rank, n) for n, rank in own.items()}
        # Propagate toward blockers. Only improving (rank, source) pairs enter
        # the worklist, so cycles terminate without recursion or depth limits.
        pending = deque(issues)
        queued = set(issues)
        while pending:
            dependent = pending.popleft()
            queued.remove(dependent)
            for blocker in local[dependent]:
                if effective[dependent] < effective[blocker]:
                    effective[blocker] = effective[dependent]
                    if blocker not in queued:
                        pending.append(blocker)
                        queued.add(blocker)
        self.priorities = {
            n: (priority.labels[rank] if rank < len(priority.labels) else None,
                source if rank < own[n] else None)
            for n, (rank, source) in effective.items()}
