"""Codex 0.160.0 owned recordings; mutated boundary/forward-format cases are synthetic."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ub_agents.log_format import ClaudeFormatter, CodexFormatter, MAX_RECORD, MAX_TEXT, MAX_TOOLS, SHORTENED
from ub_agents.log_reader import LogReader, MAX_ENTRIES, READ_BUDGET, RECORD_BUDGET
from ub_agents.view_logs import FileChanged, PAGE_BYTES, ViewReader

FIXTURES = Path(__file__).parent / 'fixtures/runtime_logs'
CODEX = FIXTURES / 'codex.log'
ERROR = FIXTURES / 'codex-error.log'
TOOLS = FIXTURES / 'codex-tools.log'
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def records(path=CODEX):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode() + b'\n'


def recorded_item(kind, *, stage='item.completed', path=CODEX, status=None):
    return next(value for value in records(path) if value['type'] == stage
                and value.get('item', {}).get('type') == kind
                and (status is None or value['item'].get('status') == status))


class CodexFormattingTests(unittest.TestCase):
    def setUp(self):
        self.formatter = CodexFormatter()

    def decode(self, value, capture=None):
        return self.formatter.decode(value if isinstance(value, bytes) else encoded(value), capture)

    def test_complete_owned_recordings_render_each_observed_kind_and_keep_raw(self):
        entries = []
        for path in (CODEX, TOOLS, ERROR):
            self.formatter.reset()
            for raw in path.read_bytes().splitlines():
                value, projected = json.loads(raw), self.decode(raw)
                self.assertTrue(projected.compact)
                self.assertIsNone(projected.event)  # This producer emits no timestamps.
                self.assertIn(value['type'], projected.display(raw=True))
                entries.append(projected)
        output = '\n'.join(value.text for value in entries)
        self.assertIn('▸ Bash /bin/zsh', output)
        self.assertIn('✗ Exit code 7: owned failure', output)
        self.assertIn('▸ file add <fixture>/greeting.txt', output)
        self.assertIn('▸ fixture.echo', output)
        self.assertIn('✗ owned tool failure', output)
        self.assertIn('✗ MCP tool call requires approval', output)
        self.assertIn('✗ Model metadata for', output)
        self.assertIn('✓ run finished', output)
        self.assertIn('Recording complete.\n          All failures were deliberate.', output)
        self.assertNotIn('thread.started', output)
        self.assertNotIn('turn.started', output)
        self.assertNotIn('owned tool success', output)  # Successful output remains raw.
        self.assertTrue(all(value.styles[-1][2] == 'error' for value in entries if value.kind == 'runtime ERROR'))
        self.assertTrue(all(value.styles[-1][2] == 'error' for value in entries if value.kind == 'tool ERROR'))
        assistant = next(value for value in entries if value.kind == 'assistant')
        self.assertTrue(all(style == 'dim italic' for _, _, style in assistant.styles))

    def test_call_result_pairing_updates_and_unpaired_completions(self):
        for kind, path in (('command_execution', CODEX), ('mcp_tool_call', TOOLS), ('file_change', CODEX)):
            with self.subTest(kind=kind):
                self.formatter.reset()
                start = recorded_item(kind, stage='item.started', path=path)
                completed = recorded_item(kind, path=path, status='completed')
                self.assertIn('▸ ', self.decode(start).text)
                # Synthetic progress update, with the same observed item shape.
                self.assertEqual(self.decode({**start, 'type': 'item.updated'}).text, '')
                result = self.decode(completed)
                self.assertEqual(result.kind, 'tool result')
                self.assertEqual(result.text, '')
                self.formatter.reset()
                self.assertIn('▸ ', self.decode(completed).text)
        failed = recorded_item('command_execution', status='failed')
        self.formatter.reset()
        unpaired = self.decode(failed)
        self.assertIn('▸ Bash ', unpaired.text)
        self.assertIn('\n            ✗ Exit code 7', unpaired.text)
        self.formatter.reset()
        start = {**failed, 'type': 'item.started', 'item': {**failed['item'], 'status': 'in_progress'}}
        self.decode(start)
        self.decode(recorded_item('agent_message'))
        self.assertIn('✗ Bash: Exit code 7', self.decode(failed).text)

    def test_synthetic_changed_activity_updates_and_failure_fields(self):
        value = recorded_item('command_execution', stage='item.started')
        self.decode(value)
        changed = deepcopy(value)
        changed['type'] = 'item.updated'
        changed['item']['command'] = 'changed command'
        self.assertIn('▸ Bash changed command', self.decode(changed).text)
        self.assertEqual(self.decode(changed).text, '')
        for kind in ('file_change', 'command_execution', 'mcp_tool_call'):
            self.formatter.reset()
            value = recorded_item(kind)
            value['item']['status'] = 'failed'
            if kind == 'command_execution':
                value['item']['exit_code'] = None
                value['item']['aggregated_output'] = ''
            projected = self.decode(value)
            self.assertEqual(projected.kind, 'tool ERROR')
            self.assertIn('✗ ', projected.text)
            self.assertEqual(projected.styles[-1][2], 'error')
        value = recorded_item('file_change', stage='item.started')
        self.decode(value)
        value['type'] = 'item.updated'
        value['item']['changes'].append({'kind': 'update', 'path': 'changed.txt'})
        self.assertIn('▸ file update changed.txt', self.decode(value).text)

    def test_unknown_complete_records_and_changed_shapes_have_dim_labels(self):
        cases = [({'type': 'future', 'payload': 'private'}, '· future'),
                 ({'type': 'item.completed', 'item': {'id': 'new', 'type': 'reasoning', 'text': 'private'}},
                  '· item.completed · reasoning'),
                 ({'type': 'item.completed', 'item': []}, '· item.completed'),
                 ({'type': 'error', 'message': []}, '· error'),
                 ({'type': 'turn.completed', 'usage': []}, '· turn.completed'),
                 ({'type': []}, '· unknown record')]
        for value, label in cases:
            projected = self.decode(value)
            self.assertEqual(projected.text, '          ' + label)
            self.assertEqual(projected.styles[-1][2], 'dim')
            self.assertNotIn('private', projected.text)
            self.assertIn('type', projected.display(raw=True))
        for kind in ('command_execution', 'mcp_tool_call', 'file_change'):
            value = recorded_item(kind)
            for field in value['item']:
                changed = deepcopy(value)
                changed['item'][field] = ['unfamiliar']
                # Unused fields may stay raw; consumed fields must never crash.
                projected = self.decode(changed)
                self.assertTrue(projected.compact)
                self.assertLessEqual(len(projected.text), MAX_TEXT)

    def test_malformed_non_json_diagnostics_and_oversized_records_remain_raw(self):
        for raw in (b'warning: owned diagnostic\x1b[2J', b'{"type":', b'[]', b'null', b'\xff',
                    b'{"type":"turn.completed","usage":NaN}', b'[' * 2000 + b']' * 2000):
            projected = self.decode(raw)
            self.assertFalse(projected.compact)
            self.assertEqual(projected.kind, 'raw text')
            self.assertNotIn('\x1b', projected.text)
        big = recorded_item('agent_message')
        big['item']['text'] = 'x' * MAX_RECORD
        self.assertIn('oversized raw', self.decode(big).kind)

    def test_only_aware_top_level_timestamps_supply_producer_time(self):
        value = recorded_item('agent_message')
        prior = os.environ.get('TZ')
        try:
            with patch.dict(os.environ, {'TZ': 'EST5'}):
                time.tzset()
                self.assertTrue(self.decode({**value, 'timestamp': '2026-10-04T14:00:00+02:00'}).text.startswith(' 07:00:00 '))
                for invalid in ('2026-10-04T14:00:00', 'bad', 123, None, 'x' * 100):
                    projected = self.decode({**value, 'timestamp': invalid}, NOW.isoformat())
                    self.assertIsNone(projected.event)
                    self.assertTrue(projected.text.startswith('~07:00:00 '))
                # No time is extracted from message text or unrelated item fields.
                value['item']['timestamp'] = '2026-10-04T14:00:00+02:00'
                self.assertTrue(self.decode(value).text.startswith('          '))
        finally:
            if prior is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = prior
            time.tzset()

    def test_progress_folding_is_runtime_specific(self):
        # Synthetic Claude-shaped progress is not validated Codex activity.
        from tests.test_log_reader import progress
        raw = progress(45, tool_id='tool-1')
        projected = self.decode(raw)
        self.assertEqual(projected.text, '          · tool_progress')
        self.assertEqual(projected.progress, ())
        self.assertIn('elapsed_time_seconds', projected.display(raw=True))
        claude = ClaudeFormatter().decode(raw)
        self.assertEqual(claude.text, '')
        self.assertEqual(claude.progress, ('tool-1', '45s'))

    def test_controls_length_limits_and_style_ranges_match_claude_projections(self):
        controls = ''.join(chr(n) for n in (*range(32), *range(127, 160)) if n != 10) + '\ud800'
        value = recorded_item('agent_message')
        value['item']['text'] = 'first\n' + controls + '\n' + ('long\x1b\n' * 2000) + 'END'
        projected = self.decode(json.dumps(value).encode())
        self.assertIn('first\n          ', projected.text)
        self.assertIn(r'\x1b', projected.text)
        self.assertIn(r'\ud800', projected.text)
        self.assertTrue(projected.text.endswith('END'))
        for output in (projected.text, projected.display(raw=True)):
            self.assertLessEqual(len(output), MAX_TEXT)
            self.assertIn(SHORTENED, output)
            self.assertFalse(any(ord(c) < 32 and c != '\n' or 127 <= ord(c) <= 159 for c in output))
        for start, end, _ in projected.styles:
            self.assertTrue(0 <= start < end <= len(projected.text))
        call = recorded_item('command_execution', stage='item.started')
        call['item']['command'] = '\x1b[2J\r\t' + 'x' * 500 + '\nsecond'
        text = self.decode(call).text
        self.assertNotIn('\n', text)
        self.assertTrue(text.endswith('…'))
        self.assertNotIn('second', text)

    def test_bounded_pairing_cache_and_invalid_record_does_not_change_it(self):
        start = recorded_item('command_execution', stage='item.started')
        for n in range(MAX_TOOLS + 10):
            value = deepcopy(start)
            value['item']['id'] = str(n)
            self.decode(value)
        self.assertEqual(len(self.formatter.tools), MAX_TOOLS)
        previous = dict(self.formatter.tools)
        bad = deepcopy(start)
        bad['item']['command'] = None
        self.assertEqual(self.decode(bad).kind, 'other')
        self.assertEqual(dict(self.formatter.tools), previous)
        self.formatter.reset()
        self.assertEqual(len(self.formatter.tools), 0)


class CodexReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'process.log'
        self.path.touch()

    def append(self, raw):
        with self.path.open('ab') as stream:
            stream.write(raw)

    def drain(self, reader):
        for _ in range(1000):
            snapshot = reader.update()
            self.assertLessEqual(snapshot.bytes_read, READ_BUDGET)
            self.assertLessEqual(snapshot.records_processed, RECORD_BUDGET)
            self.assertLessEqual(snapshot.pending_bytes, MAX_RECORD)
            self.assertLessEqual(len(snapshot.entries), MAX_ENTRIES)
            if not snapshot.unread_bytes:
                return snapshot
        self.fail('Recording did not drain within bounded reads')

    def test_recordings_incremental_capture_times_and_mixed_diagnostics(self):
        reader = LogReader(self.path, 'codex', lambda: NOW)
        data = CODEX.read_bytes() + b'owned diagnostic\x1b[2J\n' + ERROR.read_bytes() + TOOLS.read_bytes()
        for index in range(0, len(data), 17):
            self.append(data[index:index + 17])
            self.drain(reader)
        snapshot = reader.snapshot()
        self.assertEqual(len(snapshot.entries), len(data.splitlines()))
        self.assertTrue(all(value.capture == NOW.isoformat() for value in snapshot.entries))
        self.assertEqual(sum(value.kind == 'raw text' for value in snapshot.entries), 1)
        self.assertEqual(snapshot.pending_bytes, 0)
        self.assertEqual(self.path.read_bytes(), data)

    def test_unfinished_json_and_utf8_split_at_every_byte(self):
        value = recorded_item('agent_message')
        value['item']['text'] = 'hello café'
        reader = LogReader(self.path, 'codex', lambda: NOW)
        for byte in encoded(value)[:-1]:
            self.append(bytes([byte]))
            preview = reader.update()
            self.assertFalse(preview.entries)
            self.assertEqual(preview.unfinished.kind, 'unfinished raw record')
        self.append(b'\n')
        result = reader.update().entries[-1]
        self.assertEqual(result.kind, 'assistant')
        self.assertIn('café', result.text)
        self.assertNotIn('\ufffd', result.text)

    def test_large_split_records_stay_raw_and_next_record_recovers(self):
        value = recorded_item('agent_message')
        value['item']['text'] = 'x' * (MAX_RECORD * 3)
        reader = LogReader(self.path, 'codex')
        self.append(encoded(value) + CODEX.read_bytes())
        snapshot = self.drain(reader)
        fragments = [value for value in snapshot.entries if not value.compact]
        self.assertEqual(len(fragments), 4)
        self.assertTrue(all('oversized raw' in value.kind for value in fragments))
        self.assertEqual(snapshot.entries[-1].kind, 'runtime result')
        self.assertTrue(all(len(value.display()) <= MAX_TEXT for value in snapshot.entries))

    def test_near_tail_and_history_preserve_raw_boundaries_and_complete_records(self):
        value = recorded_item('agent_message')
        value['item']['text'] = 'x' * (PAGE_BYTES * 3)
        self.path.write_bytes(CODEX.read_bytes() * 20 + encoded(value) + TOOLS.read_bytes())
        reader = ViewReader(self.path, 'codex')
        snapshot = self.drain(reader)
        self.assertGreater(snapshot.skipped_bytes, 0)
        page = reader.page()
        self.assertIn('partial first raw record', page.refs[0].value.kind)
        self.assertEqual(page.refs[-1].value.kind, 'runtime result')
        self.assertTrue(all(ref.value.capture is None for ref in page.refs))
        fragments = 0
        while page.start:
            previous = page.start
            page = reader.older(previous, reader.resets)
            self.assertLess(page.start, previous)
            self.assertEqual(page.end, previous)
            self.assertLessEqual(previous - page.start, PAGE_BYTES)
            self.assertLessEqual(len(page.refs), 201)
            for ref in page.refs:
                if 'partial raw record' in ref.value.kind:
                    fragments += 1
                    self.assertFalse(ref.value.compact)
        self.assertGreater(fragments, 0)
        self.assertEqual(page.refs[0].value.kind, 'thread.started')
        self.assertIn('▸ Bash', '\n'.join(ref.value.text for ref in page.refs))

    def test_truncation_and_replacement_reset_pending_pairing_and_generation(self):
        self.path.write_bytes(CODEX.read_bytes())
        reader = ViewReader(self.path, 'codex')
        self.drain(reader)
        first = reader.page()
        self.append(b'{"type":"item.completed"')
        reader.update()
        failed = recorded_item('command_execution', status='failed')
        self.path.write_bytes(encoded(failed))
        snapshot = self.drain(reader)
        self.assertEqual(snapshot.resets, 1)
        self.assertEqual(snapshot.pending_bytes, 0)
        self.assertEqual([value.kind for value in snapshot.entries], ['reader', 'tool ERROR'])
        self.assertIn('▸ Bash', snapshot.entries[-1].text)
        self.assertIsNone(snapshot.entries[-1].capture)
        with self.assertRaises(FileChanged):
            reader.older(first.start, first.generation)
        replacement = self.path.with_suffix('.next')
        replacement.write_bytes(TOOLS.read_bytes())
        replacement.replace(self.path)
        snapshot = self.drain(reader)
        self.assertEqual(snapshot.resets, 2)
        self.assertEqual(snapshot.entries[-1].kind, 'runtime result')
        self.assertNotIn('Bash', '\n'.join(value.text for value in snapshot.entries))
