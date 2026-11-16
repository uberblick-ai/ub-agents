"""Shared local snapshot rows and history paths; no workflow authority."""

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from .attention import stamp
from .logs.log_format import inert, shorten
from .run_config import run_directory

WORK_GROUPS = ('Running', 'Needs attention', 'Eligible', 'Recent activity')
ELIGIBLE_LIMIT = 10
SNAPSHOT_BYTES = 64 * 1024


def read_json(path, limit=SNAPSHOT_BYTES):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Not a regular file')
        data = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise ValueError(f'File exceeds {limit} bytes')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def text(value, default='unavailable'):
    return shorten(inert(value)) if isinstance(value, str) else default


def mapping(value):
    return value if isinstance(value, dict) else {}


def rows(value, limit):
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


@dataclass(frozen=True)
class WorkRow:
    key: str
    group: str
    item: object
    agent: str
    state: str
    reason: str
    data: dict
    run: str | None = None
    runtime: str = 'unknown'
    log: Path | None = None
    context: Path | None = None
    hidden: bool = False
    eligible_plans: tuple[dict, ...] = ()
    attention_rows: tuple['WorkRow', ...] = ()

    def label(self):
        return f'#{text(str(self.item))} {self.state} · {self.agent}'

    def waiting_since(self):
        starts = [row.data.get('waiting_since') for row in self.attention_rows or (self,)]
        return min((start for start in starts if stamp(start) is not None), key=stamp, default=None)


def own_run(root, value):
    if isinstance(value, str) and value and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        return run_directory(root, value)
    return None


def runtime_type(value):
    # Leases publish cli:model:effort, while the #112 adapter takes the CLI.
    return text(value, 'unknown').split(':', 1)[0]


def plan_group(row):
    state = row.get('state')
    if state in {'ready', 'recover', 'backoff', 'waiting'}:
        return 'Eligible'
    if state == 'parked' and text(row.get('reason')).startswith(
            ('Waiting for blockers ', 'Waiting for active milestone #')):
        return None
    return 'Needs attention'


def omitted_plan(row):
    return row.get('state') == 'owned' or plan_group(row) is None


def newest_plans(latest, limit):
    started = stamp(latest.get('started_at'))
    plans = []
    for row in rows(latest.get('rows'), limit):
        observed = stamp(row.get('observed_at'))
        current = started is None or observed is None or observed >= started
        plans.append((row, current))
    current_items = {row.get('item') for row, current in plans if current}
    return [row for row, current in plans if current or row.get('item') not in current_items]


def work_plans(latest, assignment, limit):
    """Prefer Running, then the current pass, then Eligible for each item."""
    plans = [row for row in newest_plans(latest, limit)
             if not omitted_plan(row) and not (assignment and row.get('item') == assignment.get('item'))]
    eligible = {row.get('item') for row in plans if plan_group(row) == 'Eligible'}
    return [row for row in plans if plan_group(row) == 'Eligible' or row.get('item') not in eligible]


def work_rows(data, root, *, plan_limit=100):
    result = []
    assignment = mapping(data.get('assignment'))
    if assignment:
        run = own_run(root, assignment.get('run'))
        result.append(WorkRow('assignment:' + text(assignment.get('run'), 'claiming'), 'Running',
                              assignment.get('item', '?'), text(assignment.get('agent')), text(assignment.get('process')),
                              text(assignment.get('process_reason')), assignment, assignment.get('run'),
                              runtime_type(assignment.get('runtime')), run / 'process.log' if run else None,
                              run / 'context.json' if run else None))
    latest = mapping(data.get('latest_pass'))
    for row in work_plans(latest, assignment, plan_limit):
        owner = mapping(row.get('owner'))
        reason = (f'Owner: @{text(owner.get("actor"))} on {text(owner.get("host"))}' if owner else text(row.get('reason')))
        result.append(WorkRow(f'plan:{row.get("item")}:{text(row.get("agent"))}',
                              plan_group(row), row.get('item', '?'),
                              text(row.get('agent')), text(row.get('state')), reason, row))
    for index, row in enumerate(reversed(rows(data.get('outcomes'), 20))):
        run = own_run(root, row.get('run'))
        blockers = row.get('human_blocker')
        state = ('BLOCKED: ' + ', '.join(text(b) for b in blockers[:10]) if isinstance(blockers, list) and blockers else
                 text(row.get('result')) + ' · ' + text(row.get('acceptance')))
        result.append(WorkRow('outcome:' + text(row.get('run'), str(index)), 'Recent activity', row.get('item', '?'),
                              text(row.get('agent')), state, text(row.get('summary')), row, row.get('run'),
                              runtime_type(row.get('runtime')), run / 'process.log' if run else None, run / 'context.json' if run else None))
    histories = mapping(data.get('histories'))
    result = [replace(row, data=row.data | {'history': histories[row.data.get('history_key', str(row.item))]})
              if row.data.get('history_key', str(row.item)) in histories else row for row in result]
    ordered = sorted(result, key=lambda row: (WORK_GROUPS.index(row.group),
                                             row.state in {'backoff', 'waiting'}))
    grouped, eligible, attention = [], {}, {}
    for row in ordered:
        if row.group == 'Eligible':
            if row.item in eligible:
                index = eligible[row.item]
                first = grouped[index]
                grouped[index] = replace(first, eligible_plans=(*first.eligible_plans, row.data))
                continue
            eligible[row.item] = len(grouped)
            row = replace(row, key=f'plan:{row.item}', eligible_plans=(row.data,))
        elif row.group == 'Needs attention':
            if row.item in attention:
                index = attention[row.item]
                first = grouped[index]
                if row not in first.attention_rows:
                    grouped[index] = replace(first, attention_rows=(*first.attention_rows, row))
                continue
            attention[row.item] = len(grouped)
            row = replace(row, key=f'plan:{row.item}:attention', attention_rows=(row,))
        grouped.append(row)
    return grouped


def history_path(snapshot, key):
    # Hash the logical key: carried rows can have agent names in their keys.
    return snapshot.with_suffix('.histories') / (sha256(key.encode('utf-8')).hexdigest() + '.json')
