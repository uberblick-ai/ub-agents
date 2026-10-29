"""Relative deadlines for display; never an input to launcher scheduling."""

from datetime import datetime
from math import ceil


def deadline(value, now):
    try:
        until = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return until if until.tzinfo is not None and until > now else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def remaining_time(until, now):
    """Round up to at most two nonzero day/hour/minute units."""
    minutes = max(1, ceil((until - now).total_seconds() / 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days and hours and minutes:
        days, hours = divmod(days * 24 + hours + 1, 24)
        minutes = 0
    return ' '.join(f'{count}{unit}' for count, unit in
                    ((days, 'd'), (hours, 'h'), (minutes, 'm')) if count)


def runtime_pauses(data, now):
    values = data.get('runtime_pauses')
    if not isinstance(values, dict):
        return {}
    return {cli: end for cli in ('claude', 'codex', 'opencode') if (end := deadline(values.get(cli), now)) is not None}


def plan_pauses(plan, pauses):
    clis = plan.get('paused_clis')
    if (plan.get('state') != 'waiting' or not isinstance(clis, (list, tuple)) or not clis
            or not all(isinstance(cli, str) and cli in pauses for cli in clis)):
        return {}
    return {cli: end for cli, end in pauses.items() if cli in clis}


def pause_status(pauses, now):
    return f'{", ".join(pauses)} paused for {remaining_time(min(pauses.values()), now)}' if pauses else ''
