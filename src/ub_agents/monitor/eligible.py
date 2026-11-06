"""Retain read-only queue planning for the monitor's next twenty items.

The launcher planner owns ordering and eligibility; shared observation rows own
grouping. Keep both discovery and the GitHub conditional cache between refreshes.
"""

import threading

from ..errors import AgentError
from ..loop import Loop
from ..observation_data import work_rows
from ..records import iso
from ..run_planning import ObservationReads
from .local import runtime_pauses

LIMIT = 20
PLAN_SECONDS = 300


class EligiblePlanning:
    def __init__(self, config, github, coordinator, clock):
        self.config, self.clock = config, clock
        self.github = ObservationReads(github, threading.Event())
        self.actor = coordinator.actor
        self.loop = None
        self.value = None
        self.read_at = None
        self.error = None

    def snapshot(self):
        now = self.clock()
        if self.read_at is not None and now - self.read_at < PLAN_SECONDS:
            return self.value
        try:
            self.refresh(now)
        except AgentError as exc:
            self.error = str(exc)
        return self.value

    def refresh(self, now):
        if self.loop is None:
            self.loop = Loop(self.config, self.github, self.actor, output=lambda *_: None)
            self.loop.coordinator.clock = self.clock
        self.loop.usage.pauses = runtime_pauses(self.config, now)
        plans = [self.plan_row(plan) for plan in self.loop.iter_plans()]
        rows = [row for row in work_rows({'latest_pass': {'rows': plans}}, self.config.root,
                                         plan_limit=len(plans)) if row.group == 'Eligible']
        self.value = {'count': len(rows), 'rows': [self.row(row, index) for index, row in enumerate(rows[:LIMIT])],
                      'interval_seconds': PLAN_SECONDS, 'generated_at': iso(now)}
        self.read_at = now
        self.error = None

    def plan_row(self, plan):
        priority = self.config.queue.priority
        return {'item': plan.item.number, 'kind': plan.item.kind, 'title': plan.item.title,
                'agent': plan.agent.name, 'state': plan.state, 'reason': plan.reason,
                'priority': plan.priority.rsplit(':', 1)[-1]
                if priority.labels and plan.priority and plan.priority != priority.default else None,
                'failures': plan.attempt - 1, 'max_attempts': plan.agent.max_attempts,
                'paused_clis': list(plan.paused_clis)}

    def row(self, row, index):
        path = 'pull' if row.data['kind'] == 'pr' else 'issues'
        waiting = row.state in {'backoff', 'waiting'}
        return {'item': {'number': row.item, 'kind': row.data['kind'], 'title': row.data['title'],
                         'url': f'https://github.com/{self.config.repository}/{path}/{row.item}'},
                'glyph': '◷' if waiting else '●',
                'state': row.state if waiting or index else 'next',
                'agents': [{'name': plan['agent'], 'failures': plan['failures'],
                            'max_attempts': plan['max_attempts']} for plan in row.eligible_plans],
                'priority': row.data['priority'],
                'pauses': {cli: iso(self.loop.usage.pauses[cli]) for cli in row.data['paused_clis']
                           if cli in self.loop.usage.pauses}}
