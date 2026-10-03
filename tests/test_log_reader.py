from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.log_format import ClaudeFormatter, MAX_RECORD, MAX_TEXT, MAX_TOOLS, SHORTENED, inert
from ub_agents.log_reader import LogReader, MAX_ENTRIES, READ_BUDGET, RECORD_BUDGET, TAIL_BYTES

FIXTURE = Path(__file__).parent / "fixtures" / "runtime_logs" / "claude.log"
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def record(kind="assistant", content=None, **extra):
    if content is None:
        content = [{"type": "text", "text": "hello café"}]
    return json.dumps({"type": kind, "message": {"role": kind, "content": content},
                       **extra}, ensure_ascii=False).encode() + b"\n"


def tool(tool_id="a", name="Bash"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": "printf hello"}}


def result(tool_id="a", **extra):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "hello", **extra}


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.formatter = ClaudeFormatter()

    def decode(self, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        return self.formatter.decode(raw)

    def test_distinct_messages_calls_paired_results_and_errors(self):
        message = self.decode(record())
        call = self.decode(record(content=[tool()]))
        output = self.decode(record("user", [result()]))
        error = self.decode(record("user", [result(is_error=True, content="Exit code 1")]))
        runtime = self.decode({"type": "error", "error": {"message": "API unavailable"}})
        self.assertEqual([e.kind for e in (message, call, output, error, runtime)],
                         ["assistant", "tool call", "tool result", "tool ERROR", "runtime ERROR"])
        self.assertIn("Bash id=a", output.text)
        self.assertIn("ERROR", error.display())
        self.assertIn("Exit code 1", error.text)
        self.assertIn("API unavailable", runtime.text)

    def test_multiple_tools_pair_by_id_and_unpaired_results_are_honest(self):
        self.decode(record(content=[tool("a", "Read"), tool("b", "Bash")]))
        value = self.decode(record("user", [result("b"), result("a"), result("missing")]))
        self.assertIn("Bash id=b", value.text)
        self.assertIn("Read id=a", value.text)
        self.assertIn("unpaired id=missing", value.text)

    def test_pairing_cache_is_bounded_and_invalid_records_do_not_change_it(self):
        for index in range(MAX_TOOLS + 10):
            self.decode(record(content=[tool(str(index))]))
        self.assertEqual(len(self.formatter.tools), MAX_TOOLS)
        self.assertNotIn("0", self.formatter.tools)
        self.assertEqual(self.decode(record(content=[tool("invalid"), {"type": "future"}])).kind, "raw text")
        self.assertNotIn("invalid", self.formatter.tools)

    def test_runtime_success_and_failure_are_only_display_entries(self):
        for subtype, failed, expected in (("success", False, "runtime result"),
                                          ("error_during_execution", True, "runtime ERROR")):
            value = self.decode({"type": "result", "subtype": subtype,
                                 "is_error": failed, "result": "needs-review",
                                 "errors": ["failed tool"] if failed else []})
            self.assertEqual(value.kind, expected)
            self.assertIn(subtype, value.text)
            self.assertFalse(hasattr(value, "outcome"))
            self.assertFalse(hasattr(value, "report"))

    def test_raw_fallback_for_unknown_malformed_mixed_and_invalid_shapes(self):
        values = [b"diagnostic: unavailable", b'{"type":"assistant"', b"null", b"[]",
                  b"42", b'{"type":"error","message":NaN}', b"\xff",
                  b'{"type":"assistant","message":{"content":[],"role":"assistant"}}',
                  b"[" * 1500 + b"0" + b"]" * 1500]
        values += [json.dumps(value).encode() for value in (
            {"type": "future", "timestamp": "2026-10-03T12:00:00Z"},
            {"type": ["assistant"]}, {"type": "assistant", "message": []},
            {"type": "assistant", "message": {"content": [None]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": {}}]}},
            {"type": "assistant", "message": {"content": [tool(name=None)]}},
            {"type": "user", "message": {"content": [result(is_error="false")]}},
            {"type": "user", "message": {"content": [result(content=[{"type": "image"}])]}},
            {"type": "result", "subtype": "success", "result": {}},
            {"type": "error", "message": {}},
        )]
        for raw in values:
            with self.subTest(raw=raw[:80]):
                value = self.decode(raw)
                self.assertEqual(value.kind, "raw text")
                self.assertEqual(value.text, value.raw)
                self.assertIsNone(value.event)

    def test_nested_text_tool_result_and_mixed_assistant_content(self):
        self.decode(record(content=[{"type": "text", "text": "running"}, tool()]))
        value = self.decode(record("user", [result(content=[{"type": "text", "text": "one"},
                                                              {"type": "text", "text": "two"}])]))
        self.assertIn(r"one\ntwo", value.text)

    def test_all_terminal_controls_are_visible_in_all_projections(self):
        controls = "".join(chr(i) for i in (*range(32), *range(127, 160)))
        attacks = controls + "\x1b[2J\x1b]52;c;secret\x07\x9b31m\x9dtitle\x9c"
        values = [self.decode(attacks.encode()), self.decode(record(content=[{"type": "text", "text": attacks}])),
                  self.decode(record(content=[tool(attacks, attacks)])),
                  self.decode(record("user", [result(attacks, content=attacks, is_error=True)])),
                  self.decode({"type": "error", "message": attacks})]
        for value in values:
            for text in (value.text, value.raw, value.display(), value.display(raw=True)):
                self.assertFalse(any(ord(c) < 32 and c != "\n" or 127 <= ord(c) <= 159 for c in text))
                self.assertTrue(r"\x1b" in text or r"\u001b" in text)
                self.assertIn(r"\x9b", text)
        self.assertEqual(inert("\ud800"), r"\ud800")

    def test_projection_bounds_include_timing_and_labels(self):
        for raw in (b"a" * MAX_TEXT, record(content=[{"type": "text", "text": "A" * 10000 + "END"}]),
                    b"\x1b" * MAX_TEXT):
            value = self.decode(raw)
            self.assertTrue(value.shortened)
            for display in (value.display(), value.display(raw=True)):
                self.assertLessEqual(len(display), MAX_TEXT)
                self.assertIn(SHORTENED, display)

    def test_oversized_valid_json_is_never_interpreted(self):
        raw = record(content=[{"type": "text", "text": "a" * MAX_RECORD}])
        value = self.decode(raw)
        self.assertIn("oversized raw", value.kind)
        self.assertIsNone(value.event)

    def test_only_aware_iso_producer_timestamps_are_accepted(self):
        valid = self.decode(record(timestamp="2026-10-03T14:00:00+02:00"))
        self.assertEqual(valid.event, "2026-10-03T12:00:00+00:00")
        self.assertIn("producer=", valid.display())
        for stamp in ("2026-10-03T14:00:00", "bad", 1, None, "a" * 100,
                      "0001-01-01T00:00:00+12:00"):
            self.assertIsNone(self.decode(record(timestamp=stamp)).event)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "process.log"
        self.path.touch()
        self.now = NOW

    def reader(self, runtime="claude"):
        return LogReader(self.path, runtime, lambda: self.now)

    def append(self, data):
        with self.path.open("ab") as stream:
            stream.write(data)

    def drain(self, reader):
        for _ in range(2000):
            snapshot = reader.update()
            self.assertLessEqual(snapshot.bytes_read, READ_BUDGET)
            self.assertLessEqual(snapshot.records_processed, RECORD_BUDGET)
            self.assertLessEqual(snapshot.pending_bytes, MAX_RECORD)
            self.assertLessEqual(len(snapshot.entries), MAX_ENTRIES)
            if not snapshot.unread_bytes:
                return snapshot
        self.fail("reader did not drain bounded fixture")

    def test_accepted_claude_recording_incrementally(self):
        reader = self.reader()
        data = FIXTURE.read_bytes()
        for index in range(0, len(data), 997):
            self.append(data[index:index + 997])
            self.drain(reader)
        snapshot = self.drain(reader)
        kinds = [value.kind for value in snapshot.entries]
        self.assertIn("assistant", kinds)
        self.assertIn("tool call", kinds)
        self.assertIn("tool result", kinds)
        self.assertIn("tool ERROR", kinds)
        self.assertEqual(kinds[-1], "runtime result")
        self.assertIn("success", snapshot.entries[-1].text)
        read_result = next(e for e in snapshot.entries if e.kind == "tool result")
        self.assertIn("Read id=<id-12>", read_result.text)
        error = next(e for e in snapshot.entries if e.kind == "tool ERROR")
        self.assertIn("Bash id=<id-22>", error.text)
        self.assertIn("No such file", error.text)
        self.assertGreater(snapshot.shortened_entries, 0)
        self.assertEqual(snapshot.entries[-1].capture, NOW.isoformat())
        self.assertTrue(read_result.event.startswith("2026-10-03T12:56:16"))
        self.assertEqual(self.path.read_bytes(), data)

    def test_records_and_utf8_sequences_split_at_every_byte(self):
        reader = self.reader()
        for byte in record():
            self.append(bytes([byte]))
            reader.update()
        snapshot = reader.snapshot()
        self.assertEqual(len(snapshot.entries), 1)
        self.assertEqual(snapshot.entries[0].kind, "assistant")
        self.assertIn("café", snapshot.entries[0].text)
        self.assertNotIn("\ufffd", snapshot.entries[0].text)
        self.assertEqual(snapshot.pending_bytes, 0)

    def test_unfinished_valid_json_stays_raw_until_newline(self):
        reader = self.reader()
        self.append(record()[:-1])
        snapshot = reader.update()
        self.assertFalse(snapshot.entries)
        self.assertEqual(snapshot.unfinished.kind, "unfinished raw record")
        self.assertEqual(snapshot.unfinished.capture, NOW.isoformat())
        self.append(b"\n")
        self.assertEqual(reader.update().entries[-1].kind, "assistant")

    def test_initial_bytes_unknown_and_live_bytes_have_first_capture_time(self):
        self.path.write_bytes(b"historical\nold partial")
        reader = self.reader()
        first = reader.update()
        self.assertIsNone(first.entries[0].capture)
        self.assertIn("unknown", first.entries[0].display())
        self.append(b" completed\nnew partial")
        reader.update()
        self.now = datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc)
        self.append(b" completed\nnext\n")
        entries = reader.update().entries
        self.assertIsNone(entries[1].capture)
        self.assertEqual(entries[2].capture, NOW.isoformat())
        self.assertEqual(entries[3].capture, self.now.isoformat())

    def test_preexisting_unread_bytes_do_not_get_later_capture_times(self):
        self.path.write_bytes(b"\n" * (RECORD_BUDGET * 3))
        reader = self.reader()
        reader.update()
        self.append(b"live\n")
        snapshot = self.drain(reader)
        self.assertTrue(all(e.capture is None for e in snapshot.entries[:-1]))
        self.assertEqual(snapshot.entries[-1].capture, NOW.isoformat())

    def test_other_runtimes_always_remain_raw_including_current_codex_json(self):
        data = (record() + b'{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}\n'
                b'{"type":"error","message":"Codex error"}\n' + b"ordinary diagnostic\n")
        for runtime in ("codex", "command", "unknown"):
            self.path.write_bytes(data)
            entries = self.drain(self.reader(runtime)).entries
            self.assertEqual(len(entries), 4)
            self.assertTrue(all(e.kind == "raw text" and e.text == e.raw for e in entries))

    def test_tiny_record_flood_has_bounded_work_eviction_and_visible_lag(self):
        reader = self.reader()
        self.append(b"\n" * READ_BUDGET)
        first = reader.update()
        self.assertEqual(first.records_processed, RECORD_BUDGET)
        self.assertGreater(first.unread_bytes, 0)
        self.assertIn("unread lag", first.notice())
        snapshot = self.drain(reader)
        self.assertEqual(len(snapshot.entries), MAX_ENTRIES)
        self.assertEqual(snapshot.evicted_entries, READ_BUDGET - MAX_ENTRIES)
        self.assertIn(f"evicted {snapshot.evicted_entries}", snapshot.notice())
        self.assertEqual(snapshot.raw_path, self.path.absolute())

    def test_large_existing_file_starts_near_tail_without_replaying_history(self):
        self.path.write_bytes(b"old\n" * (1024 * 1024) + b"LATEST\n")
        reader = self.reader()
        snapshot = self.drain(reader)
        self.assertGreater(snapshot.skipped_bytes, 4 * 1024 * 1024 - TAIL_BYTES)
        self.assertEqual(snapshot.entries[-1].text, "LATEST")
        self.assertTrue(all(e.capture is None for e in snapshot.entries))
        self.assertIn("Full raw:", snapshot.notice())

    def test_partial_first_tail_record_that_looks_like_json_is_never_parsed(self):
        structured = record()
        suffix = structured + b"x" * (TAIL_BYTES - len(structured) - 1) + b"\n"
        self.path.write_bytes(b"old prefix without newline" + suffix)
        reader = self.reader()
        snapshot = self.drain(reader)
        self.assertIn("partial first raw record", snapshot.entries[0].kind)
        self.assertIsNone(snapshot.entries[0].event)
        self.assertEqual(snapshot.entries[0].text, snapshot.entries[0].raw)

    def test_tail_start_on_record_boundary_keeps_complete_record_structured(self):
        data = record()
        suffix = data + b"x" * (TAIL_BYTES - len(data) - 1) + b"\n"
        self.path.write_bytes(b"old prefix\n" + suffix)
        snapshot = self.drain(self.reader())
        self.assertEqual(snapshot.entries[0].kind, "assistant")

    def test_oversized_records_keep_all_fragments_raw_and_recover_next_record(self):
        reader = self.reader()
        self.append(b"x" * (MAX_RECORD * 3) + record() + record())
        snapshot = self.drain(reader)
        fragments = snapshot.entries[:-1]
        self.assertTrue(all("oversized raw" in e.kind for e in fragments))
        self.assertEqual(snapshot.entries[-1].kind, "assistant")
        self.assertGreater(snapshot.shortened_entries, 0)
        self.assertTrue(all(len(e.display()) <= MAX_TEXT for e in snapshot.entries))

    def test_exactly_128_kib_is_still_a_structured_record(self):
        base = record(content=[{"type": "text", "text": ""}])[:-1]
        raw = record(content=[{"type": "text", "text": "a" * (MAX_RECORD - len(base))}])
        self.assertEqual(len(raw) - 1, MAX_RECORD)
        reader = self.reader()
        self.append(raw[:-1])
        self.drain(reader)
        self.assertEqual(len(reader.pending), MAX_RECORD)
        self.assertFalse(reader.entries)
        self.append(b"\n")
        snapshot = reader.update()
        self.assertEqual(snapshot.entries[-1].kind, "assistant")

    def test_utf8_split_at_oversized_fragment_boundary_keeps_characters(self):
        reader = self.reader()
        self.append(b"x" * (MAX_RECORD - 1) + "€café".encode() + b"\n")
        snapshot = self.drain(reader)
        self.assertEqual(len(snapshot.entries), 2)
        self.assertTrue(all("oversized raw" in e.kind for e in snapshot.entries))
        self.assertTrue(all("\ufffd" not in e.text for e in snapshot.entries))
        self.assertEqual(snapshot.entries[-1].text, "€café")

    def test_no_newline_flood_bounds_pending_and_labels_shortened_preview(self):
        reader = self.reader()
        self.append(b"x" * (MAX_RECORD * 10 + MAX_TEXT * 2))
        snapshot = self.drain(reader)
        self.assertLessEqual(snapshot.pending_bytes, MAX_RECORD)
        self.assertIn("oversized raw", snapshot.unfinished.kind)
        self.assertIn(SHORTENED, snapshot.unfinished.display())
        self.assertGreater(snapshot.shortened_entries, 0)

    def test_truncation_discards_old_unfinished_bytes_and_capture_time(self):
        reader = self.reader()
        self.append(b"old live unfinished bytes")
        reader.update()
        self.assertEqual(reader.capture, NOW.isoformat())
        self.path.write_bytes(b"new\n")
        snapshot = reader.update()
        self.assertEqual(snapshot.resets, 1)
        self.assertEqual(snapshot.entries[-1].text, "new")
        self.assertIsNone(snapshot.entries[-1].capture)
        self.assertEqual(snapshot.pending_bytes, 0)

    def test_replacement_discards_partial_timing_and_pairing_even_if_larger(self):
        reader = self.reader()
        self.append(record(content=[tool()]) + b"old partial")
        reader.update()
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(record("user", [result()]) + b"new padding" * 50 + b"\n")
        replacement.replace(self.path)
        snapshot = self.drain(reader)
        self.assertEqual(snapshot.resets, 1)
        output = next(e for e in snapshot.entries if e.kind == "tool result")
        self.assertIn("unpaired id=a", output.text)
        self.assertIsNone(output.capture)
        self.assertNotIn("old partial", snapshot.entries[-1].text)

    def test_truncate_and_regrow_between_updates_detected_by_byte_anchor(self):
        reader = self.reader()
        self.append(b"old partial")
        reader.update()
        self.path.write_bytes(b"new generation is longer\n")
        snapshot = reader.update()
        self.assertEqual(snapshot.resets, 1)
        self.assertEqual(snapshot.entries[-1].text, "new generation is longer")
        self.assertIsNone(snapshot.entries[-1].capture)

    def test_generation_check_precedes_processing_flood_remainder(self):
        reader = self.reader()
        self.append(b"old\n" * 1000)
        self.assertGreater(reader.update().unread_bytes, 0)
        self.path.write_bytes(b"new\n")
        snapshot = reader.update()
        self.assertEqual(snapshot.resets, 1)
        self.assertEqual(snapshot.entries[-1].text, "new")

    def test_missing_file_created_after_attach_is_live(self):
        self.path.unlink()
        reader = self.reader()
        self.assertIsNotNone(reader.update().error)
        self.path.write_bytes(b"new live\n")
        snapshot = reader.update()
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.entries[-1].capture, NOW.isoformat())

    def test_reading_leaves_bytes_size_and_mtime_unchanged(self):
        self.path.write_bytes(FIXTURE.read_bytes())
        before = self.path.read_bytes(), self.path.stat().st_size, self.path.stat().st_mtime_ns
        reader = self.reader()
        self.drain(reader)
        for _ in range(3):
            reader.update()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_size, self.path.stat().st_mtime_ns), before)

    def test_slow_formatter_never_blocks_concurrent_append(self):
        self.path.write_bytes(record())
        reader = self.reader()
        decoding, appended = threading.Event(), threading.Event()
        errors = []

        def writer():
            if not decoding.wait(2):
                errors.append("reader never reached decoding")
                return
            try:
                self.append(b"concurrent append\n")
            except Exception as exc:
                errors.append(str(exc))
            finally:
                appended.set()

        original = reader.formatter.decode

        def slow_decode(*args):
            decoding.set()
            self.assertTrue(appended.wait(2), "reader blocked writer")
            return original(*args)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(reader.formatter, "decode", side_effect=slow_decode):
                reader.update()
        finally:
            decoding.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.drain(reader).entries[-1].text, "concurrent append")

    def test_read_errors_are_inert_and_reader_recovers(self):
        reader = self.reader()
        with patch("ub_agents.log_reader.os.open", side_effect=PermissionError("denied\x1b[2J")):
            snapshot = reader.update()
        self.assertIn(r"\x1b", snapshot.error)
        self.append(b"recovered\n")
        snapshot = reader.update()
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.entries[-1].text, "recovered")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_substituted_fifo_is_rejected_without_waiting_for_a_writer(self):
        reader = self.reader()
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIn("not a regular file", reader.update().error)


if __name__ == "__main__":
    unittest.main()
