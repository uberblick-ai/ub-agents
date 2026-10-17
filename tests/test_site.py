"""The docs build inlines committed diagrams without a Node runtime."""

from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location('site_build', Path(__file__).resolve().parents[1] / 'site/build.py')
build = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', list(sys.path)):
    spec.loader.exec_module(build)


class Links(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.hrefs = []
        self.ids = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'href' in attrs:
            self.hrefs.append(attrs['href'])
        if 'id' in attrs:
            self.ids.append(attrs['id'])


class SiteTests(unittest.TestCase):
    def test_flow_is_a_standalone_block_and_code_examples_stay_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'diagrams').mkdir()
            svg = '<svg><text>A &amp; B</text></svg>'
            (root / 'diagrams/example.svg').write_text(svg)
            with patch.object(build, 'ROOT', root):
                _, body = build.markdown('Before\n{{flow example}}\nAfter')
                self.assertEqual(body, f'<p>Before</p>\n<figure class="flow">{svg}</figure>\n<p>After</p>')
                for source in ('Inline {{flow example}}', ' {{flow example}}',
                               '{{flow example}} ', '{{flow ../example}}',
                               '```text\n{{flow example}}\n```'):
                    with self.subTest(source=source):
                        self.assertNotIn('<figure', build.markdown(source)[1])
                with self.assertRaises(FileNotFoundError):
                    build.markdown('{{flow missing}}')

    def test_discuss_opens_the_same_prompt_in_claude_and_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'prompts').mkdir()
            (root / 'prompts/example.md').write_text('Review `a.md`\n\nCheck a.md &\nask me.\n')
            (root / 'icons').mkdir()
            for name in ('claude', 'openai'):
                (root / f'icons/{name}.svg').write_text(f'<svg>{name}</svg>\n')
            with patch.object(build, 'ROOT', root):
                _, body = build.markdown('Before\n{{discuss example}}\nAfter')
                q = 'Check%20a.md%20%26%20ask%20me.'
                self.assertEqual(body, '<p>Before</p>\n<div class="discuss"><span>Review <code>a.md</code></span>'
                                 f'<a class="claude" href="claude://code/new?q={q}"><svg>claude</svg>Claude</a>'
                                 f'<a href="codex://new?prompt={q}"><svg>openai</svg>Codex</a></div>\n<p>After</p>')
                self.assertNotIn('<div', build.markdown('Inline {{discuss example}}')[1])

    def test_loop_page_navigation_diagrams_and_links(self):
        # Help expansion is unrelated to diagram rendering and would start a CLI
        # process for every command page during each test worker's discovery.
        with patch.object(build, 'command_help', return_value='Command help'):
            pages = build.collect()
        page = pages[1]
        self.assertEqual([p['title'] for p in pages[:2]], ['Installation', 'How the loop works'])
        self.assertEqual(page['path'], 'docs/how-the-loop-works.html')
        rendered = build.doc_page(pages, 1)
        self.assertNotIn('class="crumb"', rendered)
        self.assertIn('url(../assets/fonts/ibm-plex-sans.woff2)', rendered)
        self.assertIn('class="on" href="how-the-loop-works.html"', rendered)
        self.assertEqual(page['body'].count('<figure class="flow">'), 4)
        self.assertNotIn('{{flow', page['body'])

        ids = Links(rendered).ids
        self.assertEqual(len(ids), len(set(ids)), 'inline SVG ids must be unique across diagrams')
        for name in ('issue', 'issue-review-loop', 'pr', 'approvals'):
            svg = (build.ROOT / 'diagrams' / f'{name}.svg').read_text()
            self.assertIn(svg, page['body'])
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, '{http://www.w3.org/2000/svg}svg')
            for target in re.findall(r'url\(#([^)]+)\)', svg):
                self.assertIn(target, ids)

        targets = {p['path']: Links(p['body']) for p in pages}
        for href in Links(page['body']).hrefs:
            with self.subTest(href=href):
                link = urlsplit(href)
                self.assertFalse(link.scheme)
                path = link.path.lstrip('/') if link.path else page['path']
                self.assertIn(path, targets)
                if link.fragment:
                    self.assertIn(unquote(link.fragment), targets[path].ids)

        installation = pages[0]['body']
        self.assertNotIn('id="how-the-loop-works"', installation)
        self.assertNotIn('href="#how-the-loop-works"', installation)
        self.assertIn('href="/docs/how-the-loop-works.html"', installation)
