"""Lossy readable projection; original bytes remain in the supplied raw file."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

MAX_RECORD = 16 * 1024
MAX_TEXT = 2048
MAX_ENTRIES = 200
READ_BUDGET = 32 * 1024


def clean(text):
    # Untrusted output is plain text, never terminal escapes or Rich markup.
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(text))[:MAX_TEXT]


def event_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if stamp.tzinfo is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Entry:
    event: str | None
    capture: str | None
    kind: str
    text: str
    raw: str

    def display(self, raw=False):
        return f'event={self.event or "—"} \ncapture={self.capture or "— (historical)"} \n{self.kind}\n{self.raw if raw else self.text}'


def decode(raw, capture):
    plain = clean(raw.decode('utf-8', errors='replace'))
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        return Entry(None, capture, 'raw / partial', plain, plain)
    kind = str(data.get('type', 'unknown'))
    text = plain
    try:
        if kind == 'stream_event':  # Claude streaming deltas
            event = data.get('event', {})
            text = event.get('delta', {}).get('text') or json.dumps(event, ensure_ascii=False)
        elif kind == 'assistant':  # Claude full message, including tool invocation
            content = data.get('message', {}).get('content', [])
            text = '\n'.join(c.get('text') or f'tool {c.get("name", "unknown")}: {c.get("input", {})}' for c in content)
        elif kind == 'user':
            text = json.dumps(data.get('message', {}), ensure_ascii=False)
        elif kind in {'item.started', 'item.completed', 'item.updated'}:  # Codex JSONL
            item = data.get('item', {})
            text = item.get('text') or f'{item.get("type", "item")}: {item.get("command", "")}\n{item.get("aggregated_output", "")}'
        elif kind in {'result', 'error'}:
            text = data.get('result') or data.get('message') or plain
    except (AttributeError, TypeError, KeyError):
        text = plain
        kind += ' / unfamiliar shape'
    return Entry(event_time(data.get('timestamp')), capture, clean(kind), clean(text), plain)


class Buffer:
    def __init__(self):
        self.entries = deque(maxlen=MAX_ENTRIES)
        self.pending = b''
        self.capture = None
        self.discarded = 0
        self.total = 0

    def append(self, entry):
        if len(self.entries) == MAX_ENTRIES:
            self.discarded += 1
        self.entries.append(entry)
        self.total += 1

    def feed(self, chunk, capture=None):
        # Caller reads bounded chunks. Fragment oversized/no-newline output.
        if not self.pending:
            self.capture = capture
        self.pending += chunk
        while self.pending:
            newline = self.pending.find(b'\n')
            if 0 <= newline < MAX_RECORD:
                record, self.pending = self.pending[:newline], self.pending[newline + 1:]
            elif len(self.pending) >= MAX_RECORD:
                record, self.pending = self.pending[:MAX_RECORD], self.pending[MAX_RECORD:]
            else:
                break
            self.append(decode(record, self.capture))
            self.capture = capture

    def preview(self):
        return decode(self.pending, self.capture) if self.pending else None


class Tail:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.initial_size = path.stat().st_size if path.exists() else 0
        self.buffer = Buffer()

    def poll(self):
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
            self.initial_size = size
            self.buffer.append(Entry(None, None, 'observer', 'Raw file truncated; restarted', ''))
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            historical = self.offset < self.initial_size
            limit = min(READ_BUDGET, self.initial_size - self.offset) if historical else READ_BUDGET
            chunk = stream.read(limit)
        if chunk:
            self.offset += len(chunk)
            captured = None if historical else datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            self.buffer.feed(chunk, captured)
