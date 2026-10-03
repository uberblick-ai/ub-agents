"""Offline Claude comparisons and separately labeled synthetic history pressure."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time

from textual.widgets import RichLog, Static

from experiments.terminal_observer.demo import View as ArchivedView
from experiments.terminal_observer.local import fixture_observer
from .compare import PrettyPTY, adapter_output, presence, viewport, wrap_terminal
from .view import PAGE_BYTES, View, WindowTail, older_page

ROOT = Path.cwd()
BASE = ROOT / 'experiments/runtime_logs_111/evidence'
EVIDENCE = BASE / 'continuation'
PRIVATE = ROOT / '.ub-agent/spike111-continuation'
SIZES = ((110, 32), (72, 24))


async def drain(app, path, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        pending_draw = bool(getattr(app, 'draw_queue', ()))
        rendered = (not app.follow or app.seen >= app.tail.buffer.total)
        if app.tail.offset == path.stat().st_size and not app.tail.buffer.ready() and not pending_draw and rendered:
            await asyncio.sleep(.15)
            return
        assert time.monotonic() < deadline, 'offline view did not settle'
        await asyncio.sleep(.02)


def rows(app):
    return '\n'.join(line.text for line in app.query_one(RichLog).lines)


def metrics(app):
    log = app.query_one(RichLog)
    refs = getattr(app, 'display_refs', ())
    return {'decoded_since_attach': app.tail.buffer.total,
            'retained_parser_records': len(app.tail.buffer.entries),
            'parser_evictions': app.tail.buffer.discarded,
            'attach_start_byte': getattr(app.tail, 'start', 0),
            'window_start_byte': refs[0][0] if refs else None,
            'window_end_byte': refs[-1][1] if refs else None,
            'rendered_rows': len(log.lines),
            'log_region': [log.scrollable_content_region.width, log.scrollable_content_region.height],
            'scroll_y': float(log.scroll_y), 'max_scroll_y': float(log.max_scroll_y),
            'follow': app.follow, 'note': str(app.query_one('#log_note', Static).render()),
            'rendered_tokens': presence(rows(app))}


def screenshot(app, name):
    app.save_screenshot(name + '.svg', path=str(EVIDENCE))


def transcript(name, text):
    (EVIDENCE / (name + '.txt')).write_text(text + '\n')


async def captured(size):
    source = BASE / 'claude/process.log'
    data = source.read_bytes()
    meta = json.loads((source.parent / 'capture.json').read_text())
    assert hashlib.sha256(data).hexdigest() == meta['sanitized_sha256']
    _, entries = adapter_output(source)
    pretty = PrettyPTY(source, size)
    text = await pretty.finish()
    assert text == ''.join(entry.display() + '\n' for entry in entries)
    name = f'claude-{size[0]}x{size[1]}'
    transcript(name + '-pretty', text)
    result = {'label': 'complete captured Claude fixture; offline replay, owned pretty PTY, Textual headless view',
              'size': size, 'input_sha256': meta['sanitized_sha256'],
              'pretty_records': len(entries), 'pretty_equals_same_adapter': True,
              'pretty_wrapped_rows_model': len(wrap_terminal(text, size[0])),
              'pretty_tokens': presence(text)}
    app = View(fixture_observer(ROOT, source))
    started = time.monotonic()
    async with app.run_test(size=size) as pilot:
        await drain(app, source)
        result['attach_seconds'] = round(time.monotonic() - started, 3)
        result['combined'] = metrics(app)
        assert [ref[2].display() for ref in app.display_refs] == [entry.display() for entry in entries]
        screenshot(app, name + '-tail')
        await pilot.press('pageup')
        app.query_one(RichLog).scroll_home(animate=False)
        await pilot.pause(.15)
        screenshot(app, name + '-message')
        output = app.query_one(RichLog)
        for label, token in (('tool-call', 'tool Bash'), ('command-result', 'SPIKE111_COMMAND_BEGIN'),
                             ('error', '[error]'), ('shortening', 'display shortened')):
            row = next((i for i, line in enumerate(output.lines) if token in line.text), None)
            assert row is not None, token
            output.scroll_to(y=row, animate=False, force=True)
            await pilot.pause(.15)
            screenshot(app, name + '-' + label)
        before = rows(app)
        before_view = viewport(output)
        await pilot.press('2', '1')
        await pilot.pause(.3)
        assert rows(app) == before and viewport(output) == before_view
        result['pane_revisit_same_rows_and_viewport'] = True
        await pilot.press('u')
        await pilot.pause(.3)
        assert '"type"' in rows(app)
        screenshot(app, name + '-raw-projection')
        await pilot.press('u', 'f')
        await drain(app, source)
        assert app.follow and output.scroll_y == output.max_scroll_y
        await pilot.press('q')

    with tempfile.TemporaryDirectory(dir=PRIVATE) as directory:
        path = Path(directory) / 'process.log'
        path.touch()
        app = View(fixture_observer(ROOT, path))
        pretty = PrettyPTY(path, size, follow=True)
        closed = False
        try:
            async with app.run_test(size=size) as pilot:
                await pilot.pause(.2)
                split = len(data) * 3 // 5
                with path.open('ab') as writer:
                    writer.write(data[:split])
                await drain(app, path)
                assert app.tail.buffer.preview() is not None
                screenshot(app, name + '-partial')
                await pilot.press('pageup')
                await pilot.pause(.15)
                before = viewport(app.query_one(RichLog))
                before_rows = rows(app)
                before_region = metrics(app)['log_region']
                transcript(name + '-paused-before', before)
                started = time.monotonic()
                for offset in range(split, len(data), 1024):
                    with path.open('ab') as writer:
                        writer.write(data[offset:offset + 1024])
                    pretty.drain()
                    await asyncio.sleep(.02)
                await drain(app, path)
                append_seconds = time.monotonic() - started
                after = viewport(app.query_one(RichLog))
                assert not app.follow and before == after and before_rows == rows(app)
                assert before_region == metrics(app)['log_region']
                transcript(name + '-paused-after', after)
                screenshot(app, name + '-paused-after')
                await pilot.press('2', '1')
                await pilot.pause(.2)
                assert before == viewport(app.query_one(RichLog))
                await pilot.press('f')
                await drain(app, path)
                output = app.query_one(RichLog)
                assert app.follow and output.scroll_y == output.max_scroll_y
                screenshot(app, name + '-follow')
                result['stream'] = {'schedule': 'captured bytes: first 60%, then 1024B/20ms; not producer cadence',
                                    'partial_visible': True, 'paused_rows_and_region_identical': True,
                                    'paused_pane_revisit_identical': True, 'follow_at_consumed_end': True,
                                    'append_and_drain_seconds': round(append_seconds, 3), 'final': metrics(app)}
                await pilot.press('q')
            follow_text = await pretty.finish(interrupt=True)
            closed = True
            assert presence(follow_text)['SPIKE111_MESSAGE_END']
            transcript(name + '-pretty-follow', follow_text)
        finally:
            if not closed:
                await pretty.finish(interrupt=True)
    return result


async def sustained(size):
    data = (BASE / 'claude/process.log').read_bytes()
    with tempfile.TemporaryDirectory(dir=PRIVATE) as directory:
        path = Path(directory) / 'process.log'
        path.touch()
        app = View(fixture_observer(ROOT, path))
        tick_ms = []
        original = app.tick
        def measured_tick():
            started = time.monotonic()
            original()
            tick_ms.append((time.monotonic() - started) * 1000)
        app.tick = measured_tick
        max_backlog = 0
        partial_observations = 0
        async with app.run_test(size=size) as pilot:
            await pilot.pause(.2)
            started = time.monotonic()
            for offset in range(0, len(data), 256):
                with path.open('ab') as writer:
                    writer.write(data[offset:offset + 256])
                await asyncio.sleep(.05)
                max_backlog = max(max_backlog, path.stat().st_size - app.tail.offset)
                partial_observations += app.tail.buffer.preview() is not None
            producer_seconds = time.monotonic() - started
            await drain(app, path)
            settle_seconds = time.monotonic() - started
            assert app.tail.buffer.total == 18 and app.follow
            assert app.query_one(RichLog).scroll_y == app.query_one(RichLog).max_scroll_y
            assert partial_observations > 0
            screenshot(app, f'claude-{size[0]}x{size[1]}-sustained-follow')
            await pilot.press('q')
        return {'label': 'complete captured Claude bytes; sustained controlled replay, not producer cadence',
                'size': size, 'schedule': '256B every 50ms, from empty file to complete 57851B recording',
                'producer_seconds': round(producer_seconds, 3),
                'producer_and_settle_seconds': round(settle_seconds, 3),
                'max_observed_byte_backlog': max_backlog,
                'partial_observations': partial_observations,
                'max_tick_callback_ms': round(max(tick_ms), 3),
                'tick_limit': 'callback only; excludes asynchronous painting and terminal transport',
                'decoded_records': 18, 'follow_at_consumed_end': True}


def synthetic_record(index):
    return (json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': f'SYNTHETIC_ROW_{index:04d}: ' + 'bounded history load ' * 12}]}}) + '\n').encode()


async def history_pressure(view_type, size):
    name = f'{"after" if view_type is View else "before"}-synthetic-{size[0]}x{size[1]}'
    with tempfile.TemporaryDirectory(dir=PRIVATE) as directory:
        path = Path(directory) / 'process.log'
        path.write_bytes(b''.join(synthetic_record(i) for i in range(200)))
        app = view_type(fixture_observer(ROOT, path))
        async with app.run_test(size=size) as pilot:
            await drain(app, path)
            await pilot.press('pageup')
            app.query_one(RichLog).scroll_home(animate=False)
            await pilot.pause(.2)
            before = viewport(app.query_one(RichLog))
            original_rows = rows(app)
            transcript(name + '-paused-before', before)
            screenshot(app, name + '-paused-before')
            poll_durations = []
            original_poll = app.tail.poll
            def measured_poll():
                started = time.monotonic()
                original_poll()
                poll_durations.append(time.monotonic() - started)
            app.tail.poll = measured_poll
            started = time.monotonic()
            for block in range(200, 1800, 40):
                with path.open('ab') as writer:
                    writer.write(b''.join(synthetic_record(i) for i in range(block, block + 40)))
                await asyncio.sleep(.03)
            producer_seconds = time.monotonic() - started
            await drain(app, path)
            elapsed = time.monotonic() - started
            after = viewport(app.query_one(RichLog))
            transcript(name + '-paused-after', after)
            screenshot(app, name + '-paused-after')
            result = {'label': 'synthetic Claude-shaped load; not captured runtime events',
                      'size': size, 'records': 1800, 'bytes': path.stat().st_size,
                      'schedule': '200 initial, then 40 events/30ms through 1800',
                      'synthetic_input_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'producer_seconds': round(producer_seconds, 3),
                      'producer_and_drain_seconds': round(elapsed, 3),
                      'max_tail_poll_ms': round(max(poll_durations, default=0) * 1000, 3),
                      'paused_viewport_identical': before == after,
                      'paused_all_rendered_rows_identical': original_rows == rows(app),
                      'after_append': metrics(app)}
            await pilot.press('2', '1')
            await pilot.pause(1)
            result['paused_revisit_identical'] = before == viewport(app.query_one(RichLog))
            screenshot(app, name + '-revisit')
            if view_type is View:
                assert result['paused_viewport_identical'] and result['paused_all_rendered_rows_identical']
                assert result['paused_revisit_identical'] and app.tail.buffer.discarded >= 1600
            else:
                assert not result['paused_viewport_identical'], 'baseline defect was not reproduced'
            await pilot.press('f')
            resumed = time.monotonic()
            await drain(app, path)
            assert app.follow and app.query_one(RichLog).scroll_y == app.query_one(RichLog).max_scroll_y
            screenshot(app, name + '-follow')
            result['follow'] = metrics(app)
            result['follow_resume_and_settle_seconds'] = round(time.monotonic() - resumed, 3)
            if view_type is View:
                ranges = []
                visited = set()
                while True:
                    refs = app.display_refs
                    ranges.append([refs[0][0], refs[-1][1], len(refs)])
                    visited.update(ref[2].text.split(':', 1)[0] for ref in refs)
                    if refs[0][0] == 0:
                        break
                    await pilot.press('h')
                    await pilot.pause(1)
                    assert app.display_refs[0][0] < ranges[-1][0]
                    assert app.display_refs[-1][1] == ranges[-1][0]
                assert len(visited) == 1800
                screenshot(app, name + '-oldest-page')
                result['history'] = {'distinct_events_reached': len(visited), 'pages': len(ranges),
                                     'contiguous_source_byte_ranges_latest_to_oldest': ranges}
            await pilot.press('q')
        if view_type is View:
            # A fresh attachment to this large existing file starts within 256KiB,
            # not at byte zero. Every normal record remains reachable backwards.
            tail = WindowTail(path)
            assert tail.start > 0 and path.stat().st_size - tail.start <= PAGE_BYTES
            while tail.offset < path.stat().st_size or tail.buffer.ready():
                tail.poll()
            refs = list(tail.buffer.refs)
            count = len(refs)
            while refs[0][0]:
                previous, _ = older_page(path, refs[0][0])
                assert previous[-1][1] == refs[0][0]
                refs = previous
                count += len(refs)
            assert count == 1800
            result['fresh_attach'] = {'start_byte': tail.start, 'file_bytes': path.stat().st_size,
                                      'max_initial_read_bytes': PAGE_BYTES, 'all_records_reachable': count}
            attached = View(fixture_observer(ROOT, path))
            started = time.monotonic()
            async with attached.run_test(size=size) as pilot:
                await drain(attached, path)
                assert attached.display_refs[-1][1] == path.stat().st_size
                result['fresh_attach']['view_settle_seconds'] = round(time.monotonic() - started, 3)
                result['fresh_attach']['view'] = metrics(attached)
                screenshot(attached, name + '-fresh-attach')
                await pilot.press('q')
        return result


async def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    synthetic_path = EVIDENCE / 'synthetic/process.log'
    synthetic_path.parent.mkdir(parents=True, exist_ok=True)
    synthetic_path.write_bytes(b''.join(synthetic_record(i) for i in range(1800)))
    results = {'baseline_commit': '1fd246e3aef664b953cfa8fbeff084bac0715019',
               'operator_terminal_usability': 'unverified; owned PTY/headless evidence only',
               'synthetic_input': 'synthetic/process.log; generated by synthetic_record(0..1799)',
               'captured_claude': [], 'sustained_captured_replay': [], 'synthetic_history': []}
    for size in SIZES:
        result = await captured(size)
        results['captured_claude'].append(result)
        print(f'PASS: captured Claude {size}: same formatter, partial/pause/follow/revisit/raw', flush=True)
        result = await sustained(size)
        results['sustained_captured_replay'].append(result)
        print(f'PASS: sustained captured Claude {size}: {result["producer_seconds"]}s, follow at end', flush=True)
        for view_type in (ArchivedView, View):
            result = await history_pressure(view_type, size)
            results['synthetic_history'].append(result)
            print(f'PASS: {view_type.__module__} {size}: paused stable={result["paused_viewport_identical"]}', flush=True)
    (EVIDENCE / 'validation.json').write_text(json.dumps(results, indent=2) + '\n')
    for path in list(EVIDENCE.glob('*.svg')) + list(EVIDENCE.glob('*.txt')):
        path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines()).rstrip('\n') + '\n')


if __name__ == '__main__':
    asyncio.run(main())
