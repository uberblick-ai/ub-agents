"""Bounded local inputs for the development view; no workflow authority."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat

from .log_format import inert, shorten

SNAPSHOT_BYTES = 64 * 1024
CONTEXT_BYTES = 256 * 1024
SESSION_LIMIT = 256


def read_json(path, limit=SNAPSHOT_BYTES):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Not a regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
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
class Session:
    path: Path
    data: dict
    error: str | None = None

    def age(self, now=None):
        try:
            stamp = datetime.fromisoformat(self.data['published_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return None
            return max(0, (now or datetime.now(timezone.utc)).timestamp() - stamp.timestamp())
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return None

    def state(self, now=None):
        if self.error:
            return 'malformed'
        if self.data.get('ended') is True:
            return 'ended'
        age = self.age(now)
        if age is None or age > 30:
            return 'stale'
        return 'live' if mapping(self.data.get('assignment')) else 'idle'

    def freshness(self):
        age = self.age()
        return f'{self.state()} · snapshot {age:.0f}s old' if age is not None else f'{self.state()} · freshness unavailable'


def load_session(path):
    try:
        data = read_json(path)
        if data.get('version') != 1:
            raise ValueError('Missing or unsupported snapshot version')
        for key in ('assignment', 'latest_pass', 'activity', 'omitted'):
            if data.get(key) is not None and not isinstance(data[key], dict):
                raise ValueError(f'Invalid {key}')
        if 'outcomes' in data and not isinstance(data['outcomes'], list):
            raise ValueError('Invalid outcomes')
        if mapping(data.get('latest_pass')).get('rows') is not None and not isinstance(data['latest_pass']['rows'], list):
            raise ValueError('Invalid pass rows')
        return Session(path, data)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        return Session(path, {}, text(str(exc)))


def choose_session(root, session_id=None):
    directory = root / '.ub-agents' / 'sessions'
    if session_id is not None:
        if not session_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in session_id):
            raise ValueError('Session ID must be a file stem, without a path')
        return directory / f'{session_id}.json', []
    sessions = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith('.json'):
                    if len(sessions) == SESSION_LIMIT:
                        truncated = True
                        break
                    sessions.append(load_session(Path(entry.path)))
    except FileNotFoundError:
        pass
    live = [session for session in sessions if session.state() in {'live', 'idle'}]
    if len(live) == 1 and not truncated:
        return live[0].path, []
    listing = [f'{s.path.stem}  {s.state()}  {text(s.data.get("actor"))}  {text(s.data.get("repository"))}'
               for s in sorted(sessions, key=lambda s: s.path.name)]
    if truncated:
        listing.append(f'Listing limited to {SESSION_LIMIT} files; specify a session ID.')
    return None, listing


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

    def label(self):
        return f'#{self.item} {self.agent} · {self.state}\n{text(self.reason, "")} '


def own_run(root, value):
    if isinstance(value, str) and value and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        return root / '.ub-agents' / 'runs' / value
    return None


def runtime_type(value):
    # Leases publish cli:model:effort, while the #112 adapter takes the CLI.
    return text(value, 'unknown').split(':', 1)[0]


def work_rows(session, root):
    data = session.data
    result = []
    assignment = mapping(data.get('assignment'))
    if assignment:
        run = own_run(root, assignment.get('run'))
        result.append(WorkRow('assignment:' + text(assignment.get('run'), 'claiming'), 'Current assignment',
                              assignment.get('item', '?'), text(assignment.get('agent')), text(assignment.get('process')),
                              text(assignment.get('process_reason')), assignment, assignment.get('run'),
                              runtime_type(assignment.get('runtime')), run / 'process.log' if run else None,
                              run / 'context.json' if run else None))
    latest = mapping(data.get('latest_pass'))
    for index, row in enumerate(rows(latest.get('rows'), 100)):
        owner = mapping(row.get('owner'))
        reason = (f'Owner: @{text(owner.get("actor"))} on {text(owner.get("host"))}' if owner else text(row.get('reason')))
        result.append(WorkRow(f'plan:{row.get("item")}:{text(row.get("agent"))}:{index}',
                              'Latest pass (' + text(latest.get('state'), 'partial') + ')', row.get('item', '?'),
                              text(row.get('agent')), text(row.get('state')), reason, row))
    for index, row in enumerate(reversed(rows(data.get('outcomes'), 20))):
        run = own_run(root, row.get('run'))
        blockers = row.get('human_blocker')
        state = ('BLOCKED: ' + ', '.join(text(b) for b in blockers[:10]) if isinstance(blockers, list) and blockers else
                 text(row.get('result')) + ' · ' + text(row.get('acceptance')))
        result.append(WorkRow('outcome:' + text(row.get('run'), str(index)), 'Recent outcomes', row.get('item', '?'),
                              text(row.get('agent')), state, text(row.get('summary')), row, row.get('run'),
                              'unknown', run / 'process.log' if run else None, run / 'context.json' if run else None))
    return result


def item_context(row, session):
    if not row:
        return 'No item selected.'
    source = row.data
    if row.group == 'Current assignment':
        source = next((r for r in rows(mapping(session.data.get('latest_pass')).get('rows'), 100)
                       if r.get('item') == row.item), source)
    description = mapping(source.get('description'))
    title = text(source.get('title'), '')
    body = text(description.get('text'), '') if description.get('available') is True else ''
    notice = ''
    if description.get('omitted_characters'):
        notice = '\nDescription shortened in snapshot; full cached context may be available in context.json.'
    if not body and row.context:
        try:
            context = read_json(row.context, CONTEXT_BYTES)
            title, body = text(context.get('title'), title), text(context.get('body'), '')
            notice = '\nSource: run context.json (bounded local read).'
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            notice = '\nCached context unavailable: ' + text(str(exc))
    return f'#{row.item} {title}\n{row.state}\n{row.reason}\n\n{body or "Description unavailable (not cached)."}{notice}'


def outcome_text(session):
    result = []
    for row in reversed(rows(session.data.get('outcomes'), 20)):
        blockers = row.get('human_blocker')
        waiting = ' · BLOCKED: ' + ', '.join(text(b) for b in blockers[:10]) if isinstance(blockers, list) and blockers else ''
        result.append(f'#{row.get("item", "?")} {text(row.get("agent"))} · {text(row.get("result"))} · '
                      f'{text(row.get("acceptance"))}{waiting}\n{ text(row.get("time"))} · run {text(row.get("run"))}\n'
                      f'{text(row.get("summary"))}')
    omitted = mapping(session.data.get('omitted')).get('outcomes', 0)
    value = '\n\n'.join(result) or 'No session outcomes cached.'
    return value + f'\n\nOmitted outcomes: {omitted}' if omitted else value
