"""Inert action-needed presentation, using only observed launcher trust."""

from dataclasses import dataclass
from datetime import datetime
import re
import time
from typing import ClassVar

from .view_data import Description, attention_agents, description_text, mapping, text
from .attention import attention_state, notice_summary, short_reason, stamp, waiting_time
from .notices import notice_reason, outcome_resume, retry_command

ACTION_MARKER = '<!-- ub-agents:action-needed '
LINKS = re.compile(r'^\[Claim\]\(.*?\) · (?:\[Outcome\]\(.*?\)|No outcome was reported\.)$')


def comment_body(value):
    lines = description_text(value).splitlines()
    if lines and lines[0].startswith(ACTION_MARKER):
        lines.pop(0)
    if lines and lines[0] == '**Action needed**':
        lines.pop(0)
    link = next((index for index, line in enumerate(lines) if LINKS.fullmatch(line)), None)
    if link is not None and '<summary>Reasoning and evidence</summary>' not in lines:
        lines.pop(link)
    return '\n'.join(lines).strip()


def comment_sections(body):
    """Fold the generated notice wrapper, leaving nested supporting Markdown intact."""
    wrapper = re.search(r'<details>\n<summary>(?:Reasoning and evidence|Reasoning, evidence and resume instructions)</summary>\n\n', body)
    if not wrapper:
        return body, ''
    visible, details = body[:wrapper.start()], body[wrapper.end():]
    if details.endswith('\n\n</details>'):
        details = details[:-len('\n\n</details>')]
    return visible.strip(), details.strip()


def resume_section(body):
    """Extract same-role workflow steps, leaving approval-gate steps visible."""
    wrapper = re.search(r'<details>\n<summary>(To send it back to [^<\n]+ instead|To resume [^<\n]+)</summary>\n\n', body)
    if not wrapper:
        tail = re.search(r'(?m)^Then resume ([^:\n]+):\n\n', body)
        if tail:
            return body[:tail.start()].strip(), f'To resume {tail[1]}', body[tail.end():].strip()
        return body, '', ''
    steps = body[wrapper.end():].rstrip()
    if steps.endswith('\n\n</details>'):
        steps = steps[:-len('\n\n</details>')]
    return body[:wrapper.start()].strip(), wrapper[1], steps.strip()


def trust_reason(author, authors):
    if not isinstance(author, str) or not author:
        return 'The comment author is unreadable and cannot be verified.'
    verified = mapping(authors.get(author.casefold()))
    if verified.get('trusted') is True:
        return ''
    return text(verified.get('reason'), '') or f'The launcher has not verified @{author} for coordination records.'


@dataclass(frozen=True)
class ActionComment(Description):
    extract_pointers: ClassVar[bool] = False
    created_at: str = ''
    author: str = ''
    comment_id: str = ''
    omitted: bool = False

    def details(self, now=None, pending=False):
        now = time.time() if now is None else now
        lines = [] if self.available else ['No action-needed comment is cached; press g to load from GitHub.']
        if not self.available and self.omitted:
            suffix = 'loading from GitHub…' if pending else 'press g to load from GitHub.'
            lines = ['Comment left out of the snapshot to save space; ' + suffix]
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
    if not row or row.hidden or row.group != 'Needs attention':
        return False
    if row.attention_rows:
        return any(needs_attention(agent) for agent in row.attention_rows)
    failures, maximum = row.data.get('failures'), row.data.get('max_attempts')
    exhausted = type(failures) is int and type(maximum) is int and failures >= maximum
    return row.state in {'parked', 'blocked'} or row.state == 'failed' and exhausted


def local_action(row, session):
    notice = mapping(mapping(session.data.get('action_needed')).get(str(row.item))) if row and session else {}
    if notice.get('omitted') is True:
        return ActionComment(omitted=True)
    if not isinstance(notice.get('text'), str) or not notice['text'].startswith(ACTION_MARKER):
        return ActionComment()
    reason = trust_reason(notice.get('author'), mapping(session.data.get('coordination_authors')))
    if reason:
        return ActionComment(error=reason)
    return ActionComment(body=comment_body(notice['text']), source='snapshot', available=True,
                         notice='Comment shortened in snapshot.' if notice.get('omitted_characters') else '',
                         created_at=notice.get('created_at'), author=notice['author'],
                         comment_id=str(notice.get('id') or ''))


def plan_reason(row):
    """Remove observer-added notice text and generated same-role retry prose."""
    reason = description_text(row.reason)
    summary = description_text(text(row.data.get('attention_reason'), ''))
    prefix = re.match(r'^(?:Last run blocked|Stop label .+? is present): ', reason)
    start = prefix.end() if prefix else 0
    if summary and reason[start:].endswith(': ' + summary):
        reason = reason[:-len(': ' + summary)]
    return re.sub(r'; inspect (?:outcome|failures) and use ub-agents retry \d+ --agent \S+ --reason TEXT$', '', reason)


def other_role_context(row, body):
    """Show other causes without the notice summary appended by the observer."""
    visible, supporting = comment_sections(body)
    lead, title, _ = resume_section(visible)
    role = (title.removeprefix('To resume ') if title.startswith('To resume ') else
            title.removeprefix('To send it back to ').removesuffix(' instead'))
    reasons = {notice_summary(part) for part in (lead, supporting) if part}
    sections = []
    for agent in row.attention_rows:
        if agent.agent == role:
            continue
        reason = short_reason(plan_reason(agent).removeprefix('Last run blocked: '))
        if reason and reason not in reasons:
            sections.append(f'### {agent.agent} · {attention_state(agent)[1]}\n\n{notice_reason(reason)}')
    return lead, '\n\n'.join(sections)


def fallback_body(row):
    if row and len(row.attention_rows) > 1:
        bodies, resumes = [], []
        for agent in row.attention_rows:
            body = fallback_body(agent)
            lead, _, resume = resume_section(body)
            if lead:
                bodies.append(f'### {agent.agent} · {attention_state(agent)[1]}\n\n{lead}')
            if resume:
                resumes.append(f'### {agent.agent}\n\n{resume}')
        body = '\n\n'.join(bodies)
        if resumes:
            title = ('To send it back to these agents instead' if row.data.get('kind') == 'pr'
                     else 'To resume these agents')
            body += (f'\n\n<details>\n<summary>{title}</summary>\n\n' +
                     '\n\n'.join(resumes) + '\n\n</details>')
        return body
    context = plan_reason(row) if row else ''
    summary = description_text(text(row.data.get('attention_reason'), '')) if row else ''
    if summary and summary not in context:
        context = '\n\n'.join(part for part in (context, summary) if part)
    if row and row.state == 'blocked' and needs_attention(row):
        steps = (f"```sh\n{retry_command(row.item, row.agent)}\n```\n\n"
                 "Use these steps only when resuming the same role; follow the project's "
                 "correction or handoff route if a different role must act next.")
        resume = outcome_resume(row.item, text(row.agent), steps, is_pr=row.data.get('kind') == 'pr')
        return f"{context}\n\n{resume}".strip()
    return context.strip()


def unblock_body(row, comment):
    if comment.available:
        if row and len(row.attention_rows) > 1:
            lead, context = other_role_context(row, comment.body)
            if context:
                return lead + '\n\n' + context + comment.body[len(lead):]
        return comment.body
    body = fallback_body(row)
    return '**Fallback: action-needed comment unavailable.**\n\n' + body if body else ''


def unblock_metadata(row, comment, session, now=None):
    if not row:
        return '', ''
    _, state = attention_state(row)
    parts = [attention_agents(row)] if len(row.attention_rows) > 1 else [row.agent, state]
    start = row.waiting_since()
    created = stamp(start)
    waiting = ''
    if created is not None:
        waiting = 'waiting ' + waiting_time(start, now)
        parts.extend([waiting, 'since ' + datetime.fromtimestamp(created).astimezone().strftime('%H:%M')])
    return ' · '.join(part for part in parts if part), waiting
