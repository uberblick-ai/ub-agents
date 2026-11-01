"""The docs build inlines committed diagrams without a Node runtime."""

import html
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urljoin, urlsplit
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
    def test_workflow_gallery_has_two_cards_and_no_docs_sidebar(self):
        page = build.workflow_gallery()
        self.assertIn('<h1>Workflows</h1>', page)
        self.assertEqual(page.count('class="workflow-card"'), 2)
        self.assertEqual(page.count('data-mode="preview"'), 2)
        self.assertNotIn('<details class="side"', page)
        self.assertIn('href="default/"', page)
        self.assertIn('href="dependabot/"', page)
        for workflow in build.WORKFLOWS:
            self.assertIn(workflow['title'], page)
            self.assertIn(workflow['intent'], html.unescape(page))
        self.assertIn(build.WORKFLOWS[0]['about'], page)
        ids = Links(page).ids
        self.assertEqual(len(ids), len(set(ids)), 'gallery SVG ids must be unique')

    def test_workflow_scenarios_have_urls_and_native_playback_data(self):
        expected = {'default': ['clean-merge', 'one-fix-round', 'a-person-decides'],
                    'dependabot': ['clean-merge', 'tests-failing', 'fix-fails', 'major-release']}
        pages = dict(build.workflow_pages())
        self.assertEqual(len(pages), 10)
        for workflow in build.WORKFLOWS:
            name = workflow['name']
            timelines = build.workflow_timelines(name)
            self.assertEqual([build.slug(t['name']) for t in timelines], expected[name])
            svg = ET.fromstring((build.ROOT / f'diagrams/workflow-{name}.svg').read_text())
            parts = {el.attrib['data-ab-part'] for el in svg.iter() if 'data-ab-part' in el.attrib}
            for timeline in timelines:
                for step in timeline['steps']:
                    self.assertTrue(step['caption'])
                    self.assertTrue(step['actor'])
                    self.assertGreater(step['ms'], 0)
                    self.assertTrue(set(step['parts']) <= parts)
            for scenario in ['', *expected[name]]:
                path = f'workflows/{name}/' + (f'{scenario}/' if scenario else '') + 'index.html'
                page = pages[path]
                self.assertIn(f'data-scenario="{scenario}"', page)
                self.assertIn(f'href="{build.SITE}/{path.removesuffix("index.html")}"', page)
                data = json.loads(re.search(r'class="workflow-data">(.*?)</script>', page).group(1))
                self.assertEqual(data['timelines'], timelines)
                for url, target in zip(data['urls'], expected[name]):
                    self.assertEqual(urljoin(path, url), f'workflows/{name}/{target}/')
                self.assertIn('type="module" src="', page)
                self.assertNotIn('<details class="side"', page)

    def test_workflow_stories_and_adopt_prompts(self):
        for workflow in build.WORKFLOWS:
            name = workflow['name']
            _, page = build.workflow_entry(workflow)
            prompt = ' '.join((build.ROOT / f'prompts/workflow-{name}.md').read_text().split())
            self.assertIn(workflow['title'], prompt)
            self.assertIn(f'{build.SITE}/workflows/{name}/', prompt)
            self.assertIn('ub-agents.yaml', prompt)
            self.assertIn('loop policy', prompt)
            self.assertIn('smallest', prompt)
            self.assertIn('diff before writing anything', prompt)
            self.assertIn('Where you step in', page)
            for link in Links(page).hrefs:
                if link.startswith(('claude://', 'codex://')):
                    self.assertEqual(unquote(link.split('=', 1)[1]), prompt)
            self.assertEqual(page.count('class="discuss"'), 1)
            self.assertIn('claude://code/new?q=', page)
            self.assertIn('codex://new?prompt=', page)
            if name == 'dependabot':
                self.assertIn('https://uberblick.ai/blog/2026/agents-auto-merge-dependabot', Links(page).hrefs)
                for requirement in ('0.3.5', 'trusted-pr-authors: [dependabot]', 'dependencies',
                                    'one fix round', 'max-rounds: 1', 'changes-requested'):
                    self.assertIn(requirement, prompt)

    def test_build_writes_workflow_routes_sitemap_and_assets_without_node(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            with (patch.object(build, 'OUT', out),
                  patch.object(build, 'command_help', return_value='Command help'),
                  patch.object(build.subprocess, 'run') as run):
                build.main()
            run.assert_not_called()
            for path, _ in build.workflow_pages():
                page = (out / path).read_text()
                self.assertIn(build.public_url(path), (out / 'sitemap.xml').read_text())
                self.assertIn(f'href="{build.rel("workflows/", path)}">Workflows</a>', page)
            for path in ('index.html', 'docs/installation.html', '404.html'):
                self.assertIn('>Workflows</a>', (out / path).read_text())
            for asset in ('workflows.js', 'ablauf-playback.js'):
                self.assertEqual((out / 'assets' / asset).read_bytes(),
                                 (build.ROOT / 'static/assets' / asset).read_bytes())

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
            (root / 'prompts/example.md').write_text('Check a.md &\nask me.\n')
            (root / 'icons').mkdir()
            for name in ('claude', 'openai'):
                (root / f'icons/{name}.svg').write_text(f'<svg>{name}</svg>\n')
            with patch.object(build, 'ROOT', root):
                q = 'Check%20a.md%20%26%20ask%20me.'
                self.assertEqual(build.inline('- Review `a.md`. {{discuss example}}'),
                                 '- Review <code>a.md</code>. <span class="discuss">Discuss with:'
                                 f'<a class="claude" href="claude://code/new?q={q}" title="Opens the Claude desktop app">'
                                 '<svg>claude</svg>Claude</a>'
                                 f'<a href="codex://new?prompt={q}" title="Opens the Codex app"><svg>openai</svg>Codex</a></span>')
                self.assertIn('{{discuss example}}', build.markdown('```text\n{{discuss example}}\n```')[1])

    def test_shell_blocks_get_a_copy_button(self):
        _, body = build.markdown('```sh\nub-agents init\n```\n\n```text\nub-agents init\n```')
        self.assertEqual(body.count('class="copy-code"'), 1)
        self.assertTrue(body.startswith('<div class="code"><pre class="lang-sh"><code><span class="p">$ </span>ub-agents init</code></pre><button'))

    def test_loop_page_navigation_diagrams_and_links(self):
        # Help expansion is unrelated to diagram rendering and would start a CLI
        # process for every command page during each test worker's discovery.
        with patch.object(build, 'command_help', return_value='Command help'):
            pages = build.collect()
        page = pages[2]
        self.assertEqual([p['title'] for p in pages[:3]], ['Motivation', 'Installation', 'How the loop works'])
        self.assertEqual(page['path'], 'docs/how-the-loop-works.html')
        rendered = build.doc_page(pages, 2)
        self.assertNotIn('class="crumb"', rendered)
        self.assertIn('url(../assets/fonts/ibm-plex-sans.woff2)', rendered)
        self.assertIn('class="on" href="how-the-loop-works.html"', rendered)
        self.assertIn('<link rel="canonical" href="https://agents.uberblick.ai/docs/how-the-loop-works">', rendered)
        self.assertEqual([build.public_url(p) for p in ('index.html', 'docs/commands/index.html', 'docs/upgrading.html')],
                         ['https://agents.uberblick.ai/', 'https://agents.uberblick.ai/docs/commands/',
                          'https://agents.uberblick.ai/docs/upgrading'])
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
        targets.update({path: Links(body) for path, body in build.workflow_pages()})
        for href in Links(page['body']).hrefs:
            with self.subTest(href=href):
                link = urlsplit(href)
                self.assertFalse(link.scheme)
                path = link.path.lstrip('/') if link.path else page['path']
                if path.endswith('/'):
                    path += 'index.html'
                self.assertIn(path, targets)
                if link.fragment:
                    self.assertIn(unquote(link.fragment), targets[path].ids)

        installation = pages[1]['body']
        self.assertNotIn('id="how-the-loop-works"', installation)
        self.assertNotIn('href="#how-the-loop-works"', installation)
        self.assertIn('href="/docs/how-the-loop-works.html"', installation)
