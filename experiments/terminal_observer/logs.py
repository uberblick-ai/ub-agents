"""Bounded readable projection; original bytes remain in the supplied raw file."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

# Production Claude emits whole messages/tool results, often above 16 KiB.
MAX_RECORD = 128 * 1024
MAX_TEXT = 2048
MAX_ENTRIES = 200
READ_BUDGET = 32 * 1024
MAX_BATCH_ENTRIES = 128


def clean(text):
    # Untrusted output is plain text, never terminal escapes or Rich markup.
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(text))
    if len(text) > MAX_TEXT:
        marker = '\n… [display shortened; full bytes in raw file] …\n'
        side = (MAX_TEXT - len(marker)) // 2
        return text[:side] + marker + text[-side:]
    return text


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
        text = self.raw if raw else self.text
        if self.kind == 'text':  # codex exec's current human output: one line stays one line.
            return f'capture={self.capture} | {text}' if self.capture else text
        return f'event={self.event or "—"} · capture={self.capture or "— (historical)"} · {self.kind}\n{text}'


def content_text(content):
    parts = []
    for block in content:
        kind = block.get('type')
        if kind == 'text':
            parts.append(block['text'])
        elif kind == 'tool_use':
            parts.append(f'tool {block.get("name", "unknown")} id={block.get("id", "?")}: {json.dumps(block.get("input", {}), ensure_ascii=False)}')
        elif kind == 'tool_result':
            value = block.get('content', '')
            if isinstance(value, list):
                value = content_text(value)
            parts.append(f'tool result id={block.get("tool_use_id", "?")}'
                         f'{" [error]" if block.get("is_error") else ""}:\n{value}')
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return '\n'.join(parts)


def decode(raw, capture):
    plain = clean(raw.decode('utf-8', errors='replace'))
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, UnicodeError):
        return Entry(None, capture, 'text', plain, plain)
    kind = str(data.get('type', 'unknown'))
    text = plain
    try:
        if kind == 'stream_event':  # Optional variant; production doesn't request deltas.
            event = data.get('event', {})
            text = event.get('delta', {}).get('text') or json.dumps(event, ensure_ascii=False)
        elif kind in {'assistant', 'user'}:
            text = content_text(data.get('message', {}).get('content', [])) or plain
        elif kind in {'item.started', 'item.completed', 'item.updated'}:  # Optional Codex JSONL.
            item = data.get('item', {})
            text = item.get('text') or f'{item.get("type", "item")}: {item.get("command", "")}\n{item.get("aggregated_output", "")}'
        elif kind in {'result', 'error'}:
            text = data.get('result') or data.get('message') or plain
            if data.get('is_error'):
                kind += ' [error]'
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
        self.fragmented = False
        self.remainder_capture = None

    def append(self, entry):
        if len(self.entries) == MAX_ENTRIES:
            self.discarded += 1
        self.entries.append(entry)
        self.total += 1

    def reset_record(self):
        self.pending = b''
        self.capture = None
        self.fragmented = False
        self.remainder_capture = None

    def feed(self, chunk, capture=None):
        if not self.pending:
            self.capture = capture
        self.pending += chunk
        following_capture = capture if chunk else self.remainder_capture
        self.remainder_capture = following_capture
        processed = 0
        while self.pending and processed < MAX_BATCH_ENTRIES:
            newline = self.pending.find(b'\n')
            if 0 <= newline < MAX_RECORD:
                record, self.pending = self.pending[:newline], self.pending[newline + 1:]
                if self.fragmented:
                    text = clean(record.decode('utf-8', errors='replace'))
                    self.append(Entry(None, self.capture, 'oversized raw remainder', text, text))
                    self.fragmented = False
                else:
                    self.append(decode(record, self.capture))
            elif len(self.pending) >= MAX_RECORD:
                record, self.pending = self.pending[:MAX_RECORD], self.pending[MAX_RECORD:]
                text = clean(record.decode('utf-8', errors='replace'))
                self.append(Entry(None, self.capture, 'oversized raw fragment; use raw file', text, text))
                self.fragmented = True
            else:
                break
            self.capture = following_capture
            processed += 1

    def ready(self):
        newline = self.pending.find(b'\n')
        return 0 <= newline < MAX_RECORD or len(self.pending) >= MAX_RECORD

    def preview(self):
        return decode(self.pending, self.capture) if self.pending and not self.ready() else None


class Tail:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.initial_size = 0
        self.file_id = None
        self.error = None
        try:
            stat = path.stat()
            self.initial_size = stat.st_size
            self.file_id = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            pass
        except OSError as error:
            self.error = str(error)
        self.buffer = Buffer()

    def poll(self):
        # Drain bounded queued records before reading more bytes. Tiny-line floods
        # cannot cause tens of thousands of JSON parses in one UI callback.
        if self.buffer.ready():
            self.buffer.feed(b'')
            return
        try:
            stat = self.path.stat()
            file_id = (stat.st_dev, stat.st_ino)
            if stat.st_size < self.offset or (self.file_id is not None and self.file_id != file_id):
                self.offset = 0
                self.initial_size = stat.st_size
                self.buffer.reset_record()
                self.buffer.append(Entry(None, None, 'observer', 'Raw file truncated/replaced; restarted', ''))
            self.file_id = file_id
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                historical = self.offset < self.initial_size
                limit = min(READ_BUDGET, self.initial_size - self.offset) if historical else READ_BUDGET
                chunk = stream.read(limit)
            self.error = None
        except OSError as error:
            self.error = str(error)
            return
        if chunk:
            self.offset += len(chunk)
            captured = None if historical else datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            self.buffer.feed(chunk, captured)
