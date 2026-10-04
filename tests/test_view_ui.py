import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_view_data import event, fixture
from tests.test_view_github import reply
from tests.support import RecordingDescriptionTransport
from ub_agents.view_github import DescriptionLoads, Response, parse_response

from textual.widgets import Markdown, Static, TabbedContent, Tree
from ub_agents.view_ui import LogPane, MAX_RENDER_LINES, View
from ub_agents.view_worker import LocalWorker


class ViewUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, self.log, self.state = fixture(self.root, count=600)

    async def ready(self, app, pilot, condition=None):
        for _ in range(150):
            await pilot.pause(0.03)
            if condition() if condition else app.reading.page is not None:
                return
        self.fail('View did not become ready')

    def description_source(self, source, title, body):
        if source == 'snapshot':
            row = self.state['latest_pass']['rows'][0]
            row['title'] = title
            row['description'] = {'available': True, 'text': body}
            self.path.write_text(json.dumps(self.state))
        elif source == 'run context.json':
            (self.log.parent / 'context.json').write_text(json.dumps({'title': title, 'body': body}))
        else:
            (self.log.parent / 'context.json').unlink()
        transport = RecordingDescriptionTransport()
        transport.response = parse_response(reply(title, body), b'', 0, 1000)
        return transport

    async def test_markdown_body_from_every_source_is_formatted_and_inert(self):
        title = '[bold]Title[/bold]\r\nnext\ttitle\rlast\x1b[31m'
        body = ('# Overview\r\n\r\nfirst\tline\rsecond\nthird\n\n'
                '- **strong** and *emphasis* with `code`\n'
                '- [bold]literal[/bold] \x1b[31mred\n\n'
                '1. Ordered\n\n```text\ncode\tline\nnext line\n```\n\n'
                '[web](https://example.invalid) <https://example.invalid>\n'
                '[local](file:///missing) [anchor](#overview)\n'
                '![picture](https://example.invalid/pic.png)\n'
                '<b>HTML</b> <img src="https://example.invalid/pic.png">\n'
                '&#27; &#x9b; [ref][target]\n[target]: https://example.invalid')
        expected = body.replace('\r\n', '\n').replace('\r', '\n').replace('\x1b', r'\x1b')
        for source in ('snapshot', 'run context.json', 'GitHub'):
            with self.subTest(source=source):
                self.path, self.log, self.state = fixture(self.root)
                transport = self.description_source(source, title, body)
                app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                with patch.object(app, 'open_url', side_effect=AssertionError('opened link')), \
                     patch.object(Markdown, 'load', side_effect=AssertionError('loaded link')), \
                     patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
                    async with app.run_test(size=(110, 32)) as pilot:
                        await self.ready(app, pilot)
                        await pilot.press('2', 'g')
                        await self.ready(app, pilot, lambda: f'Source: {source}' in (app.last_context or ''))
                        markdown = app.query_one('#issue_body', Markdown)
                        await self.ready(app, pilot, lambda: len(markdown.query('MarkdownBullet')) == 3)
                        self.assertEqual(markdown.source, expected)
                        self.assertEqual(len(markdown.query('MarkdownH1')), 1)
                        self.assertEqual(len(markdown.query_one('MarkdownBulletList').query('MarkdownBullet')), 2)
                        self.assertEqual(len(markdown.query_one('MarkdownOrderedList').query('MarkdownBullet')), 1)
                        content = [block.render() for block in markdown.query('MarkdownParagraph')]
                        rendered = '\n'.join(part.plain for part in content)
                        self.assertIn('\nsecond\nthird', rendered)
                        self.assertIn('[bold]literal[/bold]', rendered)
                        self.assertIn(r'\x1b[31mred', rendered)
                        self.assertNotIn('\x1b', rendered)
                        for literal in ('[web](https://example.invalid)', '<https://example.invalid>',
                                        '[local](file:///missing)', '[anchor](#overview)',
                                        '![picture](https://example.invalid/pic.png)', '<b>HTML</b>',
                                        '<img src="https://example.invalid/pic.png">',
                                        '&#27; &#x9b;', '[ref][target]', '[target]: https://example.invalid'):
                            self.assertIn(literal, rendered)
                        spans = [span for part in content for span in part.spans]
                        for style in ('.strong', '.em', '.code_inline'):
                            self.assertTrue(any(span.style == style for span in spans))
                        self.assertTrue(all(isinstance(span.style, str) or not span.style.meta for span in spans))
                        header = app.query_one('#issue_text', Static).render()
                        self.assertIn('[bold]Title[/bold]\nnext\ttitle\nlast' + r'\x1b[31m', header.plain)
                        self.assertFalse(header.spans)
                        self.assertIn(f'Source: {source}', app.query_one('#issue_note', Static).render().plain)
                        self.assertNotIn('Source:', markdown.source)
                        # Neither mouse nor keyboard activation has a link target.
                        await pilot.click(markdown.query_one('MarkdownParagraph'), offset=(2, 0))
                        await pilot.press('tab', 'enter', 'space')
                        markdown.post_message(Markdown.LinkClicked(markdown, 'https://example.invalid'))
                        await pilot.pause()
                        self.assertEqual(len(transport.calls), 1 if source == 'GitHub' else 0)
                        await pilot.press('q')
                app.worker.thread.join(2)

    async def test_shortened_code_fence_keeps_plain_notice_for_every_source(self):
        body = '# Start\n\n```text\n' + 'x' * 3000 + '\n```\nEnd'
        for source in ('snapshot', 'run context.json', 'GitHub'):
            with self.subTest(source=source):
                self.path, self.log, self.state = fixture(self.root)
                transport = self.description_source(source, 'Title', body)
                app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
                async with app.run_test(size=(110, 32)) as pilot:
                    await self.ready(app, pilot)
                    await pilot.press('2', 'g')
                    await self.ready(app, pilot, lambda: f'Source: {source}' in (app.last_context or ''))
                    markdown = app.query_one('#issue_body', Markdown)
                    await self.ready(app, pilot, lambda: len(markdown.query('MarkdownFence')) == 1)
                    self.assertEqual(markdown.source, body[:2048])
                    self.assertNotIn('shortened', markdown.query_one('MarkdownFence').code)
                    note = app.query_one('#issue_note', Static)
                    self.assertIn('Description shortened to 2,048 characters.', note.render().plain)
                    self.assertFalse(note.render().spans)
                    self.assertGreater(note.region.y, markdown.region.y)
                    self.assertLess(note.region.bottom, 32)
                    await pilot.press('q')
                app.worker.thread.join(2)

    async def test_description_requests_local_sources_cache_and_navigation(self):
        transport = RecordingDescriptionTransport()
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('g', '2', 'g')
            self.assertEqual(transport.calls, [])  # run context is already present
            self.assertIn('Source: run context.json', app.last_context)
            self.assertIn('s old', app.last_context)
            own_key = app.selected
            plan_key = next(k for k, row in app.rows.items() if row.item == 114 and k != own_key)
            app.select(plan_key)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            self.assertIn('Cached description', app.last_context)
            self.assertEqual(transport.calls, [])
            foreign = next(k for k, row in app.rows.items() if row.item == 12)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('1', 'g', '3', 'g', '2')
            await pilot.resize_terminal(120, 35)
            await pilot.pause(0.4)  # redraws and local timers
            self.assertEqual(transport.calls, [])
            await pilot.press('g')
            self.assertEqual(transport.calls, [('example/repo', 12)])
            self.assertIn('Loading title/body', app.last_context)
            await pilot.press('g', 'g')
            app.select(own_key)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('2', 'g', '3', '1', 'f', 'u')
            # Logs keep ingesting while the independent GitHub request is hung.
            before = app.reading.log.total_entries
            with self.log.open('ab') as stream:
                stream.write(event(9000))
            await self.ready(app, pilot, lambda: app.reading.log.total_entries > before)
            self.assertEqual(len(transport.calls), 1)
            transport.response = Response('Loaded title', 'Loaded body')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            for _ in range(3):
                app.select(foreign)
                await self.ready(app, pilot, lambda: app.local_description is not None)
                await pilot.press('2', 'g', '3', '2')
                self.assertIn('Loaded body', app.last_context)
                self.assertIn('Source: GitHub', app.last_context)
                app.select(own_key)
                await self.ready(app, pilot, lambda: app.local_description is not None)
            self.assertEqual(len(transport.calls), 1)
            # A shortened snapshot is available, including an empty body.
            self.state['latest_pass']['rows'][0]['description'] = {
                'available': True, 'text': '', 'omitted_characters': 1000}
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.local_description.source == 'snapshot')
            await pilot.press('2', 'g')
            self.assertIn('Description shortened in snapshot', app.last_context)
            self.assertEqual(len(transport.calls), 1)
            await pilot.press('q')
        self.assertTrue(transport.closed)
        app.worker.thread.join(2)

    async def test_description_failure_explicit_retry_and_global_cooldown(self):
        transport = RecordingDescriptionTransport()
        now = [1000]
        app = View(self.root, self.path, descriptions=DescriptionLoads(transport, clock=lambda: now[0]))
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            own = app.selected
            foreign = next(k for k, row in app.rows.items() if row.item == 12)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('2', 'g')
            transport.response = Response(error='Access denied')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('Access denied', app.last_context)
            self.assertIn('Press g on Issue to retry', app.last_context)
            app.select(own)
            await pilot.pause(0.3)
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.pause(0.3)
            self.assertEqual(len(transport.calls), 1)
            await pilot.press('g')
            transport.response = Response(error='rate limit', reset=1100)
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('cooldown until', app.last_context)
            await pilot.press('g', 'g')
            other = next(k for k, row in app.rows.items() if row.item == 10)
            app.select(other)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            self.assertEqual(len(transport.calls), 2)
            now[0] = 1100
            await pilot.pause(0.3)
            self.assertEqual(len(transport.calls), 2)  # expiry never auto-retries
            app.select(foreign)
            await self.ready(app, pilot, lambda: app.local_description is not None)
            await pilot.press('g')
            transport.response = Response('Recovered title', 'Recovered body')
            await self.ready(app, pilot, lambda: app.descriptions.pending is None)
            self.assertIn('Recovered body', app.last_context)
            self.assertEqual(len(transport.calls), 3)
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_quit_closes_pending_recording_transport_for_both_keys(self):
        for key in ('q', 'ctrl+c'):
            transport = RecordingDescriptionTransport()
            app = View(self.root, self.path, descriptions=DescriptionLoads(transport))
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                app.select(next(k for k, row in app.rows.items() if row.item == 12))
                await self.ready(app, pilot, lambda: app.local_description is not None)
                await pilot.press('2', 'g')
                self.assertIsNotNone(app.descriptions.pending)
                await pilot.press(key)
            self.assertTrue(transport.closed)
            self.assertIsNone(app.descriptions.pending)
            app.worker.thread.join(2)

    async def test_pause_survives_sustained_reader_and_render_eviction_tabs_panes_raw_resize(self):
        app = View(self.root, self.path)
        with patch('subprocess.Popen', side_effect=AssertionError('view invoked a process')):
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                self.assertIn('Supervisor observed running', app.reason_nodes[app.selected].label.plain)
                output = app.query_one('#output', LogPane)
                await pilot.press('f', 'home', 'pagedown')
                await pilot.pause()
                key = app.selected
                page, lines, anchor = app.reading.page, tuple(output.lines), output.anchor()
                self.assertFalse(app.reading.follow)
                # A burst beyond the 200-entry reader and 400-line renderer bounds.
                with self.log.open('ab') as stream:
                    stream.write(b''.join(event(i, 1800) for i in range(600, 1700)))
                await self.ready(app, pilot, lambda: app.reading.log and app.reading.log.evicted_entries > 200)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(tuple(output.lines), lines)
                self.assertEqual(output.anchor(), anchor)
                self.assertGreater(app.reading.log.total_entries - app.reading.seen, 200)
                self.assertIn('PAUSED', str(app.query_one('#status', Static).render()))
                focus = app.focused
                await pilot.press('2', '3', '1')
                await pilot.pause()
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                foreign = next(k for k, row in app.rows.items() if row.item == 12)
                app.select(foreign)
                await pilot.pause(0.3)
                self.assertIsNone(app.reading.page)
                self.assertIn('Owner: @other-launcher', str(app.query_one('#log_note').render()))
                app.select(key)
                await pilot.pause(0.3)
                self.assertEqual(app.reading.page, page)
                self.assertEqual(output.anchor(), anchor)
                await pilot.press('u')
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.press('u')
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(130, 40)
                await pilot.pause()
                self.assertEqual(output.anchor()[0], anchor[0])
                await pilot.resize_terminal(110, 32)
                await pilot.press('f')
                await self.ready(app, pilot, lambda: app.reading.page != page)
                self.assertTrue(app.reading.follow)
                self.assertLessEqual(len(output.lines), MAX_RENDER_LINES)
                self.assertGreater(output.hidden, 0)
                self.assertIn('entries hidden', str(app.query_one('#log_note').render()))
                await pilot.press('q')
        app.worker.thread.join(2)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_history_navigation_and_paused_generation_notice(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            old_start = app.reading.page.start
            entered, release = threading.Event(), threading.Event()
            original = app.worker.read
            def slow_history(request):
                result = original(request)
                if request.older_end is not None:
                    entered.set()
                    release.wait(5)
                return result
            try:
                with patch.object(app.worker, 'read', side_effect=slow_history):
                    await pilot.press('h')
                    await self.ready(app, pilot, entered.is_set)
                    await pilot.press('f')
                    release.set()
                    await pilot.pause(0.4)
                    self.assertTrue(app.reading.follow)
                    self.assertEqual(app.reading.page.start, old_start)
            finally:
                release.set()
            await pilot.press('h')
            await self.ready(app, pilot, lambda: app.reading.page.start < old_start)
            self.assertFalse(app.reading.follow)
            page = app.reading.page
            other = self.log.with_suffix('.next')
            other.write_bytes(event(9000))
            other.replace(self.log)
            await self.ready(app, pilot, lambda: app.reading.latest.generation != page.generation)
            self.assertEqual(app.reading.page, page)
            self.assertIn('FILE CHANGED', str(app.query_one('#log_note').render()))
            await pilot.press('h')
            self.assertIn('File changed', app.reading.notice)
            await pilot.press('f')
            self.assertIn('event 09000', app.reading.page.refs[-1].value.text)
            self.assertNotIn('event 00599', app.reading.page.refs[-1].value.text)
            await pilot.press('p')
            await pilot.pause()
            self.assertEqual(app.screen.__class__.__name__, 'RawAccess')
            self.assertIn('FOLLOW', str(app.screen.query_one('#raw_status').render()))
            await pilot.press('pageup', 'pagedown', 'home', 'end', 'f', 'u', 'h', '2', '3', '1')
            self.assertEqual(app.screen.__class__.__name__, 'RawAccess')
            await pilot.press('q')
        app.worker.thread.join(2)

    async def test_slow_reads_do_not_block_keys_and_focus_and_snapshot_replacement_stay_stable(self):
        app = View(self.root, self.path)
        entered, release = threading.Event(), threading.Event()
        try:
            async with app.run_test(size=(110, 32)) as pilot:
                await self.ready(app, pilot)
                output = app.query_one('#output', LogPane)
                output.focus()
                selected = app.selected
                tree = app.query_one('#work', Tree)
                node = app.nodes[selected]
                self.state['latest_pass']['state'] = 'complete'
                self.state['assignment']['process'] = 'exited'
                next_path = self.path.with_suffix('.next')
                next_path.write_text(json.dumps(self.state))
                next_path.replace(self.path)
                await self.ready(app, pilot, lambda: app.rows[selected].state == 'exited')
                self.assertEqual(app.selected, selected)
                self.assertIs(app.nodes[selected], node)
                self.assertIs(app.focused, output)
                original = app.worker.read
                def slow_read(request):
                    entered.set()
                    release.wait(5)
                    return original(request)
                with patch.object(app.worker, 'read', side_effect=slow_read):
                    await self.ready(app, pilot, entered.is_set)
                    await pilot.press('f', '2', '3', '1', 'u')
                    self.assertFalse(app.reading.follow)
                    self.assertTrue(app.reading.raw)
                    self.assertEqual(app.query_one(TabbedContent).active, 'log')
                    await pilot.press('q')
                    release.set()
        finally:
            release.set()
            app.worker.thread.join(6)
        self.assertFalse(app.worker.thread.is_alive())

    async def test_malformed_snapshot_retains_paused_page_and_content_at_minimum_size(self):
        app = View(self.root, self.path)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.ready(app, pilot)
            await pilot.press('f')
            page = app.reading.page
            self.state['latest_pass']['state'] = '[/red]'
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.session.data.get('latest_pass', {}).get('state') == '[/red]')
            self.assertIn('[/red]', app.groups['Latest pass'].label.plain)
            self.state['assignment'] = None
            self.path.write_text(json.dumps(self.state))
            await self.ready(app, pilot, lambda: app.rows[app.selected].state == 'earlier observation')
            self.assertIn('earlier observation', app.last_context)
            self.path.write_text('{broken')
            await self.ready(app, pilot, lambda: app.session.error is not None)
            self.assertEqual(app.reading.page, page)
            self.assertEqual(app.rows[app.selected].state, 'earlier observation')
            self.assertIn('malformed', str(app.query_one('#status').render()))
            self.assertGreater(app.query_one('#output').size.height, 15)
            self.assertGreater(app.query_one('#output').size.width, 60)
            await pilot.press('ctrl+c')
        app.worker.thread.join(2)


if __name__ == '__main__':
    unittest.main()
