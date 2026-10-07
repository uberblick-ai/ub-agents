"""Build agents.uberblick.ai from site/content into site/out.

Standard library only, plus an installed ub-agents for the command help pages:

    pip install . && python3 site/build.py
"""
import html, os, posixpath, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTENT, OUT = ROOT / 'content', ROOT / 'out'
VERSION = re.search(r'^version = "([^"]+)"', (ROOT.parent / 'pyproject.toml').read_text(), re.M).group(1)
sys.path.insert(0, str(ROOT))
import tui  # noqa: E402
REPO = 'https://github.com/uberblick-ai/ub-agents'
SECTIONS = [  # (title, content entry, kind)
    ('Installation', 'installation.md', 'page'),
    ('Configuration', 'configuration', 'dir'),
    ('Commands', 'commands', 'dir'),
    ('Guides', 'guides', 'dir'),
    ('Best practices', 'best-practices', 'dir'),
    ('Upgrading', 'upgrading.md', 'page'),
]

# ---------- Markdown (the subset these pages use) ----------

def inline(text):
    parts = re.split(r'(`[^`]+`)', text)
    out = []
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 1:
            out.append('<code>' + html.escape(part[1:-1]) + '</code>')
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
        out.append(s)
    return ''.join(out)

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
            out.append(f'<pre class="lang-{lang}"><code>{highlight(code, lang)}</code></pre>')
            i = j + 1
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
            while i < len(lines) and lines[i].strip() and not re.match(r'^(```|#|\||- |\d+\. )', lines[i]):
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
  <a class="mark" href="{rel('index.html', here)}">ub-agents</a>
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
                f'<li><a{" class=\"on\"" if p is current else ""} href="{rel(p["path"], current["path"])}">{html.escape(p["nav"])}</a></li>'
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
<meta name="robots" content="noindex">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<link rel="stylesheet" href="{css}">
</head>
<body>
'''

MENU_JS = '''<script>
(function () { var d = document.querySelector('details.side'); if (!d) return;
  var mq = window.matchMedia('(min-width: 860px)'); function sync() { if (mq.matches) d.open = true; }
  sync(); mq.addEventListener && mq.addEventListener('change', sync); })();
</script>'''

def doc_page(pages, i):
    p = pages[i]
    here = p['path']
    title = p['title'] + ' · ub-agents'
    section_label = '' if p['section'] in ('Installation', 'Upgrading') else f'<p class="crumb">{p["section"]}</p>'
    return (HEAD.format(title=html.escape(title), css=rel('assets/site.css', here)) + header(here) + f'''
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
''' + FOOTER + MENU_JS + '\n</body>\n</html>\n')

def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / 'assets').mkdir(parents=True)
    shutil.copy(ROOT / 'site.css', OUT / 'assets' / 'site.css')
    shutil.copy(ROOT / '_headers', OUT / '_headers')
    pages = collect()
    for i, p in enumerate(pages):
        dest = OUT / p['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(doc_page(pages, i))
    landing = (ROOT / 'index.html').read_text()
    landing = landing.replace('{{header}}', header('index.html')).replace('{{footer}}', FOOTER)
    landing = landing.replace('{{tui}}', tui.render())
    head = HEAD.format(title='ub-agents', css='assets/site.css')
    if '--artifact' in sys.argv:  # the artifact host adds its own document skeleton
        head = head[head.index('<title>'):head.index('</head>')]
        landing = head + landing
    else:
        landing = head + landing + '\n</body>\n</html>\n'
    (OUT / 'index.html').write_text(fix_links(landing, 'index.html'))
    print(f'{len(pages)} pages')

if __name__ == '__main__':
    main()
