"""Synthetic oversized Claude JSONL, including image and UTF-8 read boundaries."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_log_reader import NOW, record, result, tool
from ub_agents.log_format import ClaudeFormatter, MAX_RECORD, MAX_TEXT, MAX_TOOLS, SHORTENED
from ub_agents.log_reader import LogReader, MAX_ENTRIES, READ_BUDGET, RECORD_BUDGET
from ub_agents.view_logs import PAGE_BYTES, ViewReader


def encoded(data):
    return json.dumps(data, ensure_ascii=False).encode() + b'\n'


def image_result():
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                       'data': 'QUJD' * MAX_RECORD}}
    return {'type': 'user', 'message': {'role': 'user', 'content': [
        result(content=[image])]}, 'note': 'café', 'timestamp': NOW.isoformat()}


class OversizedClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'process.log'
        self.path.touch()

    def append(self, data):
        with self.path.open('ab') as stream:
            stream.write(data)

    def drain(self, reader):
        for _ in range(2000):
            snapshot = reader.update()
            self.assertLessEqual(snapshot.bytes_read, READ_BUDGET)
            self.assertLessEqual(snapshot.records_processed, RECORD_BUDGET)
            self.assertLessEqual(snapshot.pending_bytes, MAX_RECORD)
            self.assertLessEqual(len(snapshot.entries), MAX_ENTRIES)
            self.assertLessEqual(len(reader.formatter.tools), MAX_TOOLS)
            parser = reader.record_json
            if parser is not None:
                self.assertLessEqual(len(parser.buffer), MAX_RECORD)
                self.assertLessEqual(len(parser.head), MAX_TEXT)
                self.assertLessEqual(len(parser.tail), MAX_TEXT)
                self.assertLessEqual(len(parser.decoder.getstate()[0]), 3)
            for value in snapshot.entries:
                self.assertLessEqual(len(value.display()), MAX_TEXT)
                self.assertLessEqual(len(value.display(raw=True)), MAX_TEXT)
            if not snapshot.unread_bytes:
                return snapshot
        self.fail('Reader did not drain bounded fixture')

    def stream_unfinished(self, reader, raw):
        # Both file updates and the bounded reader split inside the base64;
        # the second file update ends inside a multi-byte UTF-8 character.
        base64 = raw.index(b'QUJD') + MAX_RECORD + 3
        utf8 = raw.index('café'.encode()) + len(b'caf') + 1
        self.assertLess(base64, utf8)
        start = 0
        for end in (base64, utf8, len(raw) - 1):
            self.append(raw[start:end])
            snapshot = self.drain(reader)
            self.assertIsNotNone(snapshot.unfinished)
            self.assertIn('incomplete Claude record omitted', snapshot.unfinished.text)
            self.assertEqual(snapshot.unfinished.styles[-1][2], 'dim')
            self.assertFalse(any('QUJD' in value.text for value in snapshot.entries))
            start = end
        return snapshot

    def assert_raw_unchanged(self, reader, snapshot):
        # Replay from byte zero with the unchanged plain-runtime projection.
        plain = LogReader(self.path, 'command', lambda: NOW, attach=False)
        plain.file_id, plain.size = reader.file_id, reader.size
        original = self.drain(plain)
        fragments = [value for value in snapshot.entries if value.raw_kind or 'oversized raw' in value.kind]
        old = [value for value in original.entries if 'oversized raw' in value.kind]
        self.assertEqual([value.display(raw=True) for value in fragments],
                         [value.display(raw=True) for value in old])
        self.assertEqual([value.raw for value in snapshot.entries],
                         [value.raw for value in original.entries])
        self.assertEqual([(ref.start, ref.end) for ref in snapshot.refs],
                         [(ref.start, ref.end) for ref in original.refs])

    def test_successful_image_is_hidden_until_and_after_completion_and_preserves_pairing(self):
        reader = ViewReader(self.path, 'claude', lambda: NOW)
        call = record(content=[tool(name='Read')])
        self.append(call)
        self.drain(reader)
        raw = encoded(image_result())
        unfinished = self.stream_unfinished(reader, raw)
        self.assertEqual(sum(bool(value.text) for value in unfinished.entries), 1)
        self.assertEqual(sum('incomplete Claude' in ref.value.text for ref in reader.page().refs), 1)
        follower = record('user', [result(is_error=True, content='following error')])
        self.append(b'\n' + follower)
        snapshot = self.drain(reader)
        projected = [value for value in snapshot.entries if value.kind == 'tool result']
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].display(), '')
        self.assertEqual(snapshot.entries[-1].kind, 'tool ERROR')
        self.assertIn('✗ following error', snapshot.entries[-1].text)
        self.assertNotIn('Read:', snapshot.entries[-1].text)
        self.assertIsNone(snapshot.unfinished)
        self.assertIsNone(reader.record_json)
        self.assertFalse(any('omitted' in value.text or 'QUJD' in value.text for value in snapshot.entries))
        self.assertEqual(self.path.read_bytes(), call + raw + follower)
        self.assert_raw_unchanged(reader, snapshot)

    def test_error_result_keeps_tool_name_first_line_and_red_style(self):
        reader = LogReader(self.path, 'claude', lambda: NOW)
        prefix = record(content=[tool(name='Read')]) + record()
        self.append(prefix)
        self.drain(reader)
        data = image_result()
        data['message']['content'][0] = result(is_error=True, content='first error line\n' + 'x' * MAX_RECORD)
        # Unused metadata still carries a large base64 string.
        data['payload'] = 'QUJD' * MAX_RECORD
        # Put the UTF-8 note after the payload for the incremental split.
        data.pop('note')
        data['note'] = 'café'
        raw = encoded(data)
        self.stream_unfinished(reader, raw)
        follower = record()
        self.append(b'\n' + follower)
        snapshot = self.drain(reader)
        error = next(value for value in snapshot.entries if value.kind == 'tool ERROR')
        self.assertIn('✗ Read: first error line', error.text)
        self.assertNotIn('xxx', error.text)
        self.assertEqual(error.styles[-1][2], 'error')
        self.assertEqual(snapshot.entries[-1].kind, 'assistant')
        self.assertEqual(self.path.read_bytes(), prefix + raw + follower)
        self.assert_raw_unchanged(reader, snapshot)

    def test_malformed_truncated_deep_and_over_budget_json_have_one_diagnostic(self):
        base = encoded(image_result())
        cases = [base[:-2] + b',}\n', base[:-2] + b'\n',
                 base[:-2] + b',"nested":' + b'[' * 65 + b'0' + b']' * 65 + b'}\n',
                 base[:-2] + b',"many":[' + b'0,' * MAX_RECORD + b'0]}\n',
                 base[:-2] + b',"type":"assistant"}\n']
        for raw in cases:
            with self.subTest(suffix=raw[-80:]):
                self.path.write_bytes(b'')
                reader = LogReader(self.path, 'claude', lambda: NOW)
                self.stream_unfinished(reader, raw)
                follower = record()
                self.append(b'\n' + follower)
                snapshot = self.drain(reader)
                visible = [value for value in snapshot.entries if value.text]
                self.assertEqual(len(visible), 2)
                self.assertIn('oversized Claude record omitted', visible[0].text)
                self.assertIn('full record in Raw', visible[0].text)
                self.assertEqual(visible[0].styles[-1][2], 'dim')
                self.assertEqual(visible[1].kind, 'assistant')
                self.assertIsNone(snapshot.unfinished)
                self.assertEqual(self.path.read_bytes(), raw + follower)
                self.assert_raw_unchanged(reader, snapshot)

    def test_other_valid_shapes_use_normal_projection_rules(self):
        base = image_result()
        # Padding is unused; only the normal Claude projection selects prose.
        padding = base['message']['content'][0]['content'][0]['source']['data']
        cases = [json.loads(record(content=[{'type': 'text', 'text': 'hello'}])),
                 json.loads(record(content=[tool()])),
                 json.loads(record(content=[{'type': 'future-block', 'data': 'opaque'},
                                            {'type': 'text', 'text': 'following block'}])),
                 {'type': 'future', 'subtype': 'new'}, {'type': 'assistant', 'message': []},
                 {'type': 'system', 'subtype': 'init'}]
        for data in cases:
            with self.subTest(data_type=data['type']):
                self.path.write_bytes(b'')
                expected = ClaudeFormatter().decode(encoded(data), NOW.isoformat())
                data = deepcopy(data)
                data['payload'], data['note'] = padding, 'café'
                reader = LogReader(self.path, 'claude', lambda: NOW)
                self.stream_unfinished(reader, encoded(data))
                self.append(b'\n' + record())
                snapshot = self.drain(reader)
                projected = snapshot.entries[-2]
                self.assertEqual(projected.kind, expected.kind)
                self.assertEqual(projected.text, expected.text)
                self.assertEqual(projected.styles, expected.styles)
                self.assertEqual(snapshot.entries[-1].kind, 'assistant')

    def test_assistant_text_utf8_fragment_boundary_and_long_error_are_shortened(self):
        data = json.loads(record(content=[{'type': 'text', 'text': ''}]))
        prefix = encoded(data).index(b'"text": "') + len(b'"text": "')
        text = 'HEAD' + 'x' * (MAX_RECORD - prefix - 5) + '€café' + 'x' * MAX_RECORD + 'END'
        for data in (json.loads(record(content=[{'type': 'text', 'text': text}])),
                     json.loads(record('user', [result(is_error=True, content=text)]))):
            projected = ClaudeFormatter().decode(encoded(data))
            self.assertTrue(projected.compact)
            self.assertLessEqual(len(projected.text), MAX_TEXT)
            self.assertIn('HEAD', projected.text)
            if projected.kind == 'assistant':
                self.assertIn(SHORTENED, projected.text)
                self.assertTrue(projected.text.endswith('END'))
            else:
                self.assertTrue(projected.text.endswith('…'))
                self.assertEqual(projected.styles[-1][2], 'error')
            self.path.write_bytes(b'')
            reader = LogReader(self.path, 'claude', lambda: NOW)
            self.append(encoded(data) + record())
            snapshot = self.drain(reader)
            self.assertNotIn('\ufffd', ''.join(value.raw for value in snapshot.entries))
            self.assertEqual(snapshot.entries[-1].kind, 'assistant')
            self.assert_raw_unchanged(reader, snapshot)

    def test_tail_and_history_fragments_keep_earlier_output_skipped(self):
        raw = encoded(image_result())
        self.path.write_bytes(raw + record())
        reader = ViewReader(self.path, 'claude')
        snapshot = self.drain(reader)
        self.assertIn('earlier output skipped', snapshot.entries[0].text)
        page = reader.older(PAGE_BYTES * 3, reader.resets)
        self.assertEqual(len(page.refs), 1)
        self.assertIn('earlier output skipped', page.refs[0].value.text)
        self.assertIn('QUJD', page.refs[0].value.display(raw=True))
        self.assertLessEqual(page.end - page.start, PAGE_BYTES)

    def test_history_pages_to_byte_zero_omit_image_json_and_preserve_raw(self):
        call = record(content=[tool(name='Read')])
        data = call + encoded(image_result()) + record() * 20
        self.path.write_bytes(data)
        reader = ViewReader(self.path, 'claude')
        plain = ViewReader(self.path, 'command')
        self.drain(reader)
        self.drain(plain)
        page = reader.page()
        pages = 0
        while page.start:
            end = page.start
            page = reader.older(end, reader.resets)
            original = plain.older(end, plain.resets)
            self.assertLess(page.start, end)
            self.assertEqual(page.end, end)
            self.assertLessEqual(page.end - page.start, PAGE_BYTES)
            self.assertLessEqual(len(page.refs), MAX_ENTRIES + 1)
            self.assertEqual([(ref.start, ref.end) for ref in page.refs],
                             [(ref.start, ref.end) for ref in original.refs])
            self.assertEqual([ref.value.raw for ref in page.refs],
                             [ref.value.raw for ref in original.refs])
            for ref, old in zip(page.refs, original.refs):
                if ref.value.kind.startswith('partial '):
                    self.assertEqual(ref.value.display(raw=True), old.value.display(raw=True))
                self.assertNotIn('QUJD', ref.value.display())
                self.assertNotIn('{"type"', ref.value.display())
            pages += 1
        self.assertGreater(pages, 1)
        self.assertEqual(page.refs[0].value.kind, 'tool call')
        fragment = page.refs[-1]
        self.assertEqual(fragment.start, len(call))
        self.assertEqual(fragment.end, page.end)
        self.assertIn('incomplete Claude record omitted', fragment.value.display())
        self.assertEqual(fragment.value.styles[-1][2], 'dim')
        self.assertIn('partial raw record (page end)', fragment.value.display(raw=True))
        self.assertIn('QUJD', fragment.value.display(raw=True))
        self.assertEqual(self.path.read_bytes(), data)

    def test_oversized_write_and_edit_keep_full_line_counts_and_pair_following_results(self):
        for name in ('Write', 'Edit'):
            for trailing_newline in (False, True):
                with self.subTest(name=name, trailing_newline=trailing_newline):
                    text = 'line\n' * MAX_RECORD + ('' if trailing_newline else 'end')
                    inputs = {'file_path': 'synthetic.py', 'content': text} if name == 'Write' else {
                        'file_path': 'synthetic.py', 'new_string': text, 'old_string': text}
                    raw = record(content=[{**tool(name=name), 'input': inputs}])
                    self.path.write_bytes(b'')
                    reader = LogReader(self.path, 'claude', lambda: NOW)
                    self.append(raw + record('user', [result(is_error=True, content='following error')]))
                    snapshot = self.drain(reader)
                    call = next(value for value in snapshot.entries if value.kind == 'tool call')
                    count = MAX_RECORD + (not trailing_newline)
                    self.assertIn(f'+{count}', call.text)
                    if name == 'Edit':
                        self.assertIn(f'-{count}', call.text)
                    self.assertIn('✗ following error', snapshot.entries[-1].text)
                    self.assertNotIn(name + ':', snapshot.entries[-1].text)

    def test_generation_change_discards_oversized_parser_and_pairing(self):
        reader = LogReader(self.path, 'claude', lambda: NOW)
        self.append(record(content=[tool(name='Read')]) + encoded(image_result())[:-1])
        self.drain(reader)
        self.assertIsNotNone(reader.record_json)
        self.path.write_bytes(record('user', [result(is_error=True, content='new error')]))
        snapshot = self.drain(reader)
        self.assertEqual(snapshot.resets, 1)
        self.assertIsNone(reader.record_json)
        self.assertIsNone(snapshot.unfinished)
        self.assertIn('✗ tool: new error', snapshot.entries[-1].text)
