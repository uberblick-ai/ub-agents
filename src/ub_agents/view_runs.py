"""Read-only, per-item Runs table from local history files."""

from copy import copy
from datetime import datetime
import socket

from rich.console import Group
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from .context_usage import context_fields
from .denials import denial_count
from .run_usage import short_tokens, usage_fields
from .view_data import item_history, mapping, text
from .view_spinner import spinner_frame
from .view_theme import theme_style


class RunsTable(Table):
    """Reserve the entire context column, or render the original six columns."""

    minimum_widths = (11, 1, 1, 4, 9, 6)

    def __rich_console__(self, console, options):
        # Six existing column minimums, plus two cells between all seven columns.
        minimum = sum(self.minimum_widths) + 2 * len(self.minimum_widths) + self.columns[-1].width
        if options.max_width < minimum:
            table = copy(self)
            table.columns = self.columns[:-1]
            yield from Table.__rich_console__(table, console, options)
        else:
            yield from super().__rich_console__(console, options)

    def _calculate_column_widths(self, console, options):
        widths = super()._calculate_column_widths(console, options)
        if len(self.columns) > len(self.minimum_widths):
            # Rich's last-resort shrinking can go below column minimums. Keep
            # those minimums and take any excess from text with room to spare.
            minimums = [width + self._get_padding_width(index) for index, width in
                        enumerate((*self.minimum_widths, self.columns[-1].width))]
            # Preserve denial suffixes whenever space beyond the base minimums
            # allows, without raising the context column's visibility threshold.
            outcome_minimum = self._measure_column(console, options, self.columns[4]).minimum
            spare = max(0, options.max_width - sum(minimums))
            minimums[4] += min(spare, max(0, outcome_minimum - minimums[4]))
            widths = [max(width, minimum) for width, minimum in zip(widths, minimums)]
            excess = max(0, sum(widths) - options.max_width)
            for index in (2, 3, 4, 5):
                reduction = min(excess, widths[index] - minimums[index])
                widths[index] -= reduction
                excess -= reduction
        return widths


def run_context(run):
    context = context_fields(run).get('context')
    if context is None:
        return ''
    percent = (100 * context['peak'] + context['window'] // 2) // context['window']
    suffix = f" ↻{context['compactions']}" if context['compactions'] else ''
    return f'{percent}%{suffix}'


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
    contexts = [run_context(run) for run in runs]
    table = RunsTable(box=None, padding=(0, 1), pad_edge=False, expand=True, header_style=muted)
    table.add_column('when', width=11, no_wrap=True, overflow='ellipsis')
    table.add_column('', min_width=1, max_width=1, no_wrap=True)
    # Let Rich shrink text columns first, while their cells remain single-line.
    # Keep the status glyph and token count visible in a narrow pane.
    table.add_column(Text('agent · summary', no_wrap=True, overflow='ellipsis'), ratio=1)
    table.add_column(Text('where', no_wrap=True, overflow='ellipsis'), min_width=4, max_width=14)
    table.add_column(Text('outcome', no_wrap=True, overflow='ellipsis'), min_width=9, max_width=24)
    table.add_column('tokens', min_width=6, justify='right', no_wrap=True, overflow='ellipsis')
    context_width = max(len(value) for value in ['context', *contexts])
    table.add_column('context', width=context_width, justify='right', no_wrap=True)
    if filed:
        table.add_row(relative_time(filing['time'], relative_now), Text('✓', style=success),
                      Text('filed by ' + text(filing['author']), no_wrap=True, overflow='ellipsis'),
                      Text('GitHub', no_wrap=True, overflow='ellipsis'), 'filed', '', '')
    frame = spinner_frame(now.timestamp())
    for run, context in zip(runs, contexts):
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
                      short_tokens(usage['tokens']) if usage is not None else '', context)
    return Group(subtitle, table)
