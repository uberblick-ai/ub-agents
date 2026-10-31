"""Read-only, per-item Runs table from local history files."""

from datetime import datetime
import socket

from rich.console import Group
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from .denials import denial_count
from .run_usage import short_tokens, usage_fields
from .view_data import item_history, mapping, text
from .view_spinner import spinner_frame
from .view_theme import theme_style


class RunOutcome:
    """Shorten the outcome at the cell width while keeping its denial suffix."""

    def __init__(self, value, count):
        self.value = Text(value, no_wrap=True, overflow='ellipsis')
        self.suffix = Text(f' · {count} denied' if count else '')

    def __rich_measure__(self, console, options):
        return Measurement(1 + self.suffix.cell_len, self.value.cell_len + self.suffix.cell_len)

    def __rich_console__(self, console, options):
        value = self.value.copy()
        value.truncate(max(0, options.max_width - self.suffix.cell_len), overflow='ellipsis')
        yield value + self.suffix


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
            return Text('this machine', no_wrap=True, overflow='ellipsis')
        name = text(value).split('.', 1)[0]
    else:
        name = 'unknown'
    host = Text(name, style=muted, no_wrap=True, overflow='ellipsis')
    host.truncate(14, overflow='ellipsis')
    return host


def run_status(row, now):
    result = row.get('result')
    expires = moment(row.get('expires'))
    expired = expires is not None and expires.timestamp() <= now.timestamp()
    if row.get('rejection') or result in {'retry', 'blocked'}:
        return 'failed', result or 'blocked'
    if row.get('acceptance') == 'unaccepted':
        if row.get('state') in {'claiming', 'running'} and expires is not None and not expired:
            return 'running', text(row.get('state'), 'running')
        return 'failed', result or 'blocked'
    if result == 'success':
        return 'success', 'success'
    if row.get('state') == 'withdrawn' or expired or row.get('state') == 'released':
        return 'failed', 'abandoned' if row.get('state') == 'withdrawn' or expired else 'released'
    return 'running', text(row.get('state'), 'running')


def runs_view(row, session, now=None, *, app=None, history=None):  # noqa: C901
    # The plain projection remains usable without a running Textual app.
    muted = theme_style(app, 'view-muted', dim=True)
    success = theme_style(app, 'view-success')
    error = theme_style(app, 'view-error')
    if row is None:
        return Text('Select an item to see its history.')
    relative_now = now
    now = now or datetime.now().astimezone()
    if history is None:
        history = item_history(row, session, full=True)
    runs = [run for run in history.get('runs', []) if isinstance(run, dict)]
    if history.get('error'):
        return Text(history['error'])
    filing = mapping(history.get('filing'))
    filed = isinstance(filing.get('author'), str) and bool(filing['author']) and moment(filing.get('time')) is not None
    details = []
    if history.get('kind') == 'pr' and type(history.get('closes')) is int:
        details.append(f'closes #{history["closes"]}')
    if filed:
        details.append('filed by ' + text(filing['author']))
    details.append(f'{len(runs)} runs')
    subtitle = Text(' · '.join(details), style=muted, no_wrap=True, overflow='ellipsis')
    if not filed and not runs:
        return Group(subtitle, Text('No item history cached.'))
    table = Table(box=None, padding=(0, 1), pad_edge=False, expand=True, header_style=muted)
    table.add_column('when', width=11, no_wrap=True, overflow='ellipsis')
    table.add_column('', min_width=1, max_width=1, no_wrap=True)
    # Let Rich shrink text columns first, while their cells remain single-line.
    # Keep the status glyph and token count visible in a narrow pane.
    table.add_column(Text('agent · summary', no_wrap=True, overflow='ellipsis'), ratio=1)
    table.add_column(Text('where', no_wrap=True, overflow='ellipsis'), min_width=4, max_width=14)
    table.add_column(Text('outcome', no_wrap=True, overflow='ellipsis'), min_width=9, max_width=24)
    table.add_column('tokens', min_width=6, justify='right', no_wrap=True, overflow='ellipsis')
    if filed:
        table.add_row(relative_time(filing['time'], relative_now), Text('✓', style=success),
                      Text('filed by ' + text(filing['author']), no_wrap=True, overflow='ellipsis'),
                      Text('GitHub', no_wrap=True, overflow='ellipsis'), 'filed', '')
    frame = spinner_frame(now.timestamp())
    for run in runs:
        status, label = run_status(run, now)
        glyph = Text('✓', style=success) if status == 'success' else Text('✗', style=error) if status == 'failed' else Text(frame)
        summary = text(run.get('agent'))
        if run.get('summary'):
            summary += ' · ' + text(run['summary'])
        outcome = text(run.get('outcome'), label)
        blockers = run.get('human_blocker')
        if isinstance(blockers, list) and blockers:
            outcome += ' · BLOCKED: ' + ', '.join(text(b) for b in blockers[:10])
        usage = usage_fields(run).get('usage')
        table.add_row(relative_time(run.get('time'), relative_now), glyph,
                      Text(summary, no_wrap=True, overflow='ellipsis'),
                      run_host(run.get('host'), muted=muted), RunOutcome(outcome, denial_count(run)),
                      short_tokens(usage['tokens']) if usage is not None else '')
    return Group(subtitle, table)
