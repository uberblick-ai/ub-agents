"""Offline replay + owned PTY/headless comparison of the unchanged #99 views."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata

from rich.console import Console
from rich.text import Text
from textual.widgets import RichLog, Static

from experiments.terminal_observer.demo import View
from experiments.terminal_observer.local import fixture_observer
from experiments.terminal_observer.logs import Tail

ROOT = Path.cwd()
EVIDENCE = ROOT / 'experiments/runtime_logs_111/evidence'
PRIVATE = ROOT / '.ub-agent/spike111-comparison'
SIZES = ((110, 32), (72, 24))
TOKENS = ('SPIKE111_MESSAGE_BEGIN', 'SPIKE111_COMMAND_BEGIN', 'SPIKE111_ROW_000',
          'SPIKE111_ROW_120', 'SPIKE111_ROW_239', 'SPIKE111_LONG_BEGIN',
          'SPIKE111_LONG_END', 'SPIKE111_STDERR', 'SPIKE111_COMMAND_END',
          'missing-owned.txt', 'SPIKE111_MESSAGE_END')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def presence(text):
    return {token: token in text for token in TOKENS}


def adapter_output(path):
    tail = Tail(path)
    seen = 0
    entries = []
    while tail.offset < path.stat().st_size or tail.buffer.ready():
        tail.poll()
        count = tail.buffer.total - seen
        entries.extend(list(tail.buffer.entries)[-count:] if count else [])
        seen = tail.buffer.total
    return tail, entries


class PrettyPTY:
    """Own only this subprocess/PTY; no operator terminal or terminal multiplexer."""
    def __init__(self, path, size, follow=False):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))
        argv = [sys.executable, '-m', 'experiments.terminal_observer.pretty', str(path)]
        if follow:
            argv.append('--follow')
        self.process = subprocess.Popen(argv, cwd=ROOT, stdin=slave, stdout=slave,
                                        stderr=slave, start_new_session=True,
                                        env=os.environ | {'TERM': 'xterm-256color'})
        os.close(slave)
        self.data = bytearray()

    def drain(self):
        while select.select([self.master], [], [], 0)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.data.extend(chunk)

    async def finish(self, interrupt=False):
        if interrupt and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 10
        try:
            while self.process.poll() is None:
                self.drain()
                if time.monotonic() > deadline:
                    raise AssertionError('owned pretty-printer did not finish')
                await asyncio.sleep(.02)
            self.drain()
            assert self.process.wait(timeout=2) == 0
            return bytes(self.data).decode('utf-8').replace('\r\n', '\n')
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            os.close(self.master)


def wrap_terminal(text, width):
    """Plain-output autowrap model, not a terminal emulator/operator observation."""
    rows = []
    for logical in text.splitlines():
        line = ''
        column = 0
        for char in logical.expandtabs(8):
            cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
            if column + cells > width:
                rows.append(line)
                line = ''
                column = 0
            line += char
            column += cells
        rows.append(line)
    return rows


def pretty_screen(text, size, name):
    rows = wrap_terminal(text, size[0])
    visible = rows[-size[1]:]
    console = Console(record=True, width=size[0], height=size[1], force_terminal=True,
                      color_system=None, file=__import__('io').StringIO())
    console.print(Text('\n'.join(visible)), soft_wrap=True, end='')
    (EVIDENCE / f'{name}-pretty.svg').write_text(console.export_svg(title='pretty.py — owned PTY output; modeled final viewport'))
    return {'logical_lines': len(text.splitlines()), 'modeled_wrapped_rows': len(rows),
            'final_viewport_tokens': presence('\n'.join(visible)),
            'viewport_label': 'replay: plain autowrap model of owned PTY transcript; no operator observation'}


async def settled(app, path, pretty=None):
    deadline = time.monotonic() + 12
    while True:
        if pretty:
            pretty.drain()
        if (app.tail and app.tail.offset == path.stat().st_size
                and not app.tail.buffer.ready() and app.seen >= app.tail.buffer.total):
            await asyncio.sleep(.15)
            return
        if time.monotonic() > deadline:
            raise AssertionError('headless replay did not drain')
        await asyncio.sleep(.02)


def viewport(widget):
    start = int(widget.scroll_y)
    return '\n'.join(line.text for line in widget.lines[start:start + widget.scrollable_content_region.height])


def ui_metrics(app):
    output = app.query_one(RichLog)
    return {'decoded_records': app.tail.buffer.total, 'retained_records': len(app.tail.buffer.entries),
            'evicted_records': app.tail.buffer.discarded, 'rendered_rows': len(output.lines),
            'log_region': [output.scrollable_content_region.width, output.scrollable_content_region.height],
            'scroll_y': float(output.scroll_y), 'max_scroll_y': float(output.max_scroll_y),
            'follow': app.follow, 'retained_tokens': presence('\n'.join(e.display() for e in app.tail.buffer.entries)),
            'rendered_tokens': presence('\n'.join(line.text for line in output.lines)),
            'viewport_tokens': presence(viewport(output)),
            'note': str(app.query_one('#log_note', Static).render())}


async def compare(runtime, size):
    source = EVIDENCE / runtime / 'process.log'
    meta = json.loads((source.parent / 'capture.json').read_text())
    data = source.read_bytes()
    assert sha(data) == meta['sanitized_sha256']
    name = f'{runtime}-{size[0]}x{size[1]}'
    tail, entries = adapter_output(source)
    expected = ''.join(e.display() + '\n' for e in entries)
    pretty = PrettyPTY(source, size)
    text = await pretty.finish()
    assert text == expected, 'pretty PTY differs from the full sequential adapter projection'
    (EVIDENCE / f'{name}-pretty.txt').write_text(text)
    finding = {'runtime': runtime, 'size': list(size), 'input_sha256': sha(data),
               'observation': 'sanitized real recording replay; owned PTY for pretty.py, Textual headless pilot for combined view',
               'pretty': {'exit_code': 0, 'equals_full_adapter_projection': True,
                          'records_printed': len(entries), 'tokens': presence(text),
                          **pretty_screen(text, size, name)}, 'complete_probe': meta['complete']}
    with tempfile.TemporaryDirectory(dir=PRIVATE) as directory:
        path = Path(directory) / 'process.log'
        path.write_bytes(data)
        app = View(fixture_observer(ROOT, path))
        started = time.monotonic()
        async with app.run_test(size=size) as pilot:
            await settled(app, path)
            finding['attachment_drain_seconds'] = round(time.monotonic() - started, 3)
            finding['combined'] = ui_metrics(app)
            app.save_screenshot(f'{name}-combined-tail.svg', path=str(EVIDENCE))
            output = app.query_one(RichLog)
            await pilot.press('pageup')
            output.scroll_home(animate=False)
            await pilot.pause(.15)
            app.save_screenshot(f'{name}-combined-oldest.svg', path=str(EVIDENCE))
            finding['oldest_available_viewport'] = presence(viewport(output))
            # Locate the retained long-output notice/command result in rendered rows.
            row = next((i for i, line in enumerate(output.lines) if 'SPIKE111_LONG_BEGIN' in line.text
                        or 'SPIKE111_COMMAND_BEGIN' in line.text), 0)
            output.scroll_to(y=row, animate=False, force=True)
            await pilot.pause(.15)
            app.save_screenshot(f'{name}-combined-command.svg', path=str(EVIDENCE))
            # Pane changes/resize rebuild from decoded records, not the old rendered rows.
            await pilot.press('2', '1')
            await settled(app, path)
            finding['after_pane_revisit'] = ui_metrics(app)
            app.save_screenshot(f'{name}-combined-revisit.svg', path=str(EVIDENCE))
            await pilot.press('q')

        # Split only existing bytes, inside a real record. No synthetic events/appends.
        path.write_bytes(b'')
        stream_pretty = PrettyPTY(path, size, follow=True)
        app = View(fixture_observer(ROOT, path))
        interrupted = False
        try:
            async with app.run_test(size=size) as pilot:
                await pilot.pause(.2)
                split = len(data) * 3 // 5
                with path.open('ab') as writer:
                    writer.write(data[:split])
                await settled(app, path, stream_pretty)
                output = app.query_one(RichLog)
                app.save_screenshot(f'{name}-combined-live.svg', path=str(EVIDENCE))
                partial = bool(app.tail.buffer.pending)
                await pilot.press('pageup')
                await pilot.pause(.15)
                before_y = float(output.scroll_y)
                before_text = viewport(output)
                before_region = [output.scrollable_content_region.width, output.scrollable_content_region.height]
                before_rendered = '\n'.join(line.text for line in output.lines)
                app.save_screenshot(f'{name}-combined-before-pause-append.svg', path=str(EVIDENCE))
                before_total = app.tail.buffer.total
                stream_pretty.drain()
                pretty_before_bytes = len(stream_pretty.data)
                for offset in range(split, len(data), 1024):
                    with path.open('ab') as writer:
                        writer.write(data[offset:offset + 1024])
                    stream_pretty.drain()
                    await asyncio.sleep(.02)
                await settled(app, path, stream_pretty)
                await pilot.pause(.3)
                stream_pretty.drain()
                after_text = viewport(output)
                (EVIDENCE / f'{name}-paused-before.txt').write_text(before_text + '\n')
                (EVIDENCE / f'{name}-paused-after.txt').write_text(after_text + '\n')
                before_rows = before_text.splitlines()
                after_rows = after_text.splitlines()
                paused = {'partial_record_visible_before_pause': partial,
                          'pause_active': not app.follow,
                          'scroll_position_before': before_y, 'scroll_position_after': float(output.scroll_y),
                          'same_viewport_text_after_append': before_text == after_text,
                          'same_top_visible_row': bool(before_rows and after_rows and before_rows[0] == after_rows[0]),
                          'same_shared_visible_rows': before_rows[:min(len(before_rows), len(after_rows))] == after_rows[:min(len(before_rows), len(after_rows))],
                          'visible_region_before': before_region,
                          'visible_region_after': [output.scrollable_content_region.width, output.scrollable_content_region.height],
                          'prior_viewport_still_in_rendered_rows': before_text in '\n'.join(line.text for line in output.lines),
                          'rendered_rows_before': len(before_rendered.splitlines()),
                          'rendered_rows_after': len(output.lines),
                          'decoded_before': before_total, 'decoded_after': app.tail.buffer.total,
                          'pretty_bytes_before_append': pretty_before_bytes,
                          'pretty_bytes_after_append': len(stream_pretty.data),
                          'pretty_pause_control': 'none in pretty.py; external terminal scrollback not assessed',
                          'schedule': 'controlled byte replay: first 60%, then 1024-byte chunks every 20ms; deliberately split record, not original runtime cadence'}
                app.save_screenshot(f'{name}-combined-paused.svg', path=str(EVIDENCE))
                await pilot.press('f')
                await pilot.pause(.25)
                paused['follow_resumed'] = app.follow
                paused['follow_at_consumed_end'] = output.scroll_y == output.max_scroll_y
                paused['final'] = ui_metrics(app)
                app.save_screenshot(f'{name}-combined-follow.svg', path=str(EVIDENCE))
                finding['stream_replay'] = paused
                await pilot.press('q')
            follow_text = await stream_pretty.finish(interrupt=True)
            interrupted = True
            assert presence(follow_text)['SPIKE111_MESSAGE_END']
            finding['pretty_follow'] = {'exit_code_after_owned_SIGINT': 0,
                                       'tokens': presence(follow_text),
                                       'capture_labels': 'observer read time; startup bytes can be historical'}
            # Capture labels vary on every replay; preserve this transcript as evidence.
            (EVIDENCE / f'{name}-pretty-follow.txt').write_text(follow_text)
        finally:
            if not interrupted:
                await stream_pretty.finish(interrupt=True)
    return finding


async def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    findings = []
    for runtime in ('claude', 'codex'):
        for size in SIZES:
            result = await compare(runtime, size)
            findings.append(result)
            print(json.dumps({'runtime': runtime, 'size': size,
                              'records': result['combined']['decoded_records'],
                              'evicted': result['combined']['evicted_records'],
                              'log_region': result['combined']['log_region'],
                              'paused_text_stable': result['stream_replay']['same_viewport_text_after_append']}), flush=True)
    result = {'source_commit': 'f9bdba7796e80c977e398dedc139d0717ba55db8',
              'textual_version': __import__('importlib.metadata', fromlist=['version']).version('textual'),
              'checks': 'input SHA verification, pretty PTY exact sequential adapter projection, complete byte drain, view/PTY closure awaited',
              'artifact_publication': 'SVG/text exports strip end-of-line whitespace; assertions and paused measurements use original in-memory output; process.log inputs are never whitespace-normalized',
              'actual_operator_terminal_observation': 'unverified', 'comparisons': findings}
    (EVIDENCE / 'comparison.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    for path in list(EVIDENCE.glob('*.svg')) + list(EVIDENCE.glob('*.txt')):
        path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
