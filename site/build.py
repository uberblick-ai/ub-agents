"""Build agents.uberblick.ai from site/content into site/out.

Standard library only, plus an installed ub-agents for the command help pages:

    pip install . && python3 site/build.py
"""
import html, os, posixpath, re, shutil, subprocess, sys, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTENT, OUT = ROOT / 'content', ROOT / 'out'
VERSION = re.search(r'^version = "([^"]+)"', (ROOT.parent / 'pyproject.toml').read_text(), re.M).group(1)
sys.path.insert(0, str(ROOT))
import tui  # noqa: E402
REPO = 'https://github.com/uberblick-ai/ub-agents'
SITE = 'https://agents.uberblick.ai'
TAGLINE = 'The loop engineering framework for GitHub. Any agent, no server.'
SECTIONS = [  # (title, content entry, kind)
    ('Installation', 'installation.md', 'page'),
    ('How the loop works', 'how-the-loop-works.md', 'page'),
    ('Configuration', 'configuration', 'dir'),
    ('Commands', 'commands', 'dir'),
    ('Guides', 'guides', 'dir'),
    ('Best practices', 'best-practices', 'dir'),
    ('Upgrading', 'upgrading.md', 'page'),
]

# ---------- Markdown (the subset these pages use) ----------

FLOW = re.compile(r'\{\{flow ([a-z0-9-]+)\}\}')
DISCUSS = re.compile(r'\{\{discuss ([a-z0-9-]+)\}\}')

def inline(text):
    codes = []
    def stash(m):
        codes.append('<code>' + html.escape(m.group(1)) + '</code>')
        return f'\x00{len(codes) - 1}\x00'
    def buttons(m):
        codes.append(discuss(m.group(1)))
        return f'\x00{len(codes) - 1}\x00'
    s = html.escape(DISCUSS.sub(buttons, re.sub(r'`([^`]+)`', stash, text)), quote=False)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    return re.sub(r'\x00(\d+)\x00', lambda m: codes[int(m.group(1))], s)

def discuss(name):
    """Buttons that open site/prompts/NAME.md in Claude Code or Codex."""
    q = urllib.parse.quote(' '.join((ROOT / 'prompts' / f'{name}.md').read_text().split()), safe='')
    icon = {cli: (ROOT / 'icons' / f'{cli}.svg').read_text().strip() for cli in ('claude', 'openai')}
    return (f'<span class="discuss">Discuss with:<a class="claude" href="claude://code/new?q={q}" title="Opens the Claude desktop app">{icon["claude"]}Claude</a>'
            f'<a href="codex://new?prompt={q}" title="Opens the Codex app">{icon["openai"]}Codex</a></span>')

def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', re.sub(r'<[^>]+>', '', text).lower()).strip('-')

def highlight(code, lang):
    lines = []
    for line in code.split('\n'):
        esc = html.escape(line, quote=False)
        if lang == 'yaml':
            m = re.match(r'^(\s*)(- )?([A-Za-z0-9_.-]+)(:)(.*)$', esc)
            if m:
                rest = m.group(5)
                c = rest.find(' #')
                if c >= 0:
                    rest = rest[:c] + '<span class="c">' + rest[c:] + '</span>'
                esc = f'{m.group(1)}{m.group(2) or ""}<span class="k">{m.group(3)}</span>:{rest}'
            elif esc.lstrip().startswith('#'):
                esc = f'<span class="c">{esc}</span>'
        elif lang in ('sh', 'text', 'python', 'markdown'):
            if lang == 'sh' and esc.lstrip().startswith('#'):
                esc = f'<span class="c">{esc}</span>'
            elif esc.startswith('$ '):
                esc = '<span class="p">$ </span>' + esc[2:]
            elif lang == 'sh' and esc and not esc.startswith(' '):
                esc = '<span class="p">$ </span>' + esc
        lines.append(esc)
    return '\n'.join(lines)

# Shell blocks get a copy button; COPY_JS copies the commands without their "$ " prompts.
COPY_BUTTON = ('<button class="copy-code" type="button" aria-label="Copy command" title="Copy">'
               '<svg class="i-copy" aria-hidden="true" viewBox="0 0 16 16"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/>'
               '<path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5"/></svg>'
               '<svg class="i-done" aria-hidden="true" viewBox="0 0 16 16"><path d="m3 8.5 3 3 7-7"/></svg></button>')

COPY_JS = '''<script>
document.addEventListener('click', function (e) {
  var btn = e.target.closest && e.target.closest('.copy-code'); if (!btn) return;
  var code = btn.parentNode.querySelector('code'), copy = code.cloneNode(true);
  copy.querySelectorAll('.p').forEach(function (p) { p.remove(); });
  function done() { btn.classList.add('done'); setTimeout(function () { btn.classList.remove('done'); }, 1600); }
  function select() { var r = document.createRange(); r.selectNodeContents(code); var s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  try { navigator.clipboard.writeText(copy.textContent).then(done, select); } catch (err) { select(); }
});
</script>'''

def markdown(src):
    lines = src.split('\n')
    out, i, title = [], 0, None
    while i < len(lines):
        line = lines[i]
        if line.startswith('```'):
            lang = line[3:].strip() or 'text'
            j = i + 1
            while not lines[j].startswith('```'):
                j += 1
            code = '\n'.join(lines[i + 1:j])
            pre = f'<pre class="lang-{lang}"><code>{highlight(code, lang)}</code></pre>'
            out.append(f'<div class="code">{pre}{COPY_BUTTON}</div>' if lang == 'sh' else pre)
            i = j + 1
        elif m := FLOW.fullmatch(line):
            svg = (ROOT / 'diagrams' / f'{m.group(1)}.svg').read_text()
            out.append(f'<figure class="flow">{svg}</figure>')
            i += 1
        elif m := re.match(r'^(#{1,3}) (.*)', line):
            level, text = len(m.group(1)), inline(m.group(2))
            if level == 1:
                title = m.group(2)
                out.append(f'<h1>{text}</h1>')
            else:
                hid = slug(text)
                out.append(f'<h{level} id="{hid}"><a class="anchor" href="#{hid}">{text}</a></h{level}>')
            i += 1
        elif line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                if not re.match(r'^\|[\s|:-]+\|$', lines[i]):
                    rows.append([c.strip() for c in lines[i].strip('|').split('|')])
                i += 1
            head = ''.join(f'<th>{inline(c)}</th>' for c in rows[0])
            body = ''.join('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>' for r in rows[1:])
            out.append(f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
        elif re.match(r'^(- |\d+\. )', line):
            ordered = not line.startswith('- ')
            items = []
            while i < len(lines) and re.match(r'^(- |\d+\. )', lines[i]):
                items.append(re.sub(r'^(- |\d+\. )', '', lines[i]))
                i += 1
            tag = 'ol' if ordered else 'ul'
            out.append(f'<{tag}>' + ''.join(f'<li>{inline(t)}</li>' for t in items) + f'</{tag}>')
        elif line.strip():
            para = []
            while (i < len(lines) and lines[i].strip() and not FLOW.fullmatch(lines[i])
                   and not re.match(r'^(```|#|\||- |\d+\. )', lines[i])):
                para.append(lines[i])
                i += 1
            out.append(f'<p>{inline(" ".join(para))}</p>')
        else:
            i += 1
    return title, '\n'.join(out)

# ---------- Pages ----------

def command_help(command=None):
    """The help text a user sees for COMMAND, from the installed ub-agents."""
    args = [sys.executable, '-m', 'ub_agents', 'help'] + ([command] if command else [])
    env = dict(os.environ, COLUMNS='100', NO_COLOR='1')
    result = subprocess.run(args, capture_output=True, text=True, check=True, env=env)
    return '$ ub-agents help' + (f' {command}' if command else '') + '\n' + result.stdout.rstrip()

def expand(text):
    return re.sub(r'\{\{help ?([a-z-]*)\}\}', lambda m: command_help(m.group(1) or None), text)

def collect():
    pages = []  # dicts: section, title, path (site path), src, html
    for name, entry, kind in SECTIONS:
        files = [CONTENT / entry] if kind == 'page' else sorted((CONTENT / entry).glob('*.md'))
        for f in files:
            stem = re.sub(r'^\d+-', '', f.stem)
            path = f'docs/{stem}.html' if kind == 'page' else f'docs/{entry}/{stem}.html'
            title, body = markdown(expand(f.read_text()))
            pages.append({'section': name, 'title': title, 'path': path, 'body': body,
                          'nav': 'Overview' if stem == 'index' and entry == 'configuration' else
                                 ('View all commands' if stem == 'index' else title)})
    return pages

def rel(target, here):
    """Turn a site path ('/docs/x.html' or 'docs/x.html') into a link relative to `here`."""
    target = target.lstrip('/')
    return posixpath.relpath(target, posixpath.dirname(here) or '.')

def fix_links(body, here):
    return re.sub(r'href="/([^"]+)"', lambda m: f'href="{rel(m.group(1), here)}"', body)

def header(here):
    return f'''<header class="top">
  <span class="brand"><a class="parent" href="https://next.uberblick.ai">uberblick</a><span class="sep">/</span><a class="mark" href="{rel('index.html', here)}">ub-agents</a></span>
  <a class="ver" href="{REPO}/releases">v{VERSION}</a>
  <nav>
    <a href="{rel('docs/installation.html', here)}">Docs</a>
    <a href="{REPO}/releases">Releases</a>
    <a href="{REPO}">GitHub</a>
  </nav>
</header>'''

FOOTER = f'''<footer class="foot">
  <span>MIT license</span>
  <a href="{REPO}">Source</a>
  <a href="{REPO}/releases">Releases</a>
  <a href="https://github.com/uberblick-ai/homebrew-tap">Homebrew tap</a>
</footer>'''

def sidebar(pages, current):
    out = []
    for name, entry, kind in SECTIONS:
        members = [p for p in pages if p['section'] == name]
        first = members[0]
        here = current['section'] == name
        if kind == 'page':
            on = ' class="on"' if current is first else ''
            out.append(f'<li><a{on} href="{rel(first["path"], current["path"])}">{name}</a></li>')
            continue
        cls = ' class="sec on"' if here else ' class="sec"'
        sub = ''
        if here:
            sub = '<ul>' + ''.join(
                '<li><a' + (' class="on"' if p is current else '')
                + f' href="{rel(p["path"], current["path"])}">{html.escape(p["nav"])}</a></li>'
                for p in members) + '</ul>'
        out.append(f'<li><a{cls} href="{rel(first["path"], current["path"])}">{name}</a>{sub}</li>')
    return '<ul class="side-list">' + ''.join(out) + '</ul>'

def pager(pages, i):
    here = pages[i]['path']
    h = ''
    if i > 0:
        p = pages[i - 1]
        h += f'<a class="prev" href="{rel(p["path"], here)}"><span>Previous</span>{html.escape(p["nav"] if p["section"] in ("Configuration", "Commands") else p["title"])}</a>'
    if i + 1 < len(pages):
        p = pages[i + 1]
        h += f'<a class="next" href="{rel(p["path"], here)}"><span>Next</span>{html.escape(p["nav"] if p["section"] in ("Configuration", "Commands") else p["title"])}</a>'
    return f'<nav class="pager">{h}</nav>'

HEAD = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<link rel="icon" href="{favicon}" sizes="32x32">
<link rel="icon" href="{svg_icon}" type="image/svg+xml">
<link rel="icon" href="{icon}" type="image/png" sizes="512x512">
<link rel="apple-touch-icon" href="{touch_icon}">
<link rel="preload" href="{fonts}/ibm-plex-mono-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{fonts}/ibm-plex-mono-600.woff2" as="font" type="font/woff2" crossorigin>
<style>{css}</style>
</head>
<body>
'''

# Fonts never change in place: a changed font gets a new file name.
HEADERS = '''/assets/fonts/*
  Cache-Control: public, max-age=31536000, immutable
'''

def head(here, **fields):
    """The page head with site.css inlined, so the first paint needs no other request."""
    fonts = rel('assets/fonts', here)
    css = re.sub(r'url\(/assets/fonts', f'url({fonts}', (ROOT / 'site.css').read_text())
    icons = {k: rel(f, here) for k, f in
             (('favicon', 'favicon.ico'), ('svg_icon', 'favicon.svg'), ('icon', 'icon.png'),
              ('touch_icon', 'apple-touch-icon.png'))}
    return HEAD.format(fonts=fonts, css=css, **icons, **fields)

MENU_JS = '''<script>
(function () { var d = document.querySelector('details.side'); if (!d) return;
  var mq = window.matchMedia('(min-width: 860px)'); function sync() { if (mq.matches) d.open = true; }
  sync(); mq.addEventListener && mq.addEventListener('change', sync); })();
</script>'''

def doc_page(pages, i):
    p = pages[i]
    here = p['path']
    title = p['title'] + ' · ub-agents'
    section_label = '' if p['section'] in ('Installation', 'How the loop works', 'Upgrading') else f'<p class="crumb">{p["section"]}</p>'
    first = re.search(r'<p>(.*?)</p>', p['body'], re.S)
    description = re.sub(r'<[^>]+>', '', first.group(1)) if first else TAGLINE
    return (head(here, title=html.escape(title), description=html.escape(html.unescape(description)),
                 canonical=f'{SITE}/{here}') + header(here) + f'''
<div class="docs">
  <details class="side"><summary>{html.escape(p["section"])} · {html.escape(p["nav"])}</summary>
    <nav aria-label="Documentation">{sidebar(pages, p)}</nav>
  </details>
  <main class="doc">
    {section_label}
    {fix_links(p["body"], here)}
    {pager(pages, i)}
  </main>
</div>
''' + FOOTER + MENU_JS + (COPY_JS if 'copy-code' in p['body'] else '') + '\n</body>\n</html>\n')

def not_found():
    """404.html is served at any depth, so its links are root-absolute."""
    body = (head('index.html', title='Page not found · ub-agents', description=TAGLINE, canonical=f'{SITE}/')
            + header('index.html') + '''
<main class="wrap">
  <section>
    <h2>Page not found</h2>
    <p>This page does not exist. Start at the <a href="index.html">home page</a> or the <a href="docs/installation.html">installation guide</a>.</p>
  </section>
</main>
''' + FOOTER + '\n</body>\n</html>\n')
    body = re.sub(r'<link rel="canonical"[^>]*>\n', '', body)
    body = body.replace('url(assets/', 'url(/assets/')
    return re.sub(r'href="(?!https?:|/|#)', 'href="/', body)

def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / 'static', OUT)
    shutil.copytree(ROOT / 'fonts', OUT / 'assets' / 'fonts')
    (OUT / '_headers').write_text(HEADERS)
    pages = collect()
    for i, p in enumerate(pages):
        dest = OUT / p['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(doc_page(pages, i))
    landing = (ROOT / 'index.html').read_text()
    landing = landing.replace('{{header}}', header('index.html')).replace('{{footer}}', FOOTER)
    landing = landing.replace('{{tui}}', tui.render())
    top = head('index.html', title='ub-agents', description=TAGLINE, canonical=f'{SITE}/')
    if '--artifact' in sys.argv:  # the artifact host adds its own document skeleton
        landing = top[top.index('<title>'):top.index('</head>')] + landing
    else:
        landing = top + landing + '\n</body>\n</html>\n'
    (OUT / 'index.html').write_text(fix_links(landing, 'index.html'))
    (OUT / '404.html').write_text(not_found())
    urls = [f'{SITE}/'] + [f'{SITE}/{p["path"]}' for p in pages]
    (OUT / 'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + ''.join(f'  <url><loc>{u}</loc></url>\n' for u in urls) + '</urlset>\n')
    (OUT / 'robots.txt').write_text(f'User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n')
    print(f'{len(pages)} pages')

if __name__ == '__main__':
    main()
