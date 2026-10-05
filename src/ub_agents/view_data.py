"""Bounded local inputs for the development view; no workflow authority."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import time

from .log_format import CONTROLS, inert, shorten

SNAPSHOT_BYTES = 64 * 1024
CONTEXT_BYTES = 256 * 1024
SESSION_LIMIT = 256
DESCRIPTION_LIMIT = 2048
WORK_GROUPS = ('Running', 'Needs attention', 'Eligible', 'Recent activity')


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
        if type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('Missing or unsupported snapshot version')
        for key in ('assignment', 'latest_pass', 'activity', 'omitted', 'histories', 'action_needed', 'coordination_authors'):
            if data.get(key) is not None and not isinstance(data[key], dict):
                raise ValueError(f'Invalid {key}')
        if 'outcomes' in data and not isinstance(data['outcomes'], list):
            raise ValueError('Invalid outcomes')
        if mapping(data.get('latest_pass')).get('rows') is not None and not isinstance(data['latest_pass']['rows'], list):
            raise ValueError('Invalid pass rows')
        groups = [data.get('outcomes', []), mapping(data.get('latest_pass')).get('rows', [])]
        for history in mapping(data.get('histories')).values():
            if not isinstance(history, dict) or not isinstance(history.get('runs'), list):
                raise ValueError('Invalid item history')
            if history.get('filing') is not None and not isinstance(history['filing'], dict):
                raise ValueError('Invalid filing data')
            if type(history.get('omitted_runs', 0)) is not int or history.get('omitted_runs', 0) < 0:
                raise ValueError('Invalid omitted runs')
            groups.extend(([history], history['runs']))
        if data.get('assignment'):
            groups.append([data['assignment']])
        for group in groups:
            for row in group:
                if not isinstance(row, dict):
                    raise ValueError('Invalid row')
                if 'item' in row and (type(row['item']) is not int or row['item'] < 1):
                    raise ValueError('Invalid item number')
                for field in ('agent', 'run', 'runtime', 'state', 'reason', 'process', 'process_reason', 'title', 'summary',
                              'result', 'outcome', 'host', 'time', 'expires', 'acceptance', 'history_key'):
                    if row.get(field) is not None and not isinstance(row[field], str):
                        raise ValueError(f'Invalid row {field}')
                for field in ('owner', 'description'):
                    if row.get(field) is not None and not isinstance(row[field], dict):
                        raise ValueError(f'Invalid row {field}')
                for field in ('attempt', 'failures', 'max_attempts', 'handoff'):
                    value = row.get(field)
                    minimum = 0 if field == 'failures' else 1
                    if value is not None and (type(value) is not int or value < minimum):
                        raise ValueError(f'Invalid row {field}')
                blockers = row.get('human_blocker')
                if blockers is not None and (not isinstance(blockers, list) or not all(isinstance(b, str) for b in blockers)):
                    raise ValueError('Invalid human blockers')
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
    hidden: bool = False

    def label(self):
        return f'#{text(str(self.item))} {self.state} · {self.agent}'


def own_run(root, value):
    if isinstance(value, str) and value and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        return root / '.ub-agents' / 'runs' / value
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


def outcomes_today(session, now=None):
    today = (now or datetime.now().astimezone()).date()
    local_zone = now.tzinfo if now else None
    count = 0
    for row in rows(session.data.get('outcomes'), 20):
        try:
            stamp = datetime.fromisoformat(row['time'].replace('Z', '+00:00'))
            if stamp.tzinfo is not None and stamp.astimezone(local_zone).date() == today:
                count += 1
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            pass
    return count


def work_rows(session, root):
    data = session.data
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
        group = plan_group(row)
        if row.get('state') == 'owned' or group is None:
            continue
        if assignment and (row.get('item'), row.get('agent')) == (assignment.get('item'), assignment.get('agent')):
            continue
        owner = mapping(row.get('owner'))
        reason = (f'Owner: @{text(owner.get("actor"))} on {text(owner.get("host"))}' if owner else text(row.get('reason')))
        result.append(WorkRow(f'plan:{row.get("item")}:{text(row.get("agent"))}',
                              group, row.get('item', '?'),
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
    return sorted(result, key=lambda row: (WORK_GROUPS.index(row.group),
                                          row.state in {'backoff', 'waiting'}))


def item_history(row, session):
    if not row:
        return {}
    history = mapping(row.data.get('history'))
    return history or (mapping(mapping(session.data.get('histories')).get(str(row.item))) if session else {})


@dataclass(frozen=True)
class Description:
    title: str = ''
    body: str = ''
    source: str = ''
    observed_at: float | None = None
    available: bool = False
    notice: str = ''
    error: str = ''
    kind: str = ''

    def __post_init__(self):
        notices = [self.notice] if self.notice else []
        for field, label in (('title', 'Title'), ('body', 'Description')):
            value = description_text(getattr(self, field))
            if len(value) > DESCRIPTION_LIMIT:
                notices.append(f'{label} shortened to 2,048 characters.')
            object.__setattr__(self, field, value[:DESCRIPTION_LIMIT])
        error = inert(self.error)
        if len(error) > DESCRIPTION_LIMIT:
            notices.append('Description load error shortened to 2,048 characters.')
        object.__setattr__(self, 'error', error[:DESCRIPTION_LIMIT])
        object.__setattr__(self, 'notice', '\n'.join(notices))

    def details(self, now=None):
        """Plain status and provenance, kept outside the Markdown body."""
        age = f'{max(0, (time.time() if now is None else now) - self.observed_at):.0f}s old' if self.observed_at is not None else 'age unavailable'
        lines = [] if self.available else ['Description unavailable (not cached).']
        if self.error:
            lines = ['Description load failed: ' + self.error]
        if self.source:
            lines.append(f'Source: {self.source} · {age}')
        if self.notice:
            lines.append(self.notice)
        return '\n'.join(lines)

    def display(self, now=None):
        body = self.body if self.available and not self.error else ''
        return '\n'.join(part for part in (body, self.details(now)) if part)


def description_text(value):
    """Keep description line breaks and tabs; escape all other controls."""
    if not isinstance(value, str):
        return ''
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return CONTROLS.sub(lambda match: match.group() if match.group() in '\n\t' else inert(match.group()), value)


def local_description(row, session):
    if not row:
        return Description()
    source = row.data
    if not row.key.startswith('plan:'):
        source = next((r for r in rows(mapping(session.data.get('latest_pass')).get('rows'), 100)
                       if r.get('item') == row.item), source)
    description = mapping(source.get('description'))
    title = source.get('title')
    if description.get('available') is True and isinstance(description.get('text'), str):
        age = session.age()
        stamp = time.time() - age if age is not None else None
        notice = 'Description shortened in snapshot.' if description.get('omitted_characters') else ''
        return Description(title, description['text'], 'snapshot', stamp, True, notice, kind=source.get('kind', ''))
    context_path = row.context
    if context_path is None:
        # A plan row can refer to the session's current or earlier own run.
        candidates = [mapping(session.data.get('assignment')),
                      *reversed(rows(session.data.get('outcomes'), 20))]
        run = next((own_run(session.path.parents[2], candidate.get('run'))
                    for candidate in candidates if candidate.get('item') == row.item and
                    own_run(session.path.parents[2], candidate.get('run'))), None)
        context_path = run / 'context.json' if run else None
    if context_path:
        try:
            context = read_json(context_path, CONTEXT_BYTES)
            if isinstance(context.get('body'), str):
                return Description(context.get('title') or title, context['body'], 'run context.json',
                                   context_path.stat().st_mtime, True, kind=context.get('kind') or source.get('kind', ''))
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            return Description(title=title, notice='Cached context unavailable: ' + text(str(exc)))
    return Description(title=title, kind=source.get('kind', ''))


def item_handoff(row, session=None):
    """The linked PR for an outcome, or the item's latest cached handoff."""
    if not row:
        return None
    outcomes = [row.data, *(rows(session.data.get('outcomes'), 20) if session else [])]
    for outcome in reversed(outcomes):
        if outcome.get('item') != row.item:
            continue
        handoff = outcome.get('handoff') or outcome.get('target')
        if type(handoff) is int and handoff > 0 and handoff != row.item:
            return handoff
    return None


def item_header(row, description, session):
    """Shared item identity and optional context for every right-pane tab."""
    if not row:
        return 'No item selected.', ''
    source = next((r for r in rows(mapping(session.data.get('latest_pass')).get('rows'), 100)
                   if r.get('item') == row.item), {}) if session else {}
    history = item_history(row, session)
    kind = (row.data.get('kind') or source.get('kind') or (description.kind if description else '') or
            history.get('kind'))
    reference = ('⌥' if kind == 'pr' else '#') + str(row.item)
    title = text(description.title if description and description.title else
                 row.data.get('title') or source.get('title') or history.get('title'), '')
    parts = [text(row.data.get('agent'), ''), text(row.data.get('runtime'), '').replace(':', ' ')]
    if row.key.startswith('assignment:') and type(row.data.get('attempt')) is int:
        parts.append(f'attempt {row.data["attempt"]}')
    elif type(row.data.get('failures')) is int and type(row.data.get('max_attempts')) is int:
        parts.append(f'{row.data["failures"]}/{row.data["max_attempts"]} failures')
    handoff = item_handoff(row, session)
    if handoff is not None:
        parts.append(f'⌥{handoff}')
    return ' '.join(part for part in (reference, title) if part), ' · '.join(part for part in parts if part)


def run_status(row, session):
    """Keep process/plan state separate from explicitly reported outcomes."""
    if not row:
        if session and not mapping(session.data.get('assignment')):
            return '○ Idle · waiting for the next poll', '', False
        return 'No item selected.', '', False
    outcomes = [outcome for outcome in rows(session.data.get('outcomes'), 20)
                if outcome.get('item') == row.item] if session else []
    run = row.data.get('recovered_run') or row.run
    outcome = next((outcome for outcome in reversed(outcomes) if run and outcome.get('run') == run), None)
    if row.group == 'Recent activity':
        outcome = row.data
    state = 'exited' if row.group == 'Recent activity' else row.state
    assignment = mapping(session.data.get('assignment')) if session else {}
    if run and (row.item, row.data.get('agent'), run) == (
            assignment.get('item'), assignment.get('agent'), assignment.get('recovered_run') or assignment.get('run')):
        if row.key.startswith('assignment:') and mapping(session.data.get('activity')).get('state') == 'stopping':
            return '■ Stopping after this run (SIGTERM) · no new claims', '', False
        state = text(assignment.get('process'), state)
    left = ' '.join(part for part in (text(row.data.get('agent'), ''), state) if part)
    report = 'no outcome reported'
    if outcome:
        result, acceptance = text(outcome.get('result'), ''), text(outcome.get('acceptance'), '')
        report = 'reported ' + result if result else 'outcome reported'
        if acceptance:
            report += ' (' + acceptance + ')'
    earlier = sum(not run or other.get('run') != run for other in outcomes)
    right = f'{earlier} earlier run' + ('s' if earlier != 1 else '') if earlier else ''
    return left + ' · ' + report, right, state == 'running'


def context_header(row, description):
    if not row:
        return 'No item selected.'
    return f'{row.state}\n{row.reason}'


def context_text(row, description):
    return context_header(row, description) + ('\n\n' + description.display() if row else '')


def item_context(row, session):
    return context_text(row, local_description(row, session))


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
