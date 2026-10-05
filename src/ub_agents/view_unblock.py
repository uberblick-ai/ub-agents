"""Inert action-needed presentation, using only observed launcher trust."""

from dataclasses import dataclass
from datetime import datetime
import re
import time

from .view_data import Description, description_text, item_history, mapping, text

ACTION_MARKER = '<!-- ub-agents:action-needed '
LINKS = re.compile(r'^\[Claim\]\(.*?\) · (?:\[Outcome\]\(.*?\)|No outcome was reported\.)$')


def comment_body(value):
    lines = description_text(value).splitlines()
    return '\n'.join(line for index, line in enumerate(lines)
                     if not (index == 0 and line.startswith(ACTION_MARKER))
                     and line != '**Action needed**' and not LINKS.fullmatch(line)).strip()


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def trust_reason(author, authors):
    if not isinstance(author, str) or not author:
        return 'The comment author is unreadable and cannot be verified.'
    verified = mapping(authors.get(author.casefold()))
    if verified.get('trusted') is True:
        return ''
    return text(verified.get('reason'), '') or f'The launcher has not verified @{author} for coordination records.'


@dataclass(frozen=True)
class ActionComment(Description):
    created_at: str = ''
    author: str = ''

    def details(self, now=None):
        now = time.time() if now is None else now
        lines = [] if self.available else ['No action-needed comment is cached; press g to load from GitHub.']
        if self.error:
            lines.append(self.error + ' Press g to retry.')
        if self.notice:
            lines.append(self.notice)
        if self.source:
            created = stamp(self.created_at)
            clock = datetime.fromtimestamp(created).astimezone().strftime('%H:%M') if created is not None else None
            parts = ['Source: action-needed comment', clock, self.source]
            if self.observed_at is not None:
                parts.append(f'loaded {max(0, now - self.observed_at):.0f}s ago')
            lines.append(' · '.join(part for part in parts if part))
        return '\n'.join(lines)


def needs_attention(row):
    return bool(row and not row.hidden and row.group == 'Needs attention' and row.state in {'parked', 'blocked', 'failed'})


def local_action(row, session):
    notice = mapping(mapping(session.data.get('action_needed')).get(str(row.item))) if row and session else {}
    if not isinstance(notice.get('text'), str) or not notice['text'].startswith(ACTION_MARKER):
        return ActionComment()
    reason = trust_reason(notice.get('author'), mapping(session.data.get('coordination_authors')))
    if reason:
        return ActionComment(error=reason)
    return ActionComment(body=comment_body(notice['text']), source='snapshot', available=True,
                         notice='Comment shortened in snapshot.' if notice.get('omitted_characters') else '',
                         created_at=notice.get('created_at'), author=notice['author'])


def unblock_metadata(row, comment, session, now=None):
    if not row:
        return '', ''
    state = row.state
    failures, maximum = row.data.get('failures'), row.data.get('max_attempts')
    if state in {'blocked', 'failed'} and type(failures) is int and type(maximum) is int and failures >= maximum:
        state = f'failed {failures}/{maximum}'
    parts = [row.agent, state]
    created = stamp(comment.created_at) if comment and comment.available else None
    if created is None:
        times = [stamp(run.get('time')) for run in item_history(row, session).get('runs', [])]
        created = max((value for value in times if value is not None), default=None)
    waiting = ''
    if created is not None:
        waiting = f'waiting {int(max(0, (time.time() if now is None else now) - created) // 60)}m'
        parts.extend([waiting, 'since ' + datetime.fromtimestamp(created).astimezone().strftime('%H:%M')])
    return ' · '.join(part for part in parts if part), waiting
