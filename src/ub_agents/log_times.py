"""Optional supervisor observation times, independent of runtime log bytes.

process.times is a fixed-width binary index: a versioned header identifies the
log's device/inode, followed by (exclusive byte end, UTC epoch microseconds)
pairs. One pair covers each observed growth, including any partial record.
Fixed widths allow bounded seeks into long runs without replaying the index.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import stat
import struct
import time

HEADER = struct.Struct(">8sQQ")
GROWTH = struct.Struct(">Qq")
MAGIC = b"UBTIME1\n"
TIMES_BUDGET = 8192
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class LogTimes:
    """Best-effort timing must never interfere with supervision or cleanup."""

    def __init__(self, path, log):
        self.stream = None
        self.log = log
        self.end = 0
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, 0o600)
            self.stream = os.fdopen(fd, "wb", buffering=0)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Timing index is not a regular file")
            self.stream.truncate(0)
            info = os.fstat(log.fileno())
            self.stream.write(HEADER.pack(MAGIC, info.st_dev, info.st_ino))
        except OSError:
            self.close()

    def observe(self):
        if self.stream is None:
            return
        try:
            end = os.fstat(self.log.fileno()).st_size
            if end > self.end:
                self.stream.write(GROWTH.pack(end, time.time_ns() // 1000))
                self.end = end
        except OSError:
            self.close()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


@dataclass(frozen=True)
class TimeWindow:
    start: int = 0
    end: int | None = None
    ends: tuple = ()
    stamps: tuple = ()
    bytes_read: int = 0

    def capture(self, offset, fallback=None):
        index = bisect_right(self.ends, offset)
        if offset >= self.start and index < len(self.stamps):
            return self.stamps[index]
        return fallback


def read_times(path, log_info, position, *, backwards=False, log_fd=None):  # noqa: C901, PLR0915
    """Read at most TIMES_BUDGET bytes; invalid/missing metadata is invisible.

    A dense index narrows the log window instead of losing valid times or
    exceeding the caller's read budget. Truncated final pairs are ignored.
    """
    used = 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb", buffering=0) as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return TimeWindow()

            def read_at(offset, size):
                nonlocal used
                if used + size > TIMES_BUDGET:
                    raise ValueError("Timing read budget exhausted")
                stream.seek(offset)
                data = stream.read(size)
                used += len(data)
                if len(data) != size:
                    raise ValueError("Truncated timing index")
                return data

            header = HEADER.unpack(read_at(0, HEADER.size))
            if header != (MAGIC, log_info.st_dev, log_info.st_ino):
                raise ValueError("Unrelated timing index")
            count = max(0, (info.st_size - HEADER.size) // GROWTH.size)

            def growth(index):
                end, stamp = GROWTH.unpack(read_at(HEADER.size + index * GROWTH.size, GROWTH.size))
                # The supervisor can publish another growth after the caller's
                # log stat. Recheck the open log before rejecting that offset.
                size = log_info.st_size
                if end > size and log_fd is not None:
                    size = os.fstat(log_fd).st_size
                if not 0 < end <= size:
                    raise ValueError("Invalid timing offset")
                return end, (EPOCH + timedelta(microseconds=stamp)).isoformat()

            low, high = 0, count
            lower, upper = 0, 2**64
            while low < high:
                mid = (low + high) // 2
                end, _ = growth(mid)
                if not lower < end < upper:
                    raise ValueError("Unordered timing index")
                if end <= position:
                    low, lower = mid + 1, end
                else:
                    high, upper = mid, end

            # Include a preceding pair to validate the first interval's start.
            room = (TIMES_BUDGET - used) // GROWTH.size - 1
            first = max(0, min(low + 1, count) - room) if backwards else low
            last = min(count, low + 1) if backwards else min(count, first + room)
            start = growth(first - 1)[0] if first else 0
            ends, stamps = [], []
            previous = start
            for index in range(first, last):
                end, stamp = growth(index)
                if end <= previous:
                    raise ValueError("Unordered timing index")
                ends.append(end)
                stamps.append(stamp)
                previous = end
            limit = previous if not backwards and last < count else None
            return TimeWindow(start, limit, tuple(ends), tuple(stamps), used)
    except (OSError, ValueError, OverflowError, struct.error):
        return TimeWindow(bytes_read=used)
