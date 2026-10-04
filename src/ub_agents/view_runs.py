"""Read-only, per-item Runs table from the local session snapshot."""

from datetime import datetime
import socket

from rich.console import Group
from rich.table import Table
from rich.text import Text

from .view_data import item_history, mapping, rows, text
from .view_theme import theme_style

SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'


def moment(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def relative_time(value, now=None):
    stamp = moment(value)
    if stamp is None:
        return 'unknown'
    local = stamp.astimezone(now.tzinfo) if now is not None else stamp.astimezone()
    now = now or datetime.now().astimezone()
    age = max(0, now.timestamp() - stamp.timestamp())
    if age < 60:
        return 'just now'
    if age < 3600:
        return f'{int(age // 60)} min ago'
    if age < 86400:
        return f'{int(age // 3600)} h ago'
    days = (now.date() - local.date()).days
    if days <= 1:
        return 'yesterday'
    if days < 7:
        return f'{days} days ago'
    return local.strftime('%Y-%m-%d')


def run_host(value, local_host=None, *, muted='dim'):
    local_host = local_host or socket.gethostname()
    if isinstance(value, str) and value:
        if value.rstrip('.').casefold() == local_host.rstrip('.').casefold():
            return Text('this machine')
        name = text(value).split('.', 1)[0]
    else:
        name = 'unknown'
    host = Text(name, style=muted)
    host.truncate(14, overflow='ellipsis')
    return host


def run_status(row, now):
    result = row.get('result')
    expires = moment(row.get('expires'))
    expired = expires is not None and expires.timestamp() <= now.timestamp()
    if row.get('rejection') or result in {'retry', 'blocked'}:
        return 'failed', result or 'blocked'
    if result == 'success':
        return 'success', 'success'
    if row.get('state') == 'withdrawn' or expired or row.get('state') == 'released':
        return 'failed', 'abandoned' if row.get('state') == 'withdrawn' or expired else 'released'
    return 'running', text(row.get('state'), 'running')


def runs_view(row, session, now=None, *, app=None):
    # The plain projection remains usable without a running Textual app.
    muted = theme_style(app, 'view-muted', dim=True) if app else 'dim'
    success = theme_style(app, 'view-success') if app else ''
    error = theme_style(app, 'view-error') if app else ''
    if row is None:
        return Text('Select an item to see its history.')
    relative_now = now
    now = now or datetime.now().astimezone()
    history = item_history(row, session)
    runs = rows(history.get('runs'), 20)
    omitted = history.get('omitted_runs', 0)
    omitted = omitted if type(omitted) is int and omitted > 0 else 0
    filing = mapping(history.get('filing'))
    filed = isinstance(filing.get('author'), str) and bool(filing['author']) and moment(filing.get('time')) is not None
    details = []
    if history.get('kind') == 'pr' and type(history.get('closes')) is int:
        details.append(f'closes #{history["closes"]}')
    if filed:
        details.append('filed by ' + text(filing['author']))
    details.append(f'{len(runs) + omitted} runs')
    subtitle = Text(' · '.join(details), style=muted)
    if not filed and not runs and not omitted:
        return Group(subtitle, Text('No item history cached.'))
    table = Table(box=None, padding=(0, 1), pad_edge=False, expand=True, header_style=muted)
    table.add_column('when', width=11, no_wrap=True)
    table.add_column('result', width=6, no_wrap=True)
    table.add_column('agent · summary', ratio=1, min_width=12, no_wrap=True, overflow='ellipsis')
    table.add_column('where', width=14, no_wrap=True)
    table.add_column('outcome', min_width=12, max_width=24, overflow='fold')
    if filed:
        table.add_row(relative_time(filing['time'], relative_now), Text('✓', style=success),
                      Text('filed by ' + text(filing['author'])), 'GitHub', 'filed')
    frame = SPINNER[int(now.timestamp() * 10) % len(SPINNER)]
    for run in runs:
        status, label = run_status(run, now)
        glyph = Text('✓', style=success) if status == 'success' else Text('✗', style=error) if status == 'failed' else Text(frame)
        summary = text(run.get('agent'))
        if run.get('summary'):
            summary += ' · ' + text(run['summary'])
        outcome = text(run.get('outcome'), label)
        if run.get('acceptance'):
            outcome += ' · ' + text(run['acceptance'])
        blockers = run.get('human_blocker')
        if isinstance(blockers, list) and blockers:
            outcome += ' · BLOCKED: ' + ', '.join(text(b) for b in blockers[:10])
        table.add_row(relative_time(run.get('time'), relative_now), glyph, Text(summary),
                      run_host(run.get('host'), muted=muted), Text(outcome))
    tail = [Text(f'{omitted} earlier runs omitted.', style=muted)] if omitted else []
    return Group(subtitle, table, *tail)
