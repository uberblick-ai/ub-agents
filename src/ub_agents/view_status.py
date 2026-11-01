"""Pure terminal status text and layout; clocks and theme styles come from callers."""

from dataclasses import dataclass
from datetime import datetime
from math import ceil

from rich.style import Style
from rich.text import Text

from .view_data import item_handoff, item_header, mapping, run_status, text
from .view_spinner import spinner_frame
from .view_time import deadline, pause_status, remaining_time, runtime_pauses
from .view_unblock import unblock_metadata


@dataclass(frozen=True)
class StatusStyles:
    muted: Style
    accent: Style
    error: Style
    warning: Style


def pane_line(value, width, style=''):
    line = value.copy() if isinstance(value, Text) else Text(text(value, ''))
    line.stylize_before(style)
    line.truncate(max(0, width), overflow='ellipsis')
    return line


def _footer_prefix(session, width, keys, narrow, launcher_attached, awake_enabled, now):
    parts = ['' if narrow else 'ub-agents']
    repository = ''
    if session:
        version = text(session.data.get('base_version'), '')
        if version:
            parts[0] += ('' if narrow else ' ') + f'v{version}'
        repository = text(session.data.get('repository'), '')
    if repository:
        parts.append(repository)
    if launcher_attached and awake_enabled:
        parts.append('☕')
    if session:
        pauses = runtime_pauses(session.data, now)
        labels = [f'{cli} paused {remaining_time(end, now)}' if narrow else
                  pause_status({cli: end}, now) for cli, end in pauses.items()]
        prefix = parts.copy()
        if repository:
            prefix[1] = '…'
        if narrow and Text(' · '.join(prefix + labels)).cell_len + len(keys) + 1 > width:
            labels = [f'{cli} {remaining_time(end, now)}' for cli, end in pauses.items()]
        parts.extend(labels)
    return parts, repository


def _poll_activity(activity, control, narrow, launcher_attached, now):
    value = text(activity.get('state'), '')
    limited = None
    if launcher_attached:
        limited = control.get('rate_limit_until')
        if activity.get('reason') == 'rate-limit reset':
            limited = limited or activity.get('until')
    limited = deadline(limited, now)
    if value == 'waiting':
        try:
            until = datetime.fromisoformat(activity['until'].replace('Z', '+00:00'))
            if until.tzinfo is not None:
                poll_label = 'poll' if narrow else 'next poll'
                value = f'{poll_label} {max(0, ceil((until - now).total_seconds()))}s'
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            pass
    if limited is not None:
        label = f'rate limited for {remaining_time(limited, now)} · r unavailable'
        value = f'{value} · {label}' if value == 'running assignment' else label
    return value, limited


def _footer_activity(session, narrow, launcher_attached, poll_feedback, now):
    if not session:
        return ['reading session']
    parts = []
    if session.data.get('queue_agent'):
        parts.append('agent ' + text(session.data['queue_agent']))
    state = session.state(now)
    if state == 'malformed':
        parts.append('malformed: ' + text(session.error))
    elif state in {'stale', 'ended'}:
        parts.append(state)
    activity = mapping(session.data.get('activity'))
    control = mapping(session.data.get('poll_now')) | poll_feedback
    value, limited = _poll_activity(activity, control, narrow, launcher_attached, now)
    if value:
        parts.append(value)
    if limited is None:
        if value == 'running assignment' and control.get('refreshing') is True:
            parts.append('polling')
        elif launcher_attached:
            until = deadline(control.get('cooldown_until'), now)
            if until is not None:
                parts.append(f'poll now available in {ceil((until - now).total_seconds())}s')
    return parts


def _shorten_keys(keys, width, left_width):
    # Shorten paused keys only when they crowd out the version/activity.
    if len(keys) + left_width + 1 > width:
        keys = keys.replace('g reload ', '')
    if len(keys) + left_width + 1 > width and keys.startswith('f follow'):
        keys = 'f follow h older u raw PgUp/Dn ? keys q quit'
        if len(keys) + left_width + 1 > width:
            keys = 'f follow h older u raw ? keys q quit'
    if len(keys) + left_width + 1 > width and keys.startswith('1-4 tabs f follow'):
        keys = '1-4 tabs g load f follow h older u raw ? keys q quit'
        if len(keys) + left_width + 1 > width:
            keys = '1-4 tabs g load f follow ? keys q quit'
    return keys


def _footer_keys(keys, width, left_width, narrow, launcher_attached, awake_supported):
    if launcher_attached:
        poll_keys = keys.replace('? keys', 'r poll now ? keys')
        if not narrow or len(poll_keys) + left_width + 1 <= width:
            keys = poll_keys
    if narrow:
        keys = _shorten_keys(keys, width, left_width)
    # Add this hint only after retaining the existing poll/reload hints.
    # It is the first hint to disappear as space becomes scarce.
    if launcher_attached and awake_supported and 'r poll now' in keys:
        awake_keys = keys.replace('? keys', 'c Stay awake ? keys')
        if len(awake_keys) + left_width + 1 <= width:
            keys = awake_keys
    return Text(keys if narrow or width >= 110 else '? keys q quit')


def _footer_left(parts, repository, prefix_count, room):
    left = Text(' · '.join(part for part in parts if part), no_wrap=True, overflow='ellipsis')
    if repository:
        # Keep a readable name before shortening activity, then let it grow
        # when the full activity fits. Reserve a separator and activity ellipsis
        # where possible without cutting the version or awake token.
        other = Text(' · '.join(part for part in parts[:1] + parts[2:] if part))
        fixed = Text(' · '.join(part for part in parts[:1] + parts[2:prefix_count] if part))
        activity = Text(' · '.join(part for part in parts[prefix_count:] if part))
        name = Text(repository)
        name_room = max(1, room - fixed.cell_len - (3 if fixed else 0) - (4 if activity else 0))
        preferred = max(min(24, name.cell_len), room - other.cell_len - (3 if other else 0))
        name.truncate(min(name_room, preferred), overflow='ellipsis')
        parts = parts.copy()
        parts[1] = name.plain
        left = Text(' · '.join(part for part in parts[:prefix_count] if part), no_wrap=True)
        activity_room = max(0, room - left.cell_len - 3)
        if activity and activity_room:
            activity.truncate(activity_room, overflow='ellipsis')
            left.append(' · ')
            left.append_text(activity)
    left.truncate(room, overflow='ellipsis')
    return left


def footer(session, width, keys, *, now, monotonic_now, narrow=False, launcher_attached=False,
           awake_enabled=False, awake_supported=False, poll_feedback=None, copy_notice='',
           copy_notice_until=0, awake_notice='', awake_notice_until=0):
    if monotonic_now < max(copy_notice_until, awake_notice_until):
        value = awake_notice if awake_notice_until > copy_notice_until else copy_notice
        notice = Text(value, no_wrap=True, overflow='ellipsis')
        notice.truncate(max(0, width), overflow='ellipsis')
        notice.pad_right(max(0, width - notice.cell_len))
        return notice
    parts, repository = _footer_prefix(session, width, keys, narrow, launcher_attached, awake_enabled, now)
    prefix_count = len(parts)
    # Reserve an intact prefix, with at least an ellipsis for the repository.
    prefix = parts.copy()
    if repository:
        prefix[1] = '…'
    reserve = Text(' · '.join(part for part in prefix if part)).cell_len
    parts.extend(_footer_activity(session, narrow, launcher_attached, poll_feedback or {}, now))
    left_width = Text(' · '.join(part for part in parts if part)).cell_len
    right = _footer_keys(keys, width, left_width, narrow, launcher_attached, awake_supported)
    if prefix_count == 1:
        reserve = 0
    right.truncate(max(0, width - reserve - 1), overflow='ellipsis')
    left = _footer_left(parts, repository, prefix_count, max(0, width - right.cell_len - 1))
    left.append(' ' * max(0, width - left.cell_len - right.cell_len))
    left.append_text(right)
    return left


def key_hints(reading, *, narrow, item_view, unblock_visible, active_tab):
    keys = ('f follow h older u raw PgUp/PgDn scroll ? keys q quit' if not reading.follow else
            '1-3 tabs ? keys q quit')
    if narrow:
        if not item_view:
            keys = '? keys q quit'
        elif reading.follow:
            keys = 'Esc back 1-3 tabs ? keys q quit'
    if unblock_visible and (not narrow or item_view):
        keys = keys.replace('1-3', '1-4')
        if active_tab == 'unblock':
            if '1-4 tabs' not in keys:
                keys = '1-4 tabs ' + keys
            keys = keys.replace('? keys', 'g load ? keys')
    if active_tab == 'log' and (not narrow or item_view):
        keys = keys.replace('? keys', 'g reload ? keys')
    return keys


def log_mode(reading, width, styles):
    mode = Text('│ ', style=styles.muted, no_wrap=True)
    mode.append('Formatted', style=(styles.muted if reading.raw else styles.accent) + Style(underline=not reading.raw))
    mode.append('  ' if len('│ Formatted  Raw u') <= width else ' ')
    mode.append('Raw', style=(styles.accent if reading.raw else styles.muted) + Style(reverse=reading.raw))
    hint = ' (u)'
    if mode.cell_len + len(hint) > width:
        hint = ' u'
    mode.append(hint, style=styles.muted)
    return mode


def paused_pill(reading, unread, lag):
    visible = bool((reading.page or reading.log) and (not reading.follow or unread or lag))
    parts = ['⏸ PAUSED' if not reading.follow else '↓ BEHIND']
    if unread:
        parts.append(f'{unread} new ↓')
    if lag:
        parts.append(f'{lag}B lag')
    if reading.raw:
        parts.append('RAW')
    parts.append('f follow')
    return visible, Text(' ' + ' · '.join(parts) + ' ', style='reverse', no_wrap=True, overflow='ellipsis')


def header(row, description, session, width, styles, *, clickable, unblock, action, now):
    title, metadata = item_header(row, description, session)
    waiting = ''
    if unblock:
        metadata, waiting = unblock_metadata(row, action, session, now)
    title_text, metadata_text = Text(title), Text(metadata)
    if clickable:
        title_text.stylize(Style(meta={'@click': 'app.open_reference'}), 0, len(str(row.item)) + 1)
    if title.startswith('⌥'):
        title_text.stylize(styles.accent, 0, 1)
    handoff = item_handoff(row, session)
    if handoff is not None and not unblock:
        offset = len(metadata) - len(f'⌥{handoff}')
        metadata_text.stylize(styles.accent, offset, offset + 1)
    if waiting:
        offset = metadata.find(waiting)
        metadata_text.stylize(styles.error, offset, offset + len(waiting))
    value = Text()
    value.append_text(pane_line(title_text, width, 'bold'))
    value.append('\n').append_text(pane_line(metadata_text, width, styles.muted + Style(dim=True)))
    value.append('\n' + '┄' * width, style=styles.muted)
    return value


def log_notices(row, reading, width, styles):
    page, log = reading.page, reading.log
    notices = []
    if row and row.log:
        if page and reading.latest and page.generation != reading.latest.generation:
            notices.append('FILE CHANGED; paused earlier generation; f latest.')
        elif page and page.generation:
            notices.append('File replaced or changed generation.')
        if log and log.error:
            notices.append('Read error: ' + log.error)
        if log and log.pending_bytes:
            notices.append(f'Unfinished: {log.pending_bytes}B (raw preview)')
        if reading.notice:
            notices.append(reading.notice)
        if reading.runtime not in ('claude', 'codex'):
            notices.append(f'{reading.runtime}: plain/raw fallback')
    return bool(notices), pane_line(' · '.join(notices), width, styles.warning + Style(bold=True))


def empty_log_message(row):
    return (row.reason if row and mapping(row.data.get('owner')) else
            'No local log cached for this row.' if not row or not row.log else 'No log output yet.')


def run_status_line(row, session, width, styles, monotonic_now):
    left, right, running = run_status(row, session)
    if running:
        left = spinner_frame(monotonic_now) + ' ' + left
    right_line = pane_line(right, max(0, width - 1))
    left_line = pane_line(left, width - right_line.cell_len - (1 if right_line.cell_len else 0))
    status_line = left_line
    if right_line.cell_len:
        status_line.append(' ' * max(1, width - left_line.cell_len - right_line.cell_len)).append_text(right_line)
    run_note = Text('┄' * width + '\n', style=styles.muted)
    run_note.append_text(status_line)
    return run_note
