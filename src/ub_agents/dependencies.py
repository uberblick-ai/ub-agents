"""One observed open-issue graph for dependency waits and queue inheritance."""

from collections import deque


class Dependencies:
    def __init__(self, github, items, priority, milestones=()):
        issues = {i.number: i for i in items if i.kind == "issue" and i.state == "open"}
        self.blockers = {}
        local = {}
        for number, issue in issues.items():
            # Only a validated zero total proves there are no links to fetch.
            # Unknown summaries fall back to the full read; claims always reread.
            blockers = ([] if issue.total_blocked_by == 0 else
                        [b for b in github.blocked_by(number) if b.state == "open"])
            self.blockers[number] = tuple(dict.fromkeys(
                b.reference(github.repository) for b in
                sorted(blockers, key=lambda b: (b.repository.casefold(), b.number))))
            local[number] = {b.number for b in blockers
                             if b.repository.casefold() == github.repository.casefold()
                             and b.number in issues}
        own = {n: priority.rank(i.labels) for n, i in issues.items()}
        effective = self._inherit(own, local)
        self.priorities = {
            n: (priority.labels[rank] if rank < len(priority.labels) else None,
                source if rank < own[n] else None)
            for n, (rank, source) in effective.items()}
        ranks = {number: rank for rank, number in enumerate(milestones)}
        own = {n: ranks.get(i.milestone, len(ranks)) for n, i in issues.items()}
        self.milestones = {
            n: (milestones[rank], source) if rank < own[n] else (issues[n].milestone, None)
            for n, (rank, source) in self._inherit(own, local).items()}

    @staticmethod
    def _inherit(own, local):
        effective = {n: (rank, n) for n, rank in own.items()}
        # Propagate toward blockers. Only improving (rank, source) pairs enter
        # the worklist, so cycles terminate without recursion or depth limits.
        pending = deque(own)
        queued = set(own)
        while pending:
            dependent = pending.popleft()
            queued.remove(dependent)
            for blocker in local[dependent]:
                if effective[dependent] < effective[blocker]:
                    effective[blocker] = effective[dependent]
                    if blocker not in queued:
                        pending.append(blocker)
                        queued.add(blocker)
        return effective
