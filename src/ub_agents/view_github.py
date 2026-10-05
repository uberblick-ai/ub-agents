"""Explicit description reads only; no launcher client or workflow authority."""

from collections import OrderedDict
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
import signal
import subprocess
import sys
import time

from .view_data import Description, mapping
from .view_unblock import ACTION_MARKER, ActionComment, comment_body, stamp, trust_reason

REQUEST_SECONDS = 10
CACHE_ITEMS = 128
RESPONSE_BYTES = 512 * 1024
READ_BYTES = 32 * 1024
QUERY = '''query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issueOrPullRequest(number:$number) {
      ... on Issue { title body }
      ... on PullRequest { title body }
    }
  }
}'''
COMMENTS_QUERY = '''query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issueOrPullRequest(number:$number) {
      ... on Issue { comments(last:100) { nodes { author { login } createdAt body } } }
      ... on PullRequest { comments(last:100) { nodes { author { login } createdAt body } } }
    }
  }
}'''


@dataclass(frozen=True)
class Response:
    title: str = ''
    body: str = ''
    error: str = ''
    reset: float | None = None
    notice: str = ''
    action: ActionComment | None = None

    def __post_init__(self):
        description = Description(self.title, self.body, notice=self.notice, error=self.error)
        for field in ('title', 'body', 'notice', 'error'):
            object.__setattr__(self, field, getattr(description, field))


def parse_response(stdout, stderr, code, now, kind='issue', authors=None):
    raw = stdout.decode('utf-8', errors='replace').replace('\r\n', '\n')
    headers = {}
    status = ''
    if raw.startswith('HTTP/'):
        head, _, raw = raw.partition('\n\n')
        status = head.splitlines()[0]
        headers = {key.lower(): value.strip() for line in head.splitlines()[1:]
                   for key, sep, value in [line.partition(':')] if sep}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = {}
    errors = data.get('errors', []) if isinstance(data, dict) else []
    message = data.get('message', '') if isinstance(data, dict) else ''
    detail = stderr.decode('utf-8', errors='replace') or message or str(errors)
    limited = (' 429' in status or headers.get('x-ratelimit-remaining') == '0' or
               'rate limit' in detail.lower() or 'rate_limit' in str(errors).lower())
    if limited and (code or errors or ' 429' in status or ' 403' in status):
        resets = []
        try:
            resets.append(float(headers['x-ratelimit-reset']))
        except (KeyError, ValueError):
            pass
        retry = headers.get('retry-after', '')
        try:
            resets.append(now + float(retry))
        except ValueError:
            try:
                resets.append(parsedate_to_datetime(retry).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
        resets = [stamp for stamp in resets if math.isfinite(stamp) and stamp > now]
        return Response(error='GitHub rate limit reached.', reset=max(resets) if resets else now + 60)
    if code or errors:
        return Response(error=detail.strip() or f'gh exited with status {code}.')
    try:
        item = data['data']['repository']['issueOrPullRequest']
        if kind == 'unblock':
            comments = item['comments']['nodes']
            if not isinstance(comments, list):
                raise ValueError('Invalid comments')
            candidates, reasons = [], []
            for comment in comments[-100:]:
                if not isinstance(comment, dict) or not isinstance(comment.get('body'), str) or not comment['body'].startswith(ACTION_MARKER):
                    continue
                author = mapping(comment.get('author')).get('login')
                reason = trust_reason(author, authors or {})
                if reason:
                    reasons.append(reason)
                elif stamp(comment.get('createdAt')) is not None:
                    candidates.append(comment)
            if not candidates:
                reason = reasons[-1] if reasons else 'No trusted action-needed comment was found among the latest 100 comments.'
                return Response(error=reason)
            comment = max(candidates, key=lambda c: stamp(c['createdAt']))
            return Response(action=ActionComment(body=comment_body(comment['body']), available=True,
                                                 created_at=comment['createdAt'], author=comment['author']['login']))
        if not isinstance(item['title'], str) or not isinstance(item['body'], str):
            raise ValueError('Invalid description')
        return Response(item['title'], item['body'])
    except (KeyError, TypeError, ValueError):
        return Response(error='GitHub returned no readable ' + ('comments' if kind == 'unblock' else 'title/body') + ' for this item.')


class GhTransport:
    """One owned gh process, bounded nonblocking pipes and prompt group cleanup."""
    def __init__(self, clock=time.monotonic, wall_clock=time.time):
        self.clock, self.wall_clock = clock, wall_clock
        self.process = None
        self.life = None

    def start(self, repository, item, kind='issue', authors=None):
        self.kind, self.authors = kind, authors
        owner, repo = repository.split('/')
        env = dict(os.environ, GH_PROMPT_DISABLED='1', GH_PAGER='cat')
        env.pop('GH_DEBUG', None)
        command = ['gh', 'api', 'graphql', '--hostname', 'github.com', '--include',
             '-f', 'query=' + (COMMENTS_QUERY if kind == 'unblock' else QUERY), '-f', 'owner=' + owner, '-f', 'repo=' + repo,
             '-F', f'number={item}']
        read, self.life = os.pipe()
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-P', '-m', 'ub_agents.view_request', str(read), *command],
                pass_fds=(read,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True, env=env)
        except BaseException:
            os.close(self.life)
            self.life = None
            raise
        finally:
            os.close(read)
        self.deadline = self.clock() + REQUEST_SECONDS
        self.buffers = [bytearray(), bytearray()]
        self.eof = [False, False]
        try:
            for stream in (self.process.stdout, self.process.stderr):
                os.set_blocking(stream.fileno(), False)
        except BaseException:
            self.close()
            raise

    def poll(self):
        if self.process is None:
            return None
        if self.clock() >= self.deadline:
            self.close()
            return Response(error=f'GitHub read timed out after {REQUEST_SECONDS}s; press g to retry.')
        try:
            for index, stream in enumerate((self.process.stdout, self.process.stderr)):
                if self.eof[index]:
                    continue
                try:
                    chunk = os.read(stream.fileno(), READ_BYTES)
                except BlockingIOError:
                    continue
                self.buffers[index].extend(chunk)
                self.eof[index] = not chunk
                if sum(map(len, self.buffers)) > RESPONSE_BYTES:
                    self.close()
                    return Response(error='GitHub response exceeded the bounded read limit.')
            code = self.process.poll()
            if code is None or not all(self.eof):
                return None
            response = parse_response(*self.buffers, code, self.wall_clock(), self.kind, self.authors)
            self.close()
            return response
        except OSError as exc:
            self.close()
            return Response(error=str(exc))

    def close(self):
        if self.life is not None:
            os.close(self.life)
            self.life = None
        process, self.process = self.process, None
        if process is None:
            return
        try:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # Only the owned helper, never another view or the launcher.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            process.stdout.close()
            process.stderr.close()


class DescriptionLoads:
    def __init__(self, transport=None, clock=time.time):
        self.transport = transport or GhTransport()
        self.clock = clock
        self.cache = OrderedDict()
        self.pending = None
        self.pending_kind = None
        self.cooldown = 0

    @staticmethod
    def key(repository, item):
        if (isinstance(repository, str) and len(repository) <= 200 and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repository) and
                repository.split('/')[1] not in {'.', '..'} and
                type(item) is int and item > 0):
            return repository, item
        return None

    def get(self, key, kind='issue'):
        result = self.cache.get(key, {}).get(kind)
        if result is not None:
            self.cache.move_to_end(key)
        return result

    def remember(self, key, response, kind='issue'):
        result = (replace(response.action, source='GitHub', observed_at=self.clock()) if response.action else
                  ActionComment(source='GitHub', observed_at=self.clock(), error=response.error)) if kind == 'unblock' else (
                  Description(response.title, response.body, 'GitHub', self.clock(),
                              not response.error, response.notice, response.error))
        self.cache.setdefault(key, {})[kind] = result
        self.cache.move_to_end(key)
        while len(self.cache) > CACHE_ITEMS:
            self.cache.popitem(last=False)

    def request(self, key, kind='issue', authors=None):
        if key is None or self.pending is not None or self.clock() < self.cooldown:
            return
        cached = self.get(key, kind)
        if cached and cached.available:
            return
        try:
            if kind == 'unblock':
                self.transport.start(*key, kind, authors)
            else:
                self.transport.start(*key)
            self.pending = key
            self.pending_kind = kind
        except OSError as exc:
            self.remember(key, Response(error=str(exc)), kind)

    def poll(self):
        if self.pending is None:
            return
        response = self.transport.poll()
        if response is None:
            return
        key, self.pending = self.pending, None
        kind, self.pending_kind = self.pending_kind, None
        self.remember(key, response, kind)
        if response.reset is not None:
            self.cooldown = max(self.cooldown, response.reset)

    def close(self):
        self.transport.close()
        self.pending = None
        self.pending_kind = None
