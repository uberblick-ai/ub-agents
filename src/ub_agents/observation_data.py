"""Shared local snapshot rows and history paths; no workflow authority."""

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

from .attention import stamp
from .log_format import inert, shorten
from .run_config import run_directory

WORK_GROUPS = ('Running', 'Needs attention', 'Eligible', 'Recent activity')
ELIGIBLE_LIMIT = 10


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


def work_rows(data, root):  # noqa: C901
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
    for row in rows(latest.get('rows'), 100):
        if omitted_plan(row):
            continue
        if assignment and (row.get('item'), row.get('agent')) == (assignment.get('item'), assignment.get('agent')):
            continue
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
