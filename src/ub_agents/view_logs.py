"""Indexed #112 reader and immutable, bounded disk pages for #114.

The #111 view.py separation between ingestion and displayed page is retained.
Byte positions, generation validation and paged raw-fragment notices stabilize it.
"""

from dataclasses import dataclass
import os
import stat

from .log_format import codex_notice, raw_entry, skipped_entry
from .log_reader import ANCHOR_BYTES, EntryRef, LogReader, MAX_ENTRIES, READ_BUDGET

PAGE_BYTES = READ_BUDGET


class FileChanged(ValueError):
    pass


@dataclass(frozen=True)
class Page:
    refs: tuple[EntryRef, ...]
    start: int
    end: int
    generation: int
    total: int
    notice: str = ''


class ViewReader(LogReader):
    def _generation(self, info, historical):
        super()._generation(info, historical)
        # The base reader retains a reset marker with old entries for its CLI.
        # A view window never combines source generations.
        self.entries.clear()
        self.refs.clear()
        self._attach_tail = self.offset > 0

    def _consume(self, chunk, capture):
        # A tiny-record historical flood must not delay the recent tail behind
        # thousands of old records. Inspect only the already bounded first read.
        cut = 0
        if getattr(self, '_attach_tail', False):
            self._attach_tail = False
            pieces = chunk.rsplit(b'\n', MAX_ENTRIES + 1)
            if len(pieces) > MAX_ENTRIES + 1:
                cut = len(pieces[0]) + 1
                self.skipped += cut
                self.record_start = self.offset + cut
                self.raw_kind = None
        offset = self.offset
        self.offset += cut
        try:
            return cut + super()._consume(chunk[cut:], capture)
        finally:
            self.offset = offset

    def page(self):
        refs = tuple(self.refs)
        preview = self.preview()
        if preview is not None:
            refs += (EntryRef(self.record_start, self.offset, self.total_entries + 1, preview),)
        return Page(refs, refs[0].start if refs else self.offset,
                    refs[-1].end if refs else self.offset, self.resets, self.total_entries)

    def older(self, end, generation):
        if generation != self.resets:
            raise FileChanged('File changed; paused page belongs to an earlier generation. Press f for latest.')
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            self._validate(stream, before)
            end = min(end, before.st_size)
            start = max(0, end - PAGE_BYTES)
            stream.seek(max(0, start - 1))
            preceding = stream.read(1) if start else b'\n'
            data = stream.read(end - start)
            # Keep at most 200 records, selecting from the end so older navigation
            # can continue at the first displayed byte without dropping history.
            pieces = data.rsplit(b'\n', MAX_ENTRIES + 1)
            notice = ''
            if len(pieces) > MAX_ENTRIES + 1:
                dropped = len(pieces[0]) + 1
                data, start = data[dropped:], start + dropped
                preceding = b'\n'
                notice = 'Entry limit boundary; h recovers earlier bytes; full raw file available.'
            parser = ViewReader(self.path, self.runtime, attach=False)
            parser.offset = parser.record_start = start
            if start and preceding != b'\n':
                parser.raw_kind = 'partial raw record (page boundary); full record in raw file'
                notice = ('Page byte boundary splits a record; h continues toward byte zero; '
                          'full record in raw file.' if self.runtime == 'codex' else
                          'Page byte boundary splits a record; fragment is raw; h continues toward byte zero.')
            index = 0
            while index < len(data):
                parser.processed = 0
                consumed = parser._consume(data[index:], None)
                if not consumed:
                    break
                parser.offset += consumed
                index += consumed
            refs = tuple(parser.refs)
            if parser.pending:
                kind = 'partial raw record (page end); full record in raw file'
                project = (skipped_entry if self.runtime == 'claude' and parser.raw_kind and
                           'page boundary' in parser.raw_kind else raw_entry)
                value = project(bytes(parser.pending), None, kind)
                if self.runtime == 'codex':
                    value = codex_notice(value, 'incomplete Codex record omitted')
                refs += (EntryRef(parser.record_start, parser.offset, parser.total_entries + 1,
                                  value),)
            after = os.fstat(stream.fileno())
            self._validate(stream, after)
            current = self.path.stat()
            if (current.st_dev, current.st_ino) != self.file_id:
                raise FileChanged('File replaced during older read; page discarded. Press f for latest.')
            if after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns:
                raise FileChanged('File changed during older read; page discarded. Press f for latest.')
        return Page(refs, start, end, self.resets, self.total_entries, notice)

    def _validate(self, stream, info):
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Log is not a regular file')
        if self.file_id != (info.st_dev, info.st_ino) or info.st_size < self.size:
            raise FileChanged('File truncated/replaced; older page discarded. Press f for latest.')
        if self.anchor:
            stream.seek(self.offset - len(self.anchor))
            if stream.read(min(ANCHOR_BYTES, len(self.anchor))) != self.anchor:
                raise FileChanged('File rewritten; older page discarded. Press f for latest.')
