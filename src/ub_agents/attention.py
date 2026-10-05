"""Display-only waiting times and parking summaries; never coordination input."""

from datetime import datetime
import html
import re
import time

from .records import lease_summary, reported_actions


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def waiting_time(start, now=None):
    created = stamp(start)
    if created is None:
        return ''
    minutes = int(max(0, (time.time() if now is None else now) - created) // 60)
    if minutes < 60:
        return f'{minutes}m'
    hours = minutes // 60
    return f'{hours}h' if hours < 48 else f'{hours // 24}d'


def short_reason(summary):
    summary = ' '.join(summary.split()) if isinstance(summary, str) else ''
    return re.sub(r'^Stop label .+? is present(?::\s*|$)', '', summary)


def notice_summary(body):
    lines = body.splitlines()
    if lines and lines[0].startswith('<!-- ub-agents:action-needed '):
        lines.pop(0)
    if lines and lines[0] == '**Action needed**':
        lines.pop(0)
    # New notices lead with one bold ask per bullet; older notices have a reason.
    reason = '\n'.join(lines).strip().split('\n\n', 1)[0]
    asks = reason.splitlines()
    if asks and all(line.startswith('- **') and line.endswith('**') for line in asks):
        reason = '; '.join(line[4:-2] for line in asks)
    elif reason.startswith('**') and reason.endswith('**'):
        reason = reason[2:-2]
    reason = html.unescape(re.sub(r'\\([\\`*_\[\]])', r'\1', reason))
    return short_reason(reason)


def attention_details(plan, stop_labels, notice):
    """Use the notice, a finalized stop outcome, then a finished run, in order."""
    if plan.state not in {'parked', 'blocked', 'failed'}:
        return {}
    present_stops = plan.item.labels.intersection(stop_labels)
    stops = sorted(present_stops or (stop_labels if plan.approval_gate else ()))
    details = {'stop_labels': stops, 'waiting_since': None, 'attention_reason': ''}
    if notice:
        details.update(waiting_since=notice.get('created_at'), attention_reason=notice_summary(notice['text']))
        return details
    outcomes = [r for r in plan.history if r['kind'] == 'outcome'
                and r.get('accepted') and r.get('transition_complete') and not r.get('rejected')
                and (r.get('handoff') or r['assignment']) == plan.item.number
                and set(r.get('transition', {}).get('add', ())).intersection(present_stops)]
    if outcomes:
        outcome = max(outcomes, key=lambda r: r.get('id', 0))
        details.update(waiting_since=outcome.get('created'),
                       attention_reason=short_reason('; '.join(reported_actions(outcome)) or outcome.get('summary')))
        return details
    if plan.approval_gate:
        details['attention_reason'] = short_reason(plan.approval_gate.reason)
        return details
    finished = [r for r in plan.history if plan.state in {'blocked', 'failed'}
                and r['kind'] == 'lease' and r.get('state') == 'released']
    if finished:
        run = max(finished, key=lambda r: (stamp(r.get('expires')) or 0, r.get('id', 0)))
        source_id = run.get('recovered_lease_id') or run.get('id')
        outcome = next((r for r in plan.history if r['kind'] == 'outcome'
                        and source_id is not None and r.get('lease_id') == source_id and not r.get('rejected')), {})
        details['attention_reason'] = short_reason('; '.join(reported_actions(outcome)) or lease_summary(plan.history, run))
        details['waiting_since'] = run.get('expires')
    return details


def attention_state(row):
    failures, maximum = row.data.get('failures'), row.data.get('max_attempts')
    exhausted = (row.state == 'failed' or row.state == 'blocked'
                 and row.reason.startswith('Attempt limit exhausted'))
    if exhausted and type(failures) is int and type(maximum) is int and failures >= maximum:
        return '✗', f'failed {failures}/{maximum}'
    if row.state == 'parked':
        stops = row.data.get('stop_labels')
        labels = ', '.join(stops) if isinstance(stops, list) and all(isinstance(s, str) for s in stops) else ''
        return '?', labels or 'parked'
    return '!', row.state
