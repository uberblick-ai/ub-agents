"""Compact Claude projections: recorded inputs plus labelled synthetic cases."""

import json
import os
import time
import unittest
from unittest.mock import patch

from tests.test_log_reader import FIXTURE, record, result, tool
from ub_agents.log_format import ClaudeFormatter, MAX_TEXT, SHORTENED


class CompactClaudeTests(unittest.TestCase):
    def setUp(self):
        self.formatter = ClaudeFormatter()

    def decode(self, value, capture=None):
        if not isinstance(value, bytes):
            value = json.dumps(value, ensure_ascii=False).encode()
        return self.formatter.decode(value, capture)

    def test_recorded_thinking_hidden_records_calls_error_and_final_result(self):
        data = [json.loads(line) for line in FIXTURE.read_bytes().splitlines()]
        entries = [self.decode(line) for line in FIXTURE.read_bytes().splitlines()]
        subtypes = {value.get('subtype') for value in data if value['type'] == 'system'}
        self.assertTrue({'init', 'thinking_tokens', 'task_started', 'task_notification'} <= subtypes)
        for value, entry in zip(data, entries):
            self.assertTrue(entry.compact)
            if value['type'] in ('system', 'rate_limit_event') or entry.kind == 'tool result':
                self.assertEqual(entry.display(), '')
                self.assertIn(value['type'], entry.display(raw=True))
            if value.get('subtype') in ('task_started', 'task_notification'):
                self.assertIn('synthetic', value['fixture_source'])
        output = '\n'.join(entry.display() for entry in entries)
        self.assertIn('· thinking', output)
        self.assertIn('▸ Read <fixture>/fixture_output.py', output)
        self.assertIn('▸ Bash cat missing-owned.txt', output)
        error = next(entry for entry in entries if entry.kind == 'tool ERROR')
        self.assertTrue(error.text.endswith('  ✗ Exit code 1'))
        self.assertEqual(error.styles[-1][2], 'red')
        self.assertNotIn('cat:', error.text)
        self.assertEqual(entries[-1].text, '--:--:--  ✓ run finished')
        self.assertNotIn('assistant:', output)
        self.assertNotIn('id=<', output)
        self.assertNotIn('producer=', output)

    def test_producer_time_local_capture_marked_and_placeholder_aligned(self):
        # Synthetic timezone exercises conversion rather than assuming host UTC.
        prior = os.environ.get('TZ')
        try:
            with patch.dict(os.environ, {'TZ': 'EST5'}):
                time.tzset()
                producer = self.decode(record(timestamp='2026-10-03T14:00:00+02:00'))
                captured = self.decode(record(timestamp='bad'), '2026-10-03T12:00:00+00:00')
                absent = self.decode(record())
                self.assertEqual(producer.text, '07:00:00  hello café')
                self.assertEqual(captured.text, '~07:00:00 hello café')
                self.assertEqual(absent.text, '--:--:--  hello café')
                self.assertEqual(self.decode(record(), 'bad').text, absent.text)
        finally:
            if prior is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = prior
            time.tzset()

    def test_synthetic_tools_choose_main_argument_without_ids_or_json(self):
        cases = [('Read', {'file_path': 'src/a.py', 'offset': 2}, 'src/a.py'),
                 ('Edit', {'file_path': 'src/a.py'}, 'src/a.py'),
                 ('Write', {'file_path': 'src/a.py'}, 'src/a.py'),
                 ('Bash', {'description': 'ignored', 'command': 'python -m unittest'}, 'python -m unittest'),
                 ('Grep', {'path': 'ignored', 'pattern': 'needle'}, 'needle'),
                 ('Glob', {'pattern': '*.py'}, '*.py'),
                 ('FutureTool', {'count': 1, 'query': 'first', 'other': 'second'}, 'first'),
                 ('FutureTool', {'count': 1}, '')]
        for name, inputs, argument in cases:
            with self.subTest(name=name, inputs=inputs):
                block = {**tool(name=name), 'input': inputs}
                entry = self.decode(record(content=[block]))
                self.assertEqual(entry.text, '--:--:--  ▸ ' + name + (' ' + argument if argument else ''))
                self.assertNotIn('id=', entry.text)
                self.assertNotIn('{', entry.text)

    def test_synthetic_edit_and_write_counts_and_colors(self):
        for name, inputs, suffix, colors in (
                ('Edit', {'new_string': 'a\nb\n', 'old_string': 'old'}, '+2 -1', ('green', 'red')),
                ('Edit', {'new_string': '', 'old_string': '\n'}, '+0 -1', ('green', 'red')),
                ('Write', {'content': 'a\nb'}, '+2', ('green',)),
                ('Write', {'content': 'a\r\nb\r\n'}, '+2', ('green',)),
                ('Write', {'content': 'a\rb\vc\fd\x1ce\x1df\x1eg\x85h\u2028i\u2029'}, '+1', ('green',)),
                ('Write', {}, '', ()), ('Edit', {}, '', ())):
            entry = self.decode(record(content=[{**tool(name=name), 'input': {'file_path': 'a', **inputs}}]))
            self.assertEqual(entry.text, '--:--:--  ▸ ' + name + ' a' + (' ' + suffix if suffix else ''))
            self.assertEqual(tuple(style for _, _, style in entry.styles), colors)
            for start, end, color in entry.styles:
                self.assertTrue(entry.text[start:end].startswith('+' if color == 'green' else '-'))

    def test_synthetic_multiline_long_and_control_arguments_are_one_line(self):
        for command in ('first\nsecond', 'x' * 500, '\x1b[2J\r\t\x9b31m\nsecond'):
            entry = self.decode(record(content=[{**tool(), 'input': {'command': command}}]))
            self.assertNotIn('\n', entry.text)
            self.assertTrue(entry.text.endswith('…'))
            self.assertNotIn('second', entry.text)
            self.assertLessEqual(len(entry.text), 180)
            self.assertNotIn('\x1b', entry.text)
            self.assertNotIn('\r', entry.text)

    def test_synthetic_assistant_newlines_thinking_and_unknown_blocks_survive(self):
        controls = ''.join(chr(i) for i in (*range(32), *range(127, 160)) if i != 10)
        entry = self.decode(record(content=[{'type': 'text', 'text': 'one\n\nthree' + controls},
                                           {'type': 'thinking', 'thinking': 'private'},
                                           {'type': 'future_block', 'payload': 'private'},
                                           {'type': 'text', 'text': 'last'}]))
        lines = entry.text.split('\n')
        self.assertEqual(lines[0], '--:--:--  one')
        self.assertEqual(lines[1], ' ' * 10)
        self.assertTrue(lines[2].startswith(' ' * 10 + 'three'))
        self.assertEqual(lines[3:], ['--:--:--  · thinking', '--:--:--  · future_block', '--:--:--  last'])
        self.assertNotIn('private', entry.text)
        self.assertNotIn('assistant:', entry.text)
        self.assertFalse(any(ord(char) < 32 and char != '\n' or 127 <= ord(char) <= 159 for char in entry.text))
        self.assertIn(r'\x1b', entry.text)
        self.assertIn(r'\x9b', entry.text)

    def test_synthetic_failed_results_name_tool_only_when_needed(self):
        self.decode(record(content=[tool()]))
        # Hidden records and successful results add no intervening line.
        self.decode({'type': 'system', 'subtype': 'task_notification'})
        self.assertEqual(self.decode(record('user', [result()])).text, '')
        direct = self.decode(record('user', [result(is_error=True, content='Exit code 1\nsecond')]))
        self.assertEqual(direct.text, '--:--:--    ✗ Exit code 1')
        self.decode(record(content=[tool()]))
        self.decode(record())
        delayed = self.decode(record('user', [result(is_error=True, content='failed\nsecond')]))
        self.assertEqual(delayed.text, '--:--:--    ✗ Bash: failed')
        self.formatter.reset()
        unpaired = self.decode(record('user', [result(is_error=True)]))
        self.assertIn('✗ tool: hello', unpaired.text)

    def test_synthetic_runtime_results_errors_and_unrecognized_record_labels(self):
        failure = self.decode({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                               'result': 'last assistant message', 'errors': ['API failed\nstack', 'another error']})
        self.assertEqual(failure.text, '--:--:--  ✗ error_during_execution: API failed')
        self.assertEqual(failure.styles[-1][2], 'red')
        for value in ('API failed\nstack', {'message': 'API failed\nstack'}):
            runtime = self.decode({'type': 'error', 'error': value})
            self.assertEqual(runtime.text, '--:--:--  ✗ API failed')
            self.assertEqual(runtime.styles[-1][2], 'red')
        unknown = self.decode({'type': 'future', 'subtype': 'phase', 'payload': 'private'})
        self.assertEqual(unknown.text, '--:--:--  · future · phase')
        self.assertEqual(unknown.styles[-1][2], 'dim')

    def test_synthetic_bounds_include_time_indent_and_retained_styles(self):
        entry = self.decode(record(content=[{'type': 'text', 'text': ('long\x1b\n' * 2000) + 'END'}],
                                   timestamp='2026-10-03T12:00:00Z'))
        for output in (entry.display(), entry.display(raw=True)):
            self.assertLessEqual(len(output), MAX_TEXT)
            self.assertIn(SHORTENED, output)
        self.assertTrue(entry.text.endswith('END'))
        for start, end, _ in entry.styles:
            self.assertTrue(0 <= start < end <= len(entry.text))
