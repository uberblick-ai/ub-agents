"""Bounded incremental read-only process.log reader, for future local views.

No locks, writes, GitHub access, runtime invocation or workflow decisions. Each
update opens a regular file and closes it before returning an immutable snapshot.
Unread records stay on disk when the update budget runs out. A generation change
discards unfinished bytes, timing and tool pairing, while retaining old entries.
"""

from collections import deque
import codecs
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from .log_format import (ClaudeFormatter, CodexFormatter, Entry, MAX_RECORD, entry, inert, raw_entry,
                         shorten, skipped_entry, with_elapsed)

MAX_ENTRIES = 200
READ_BUDGET = 32 * 1024
RECORD_BUDGET = 128
TAIL_BYTES = 32 * 1024
ANCHOR_BYTES = 64


@dataclass(frozen=True)
class EntryRef:
    start: int
    end: int
    serial: int
    value: Entry


@dataclass(frozen=True)
class Snapshot:
    entries: tuple[Entry, ...]
    unfinished: Entry | None
    raw_path: Path
    bytes_read: int
    records_processed: int
    unread_bytes: int
    pending_bytes: int
    skipped_bytes: int
    evicted_entries: int
    shortened_entries: int
    resets: int
    error: str | None
    refs: tuple[EntryRef, ...] = ()
    total_entries: int = 0

    def notice(self):
        return (f"{len(self.entries)}/{MAX_ENTRIES} entries; shortened {self.shortened_entries}; "
                f"evicted {self.evicted_entries}; skipped {self.skipped_bytes} historical bytes; "
                f"unread lag {self.unread_bytes} bytes; unfinished {self.pending_bytes} bytes; "
                f"file resets {self.resets}\nFull raw: {inert(str(self.raw_path))}" +
                (f"\nRead error: {self.error}" if self.error else ""))


class LogReader:
    def __init__(self, path, runtime, clock=None, *, attach=True):
        self.path = Path(path).absolute()
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.formatter = CodexFormatter() if runtime == "codex" else ClaudeFormatter()
        self.entries = deque(maxlen=MAX_ENTRIES)
        self.refs = deque(maxlen=MAX_ENTRIES)
        self.total_entries = 0
        self.generation_start = 0
        self.record_start = 0
        self.pending = bytearray()
        self.capture = None
        self.raw_kind = None
        self.offset = self.initial_size = self.size = 0
        self.file_id = None
        self.anchor = b""
        self.skipped = self.evicted = self.shortened = self.resets = 0
        self.bytes_read = self.processed = 0
        self.error = None
        self._tail_start = False
        if not attach:
            return
        try:
            info = self.path.stat()
            self._generation(info, historical=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.error = shorten(inert(str(exc)))

    def _generation(self, info, historical):
        self.file_id = (info.st_dev, info.st_ino)
        self.size = info.st_size
        self.initial_size = info.st_size if historical else 0
        self.offset = max(0, info.st_size - TAIL_BYTES)
        self.record_start = self.offset
        self.skipped += self.offset
        self._tail_start = self.offset > 0
        self.pending.clear()
        self.capture = self.raw_kind = None
        self.anchor = b""
        self.formatter.reset()
        self.generation_start = self.total_entries

    def _append(self, value, end=None):
        if value.progress:
            for index, ref in enumerate(self.refs):
                if ref.serial <= self.generation_start:
                    continue
                updated = with_elapsed(ref.value, *value.progress)
                if updated is not ref.value:
                    self.entries[index] = updated
                    self.refs[index] = replace(ref, value=updated)
                    self.shortened += updated.shortened and not ref.value.shortened
        if len(self.entries) == MAX_ENTRIES:
            self.evicted += 1
        self.entries.append(value)
        self.total_entries += 1
        self.refs.append(EntryRef(self.record_start, end if end is not None else self.offset,
                                  self.total_entries, value))
        self.shortened += value.shortened

    def _emit(self, oversized=False, end=None):
        raw = bytes(self.pending)
        trailing = b""
        kind = self.raw_kind
        if oversized:
            if not kind:
                kind = "oversized raw fragment (>128 KiB)"
            elif "oversized" not in kind:
                kind += "; oversized raw fragment"
            self.raw_kind = kind
            # An oversized raw fragment may end in the middle of valid UTF-8.
            # Carry at most three unfinished bytes into the next raw fragment.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoder.decode(raw, final=False)
            trailing, _ = decoder.getstate()
            if trailing:
                raw = raw[:-len(trailing)]
        if kind:
            self.formatter.last_call = None
        skipped = self.runtime == "claude" and kind in (
            "partial first raw record (tail start)",
            "partial raw record (page boundary); full record in raw file")
        value = (skipped_entry(raw, self.capture, kind) if skipped else
                 raw_entry(raw, self.capture, kind) if kind else
                 self.formatter.decode(raw, self.capture) if self.runtime in ("claude", "codex") else
                 raw_entry(raw, self.capture))
        self._append(value, end)
        if end is not None:
            self.record_start = end - len(trailing)
        self.pending.clear()
        self.pending.extend(trailing)
        self.processed += 1

    def _consume(self, chunk, capture):
        index = 0
        while index < len(chunk) and self.processed < RECORD_BUDGET:
            if not self.pending and self.raw_kind is None:
                self.capture = capture
            newline = chunk.find(b"\n", index)
            end = len(chunk) if newline < 0 else newline
            take = min(end - index, MAX_RECORD - len(self.pending))
            self.pending.extend(chunk[index:index + take])
            index += take
            if index < len(chunk) and chunk[index] == 10:
                self._emit(end=self.offset + index + 1)
                index += 1
                self.capture = self.raw_kind = None
            elif len(self.pending) == MAX_RECORD and index < len(chunk):
                self._emit(oversized=True, end=self.offset + index)
            else:
                break
        return index

    def preview(self):
        """Unfinished bytes are always raw, even if they look like valid JSON."""
        if not self.pending:
            return None
        return raw_entry(bytes(self.pending), self.capture,
                         self.raw_kind or "unfinished raw record")

    def snapshot(self):
        preview = self.preview()
        return Snapshot(tuple(self.entries), preview, self.path, self.bytes_read, self.processed,
                        max(0, self.size - self.offset), len(self.pending), self.skipped,
                        self.evicted, self.shortened + bool(preview and preview.shortened),
                        self.resets, self.error, tuple(self.refs), self.total_entries)

    def update(self):
        self.bytes_read = self.processed = 0
        try:
            # O_NONBLOCK prevents a substituted FIFO from hanging an observer.
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Log is not a regular file")
                changed = (self.file_id is not None and
                           (self.file_id != (info.st_dev, info.st_ino) or info.st_size < self.size))
                if not changed and self.anchor:
                    stream.seek(self.offset - len(self.anchor))
                    check = stream.read(len(self.anchor))
                    self.bytes_read += len(check)
                    changed = check != self.anchor
                if self.file_id is None or changed:
                    self._generation(info, historical=changed)
                    if changed:
                        self.resets += 1
                        self._append(entry("reader", "Raw file truncated/replaced; new generation", ""))
                self.size = info.st_size
                if self._tail_start:
                    stream.seek(self.offset - 1)
                    preceding = stream.read(1)
                    self.bytes_read += len(preceding)
                    if preceding != b"\n":
                        self.raw_kind = "partial first raw record (tail start)"
                    self._tail_start = False
                stream.seek(self.offset)
                historical = self.offset < self.initial_size
                limit = READ_BUDGET - self.bytes_read
                if historical:
                    limit = min(limit, self.initial_size - self.offset)
                chunk = stream.read(limit)
                self.bytes_read += len(chunk)
                capture = None if historical else self.clock().astimezone(timezone.utc).isoformat()
                consumed = self._consume(chunk, capture)
                if consumed:
                    self.offset += consumed
                    self.anchor = (self.anchor + chunk[:consumed])[-ANCHOR_BYTES:]
                self.error = None
        except OSError as exc:
            self.error = shorten(inert(str(exc)))
        return self.snapshot()
