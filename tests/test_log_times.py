from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.log_reader import LogReader, READ_BUDGET, TAIL_BYTES
from ub_agents.log_times import (EPOCH, GROWTH, HEADER, LogTimes, MAGIC, TIMES_BUDGET,
                                 growth_position, growth_window, read_times)
from ub_agents.view_logs import ViewReader
from ub_agents.view_worker import LocalWorker, Request
from tests.test_view_data import fixture

NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def record(text="hello\ncontinued", runtime="codex", **extra):
    data = ({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": text}}
            if runtime == "codex" else
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    return json.dumps(data | extra).encode() + b"\n"


class LogTimesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.log = self.root / "process.log"
        self.log.touch()
        self.path = self.root / "process.times"

    def write_index(self, pairs):
        info = self.log.stat()
        self.path.write_bytes(HEADER.pack(MAGIC, info.st_dev, info.st_ino) +
                              b"".join(GROWTH.pack(end, stamp) for end, stamp in pairs))

    def timed_output(self, chunks):
        with self.log.open("ab", buffering=0) as stream:
            times = LogTimes(self.path, stream)
            self.addCleanup(times.close)
            for index, chunk in enumerate(chunks):
                stream.write(chunk)
                stamp = NOW + timedelta(seconds=index)
                with patch("ub_agents.log_times.time.time_ns", return_value=int(stamp.timestamp()) * 10**9):
                    times.observe()
        return times

    def drain(self, reader):
        for _ in range(2000):
            result = reader.update()
            self.assertLessEqual(result.bytes_read, READ_BUDGET)
            if not result.unread_bytes:
                return result
        self.fail("Reader did not drain")

    def test_growth_position_uses_exclusive_byte_ends_and_rejects_unordered_search(self):
        pairs = [(10, "a"), (20, "b"), (30, "c")]
        for position, expected in ((0, 0), (9, 0), (10, 1), (19, 1), (20, 2), (30, 3)):
            with self.subTest(position=position):
                self.assertEqual(growth_position(pairs.__getitem__, len(pairs), position), expected)
        self.assertEqual(growth_position(pairs.__getitem__, 0, 0), 0)
        unordered = [(30, "a"), (20, "b"), (40, "c")]
        with self.assertRaisesRegex(ValueError, "Unordered timing index"):
            growth_position(unordered.__getitem__, len(unordered), 0)

    def test_growth_window_preserves_preceding_interval_and_directional_limits(self):
        pairs = [(10, "a"), (20, "b"), (30, "c"), (40, "d")]
        for low, room, backwards, expected in (
                (2, 1, False, (20, 30, (30,), ("c",))),
                (2, 2, True, (10, None, (20, 30), ("b", "c"))),
                (0, 2, True, (0, None, (10,), ("a",))),
                (4, 2, True, (20, None, (30, 40), ("c", "d"))),
                (4, 2, False, (40, None, (), ()))):
            with self.subTest(low=low, room=room, backwards=backwards):
                self.assertEqual(growth_window(pairs.__getitem__, len(pairs), low, room, backwards), expected)
        unordered = [(10, "a"), (10, "b")]
        with self.assertRaisesRegex(ValueError, "Unordered timing index"):
            growth_window(unordered.__getitem__, len(unordered), 1, 1, False)

    def test_historical_partial_and_live_records_use_their_first_observed_byte(self):
        raw = record()
        times = self.timed_output([raw[:17]])
        reader = LogReader(self.log, "codex", lambda: NOW + timedelta(hours=1))
        self.assertEqual(reader.update().unfinished.capture, NOW.isoformat())
        with self.log.open("ab", buffering=0) as stream:
            times.log = stream
            stream.write(raw[17:] + raw)
            with patch("ub_agents.log_times.time.time_ns", return_value=int(NOW.timestamp() + 1) * 10**9):
                times.observe()
        entries = self.drain(reader).entries
        self.assertEqual([value.capture for value in entries],
                         [NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()])
        self.assertTrue(all(value.text.startswith("~") for value in entries))
        self.assertTrue(all(value.text.splitlines()[1] == "          continued" for value in entries))
        # A fresh attachment after completion recovers exactly the same times.
        reopened = self.drain(LogReader(self.log, "codex"))
        self.assertEqual(reopened.entries, entries)
        self.assertEqual(self.log.read_bytes(), raw * 2)

    def test_claude_producer_timestamp_wins_and_untimed_records_use_sidecar(self):
        self.timed_output([record(runtime="claude"),
                           record(runtime="claude", timestamp="2026-10-03T12:00:00Z")])
        first, second = self.drain(LogReader(self.log, "claude")).entries
        self.assertEqual(first.capture, NOW.isoformat())
        self.assertTrue(first.text.startswith("~"))
        self.assertEqual(second.event, "2026-10-03T12:00:00+00:00")
        self.assertTrue(second.text.startswith(" "))
        self.assertNotIn("~", second.text)

    def test_tail_and_older_pages_recover_all_historical_times(self):
        chunks = [record(str(index)) for index in range(1800)]
        self.timed_output(chunks)
        reader = ViewReader(self.log, "codex")
        self.drain(reader)
        page = reader.page()
        self.assertGreater(page.start, 0)
        seen = set()
        while True:
            for ref in page.refs:
                if ref.value.kind == "assistant":
                    index = int(ref.value.text[10:])
                    seen.add(index)
                    self.assertEqual(ref.value.capture, (NOW + timedelta(seconds=index)).isoformat())
                    self.assertTrue(ref.value.text.startswith("~"))
            if not page.start:
                break
            previous = page.start
            page = reader.older(previous, reader.resets)
            self.assertLess(page.start, previous)
        self.assertEqual(seen, set(range(1800)))

    def test_worker_reload_recovers_times_at_tail(self):
        session, log, _ = fixture(self.root, runtime="codex:synthetic-model:high")
        self.log, self.path = log, log.with_suffix(".times")
        self.timed_output([record("old") * 500, record("latest")])
        worker = LocalWorker(self.root, session)
        request = Request("assignment:owned-run", 1, chosen=True)
        first = worker.read(request)
        original = worker.readers[str(log)]
        reloaded = worker.read(Request(request.key, 2, chosen=True, reload=True))
        self.assertIsNot(worker.readers[str(log)], original)
        for _ in range(10):
            if not reloaded.log.unread_bytes:
                break
            reloaded = worker.read(request)
        self.assertLessEqual(reloaded.log.bytes_read, READ_BUDGET)
        latest = reloaded.page.refs[-1].value
        self.assertEqual(latest.capture, (NOW + timedelta(seconds=1)).isoformat())
        self.assertTrue(latest.text.startswith("~"))
        self.assertIn("latest", latest.text)
        self.assertGreater(first.page.start, 0)

    def test_missing_empty_malformed_or_non_regular_sidecar_only_drops_times(self):
        self.log.write_bytes(record() * 400)
        expected = self.drain(ViewReader(self.log, "codex"))
        info = self.log.stat()
        header = HEADER.pack(MAGIC, info.st_dev, info.st_ino)
        cases = [b"", b"garbage", header[:10], header + b"partial",
                 header + GROWTH.pack(0, 0), header + GROWTH.pack(info.st_size + 1, 0),
                 header + GROWTH.pack(info.st_size, 2**63 - 1),
                 header + GROWTH.pack(info.st_size, 0) * 2]
        for damaged in cases:
            with self.subTest(damaged=damaged):
                self.path.write_bytes(damaged)
                reader = ViewReader(self.log, "codex")
                result = self.drain(reader)
                self.assertIsNone(result.error)
                self.assertEqual(result.entries, expected.entries)
                older = reader.older(reader.page().start, reader.resets)
                self.assertTrue(all(ref.value.capture is None for ref in older.refs))
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.drain(ViewReader(self.log, "codex")).entries, expected.entries)
        self.path.rmdir()
        os.mkfifo(self.path)
        self.assertEqual(self.drain(ViewReader(self.log, "codex")).entries, expected.entries)

    def test_truncated_final_pair_preserves_earlier_times_and_reader_is_read_only(self):
        raw = record()
        self.timed_output([raw, raw])
        self.path.write_bytes(self.path.read_bytes()[:-3])
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.log, self.path)]
        entries = self.drain(LogReader(self.log, "codex")).entries
        self.assertEqual(entries[0].capture, NOW.isoformat())
        self.assertIsNone(entries[1].capture)
        self.assertTrue(entries[1].text.startswith(" " * 10))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.log, self.path)], before)

    def test_sidecar_from_replaced_log_is_ignored(self):
        self.timed_output([record()])
        reader = LogReader(self.log, "codex")
        self.drain(reader)
        replacement = self.log.with_suffix(".replacement")
        replacement.write_bytes(record("replacement"))
        replacement.replace(self.log)
        self.assertIsNone(self.drain(reader).entries[-1].capture)
        self.assertIsNone(self.drain(LogReader(self.log, "codex")).entries[-1].capture)

    def test_growth_published_after_log_stat_keeps_historical_times(self):
        self.log.write_bytes(record())
        reader = LogReader(self.log, "codex")
        original = read_times
        def append_then_read(path, info, position, **kwargs):
            self.timed_output([record()])
            return original(path, info, position, **kwargs)
        with patch("ub_agents.log_reader.read_times", side_effect=append_then_read):
            snapshot = reader.update()
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.entries[0].capture, NOW.isoformat())

    def test_dense_index_bounds_reads_without_losing_record_times(self):
        reader = LogReader(self.log, "codex")
        raw = record("x" * (TAIL_BYTES * 2))
        self.log.write_bytes(raw)
        stamp = int((NOW - EPOCH).total_seconds()) * 10**6
        self.write_index((end, stamp + end) for end in range(1, len(raw) + 1))
        reader.initial_size = len(raw)
        snapshot = self.drain(reader)
        self.assertEqual(snapshot.entries[-1].capture, (NOW + timedelta(microseconds=1)).isoformat())
        for backwards in (False, True):
            window = read_times(self.path, self.log.stat(), len(raw) // 2, backwards=backwards)
            self.assertLessEqual(window.bytes_read, TIMES_BUDGET)
            self.assertTrue(window.ends)
        reader = ViewReader(self.log, "codex")
        self.drain(reader)
        page = reader.page()
        while page.start:
            previous = page.start
            page = reader.older(previous, reader.resets)
            self.assertLess(page.start, previous)

    def test_huge_sparse_damaged_index_has_bounded_reads(self):
        self.log.write_bytes(record())
        self.write_index([])
        with self.path.open("r+b") as stream:
            stream.truncate(2**40)
        snapshot = self.drain(LogReader(self.log, "codex"))
        self.assertIsNone(snapshot.error)
        self.assertIsNone(snapshot.entries[0].capture)
        self.assertLessEqual(snapshot.bytes_read, READ_BUDGET)
