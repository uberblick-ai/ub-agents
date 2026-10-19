"""The terminal view snapshot on the landing page, drawn as HTML from an example session."""
import html
import re

W_L, W_R = 46, 66  # outer widths incl. borders
CL, CR = W_L - 6, W_R - 6  # content widths (border + 2 padding each side)

# The site's fonts carry no box-drawing glyphs, and fallback fonts differ in
# width and height. Lines and corners are drawn with CSS, one cell per column,
# and every other non-ASCII glyph is held to one column.
LINES = {'─': 'h', '┄': 'd'}
CELLS = {'│': 'v', '╭': 'corner tl', '╮': 'corner tr', '╰': 'corner bl', '╯': 'corner br'}

def seg(t, c=None): return (t, c)
def length(segs): return sum(len(t) for t, _ in segs)

def glyphs(t):
    out = []
    for run in re.findall(r'─+|┄+|.', t, re.S):
        ch = run[0]
        if ch in LINES:
            out.append(f'<i class="{LINES[ch]}" style="width:{len(run)}ch"></i>')
        elif ch in CELLS:
            out.append(f'<i class="{CELLS[ch]}"></i>')
        elif ord(ch) > 127 and ch not in '·…':
            out.append(f'<i class="g">{ch}</i>')
        else:
            out.append(html.escape(ch))
    return ''.join(out)

def spans(segs): return ''.join(glyphs(t) if not c else f'<span class="{c}">{glyphs(t)}</span>' for t, c in segs)

def fit(text, width):
    return text if len(text) <= width else text[:width-1] + '…'

def row(glyph, gcls, ref, title, right, rcls, width=CL, dim=False):
    head = f'{glyph} '
    refs = [seg(ref[0], 'acc') , seg(ref[1:] + ' ')] if ref.startswith('⌥') else [seg(ref + ' ')]
    room = width - len(head) - len(ref) - 1 - len(right) - 1
    t = fit(title, room).ljust(room)
    s = [seg(glyph, gcls), seg(' ')] + refs + [seg(t + ' '), seg(right, rcls)]
    if dim: s = [(x, (c + ' dim') if c else 'dim') for x, c in s]
    return s

def sub(text, width=CL): return [seg(fit('  ' + text, width), 'dim')]
def heading(label, cls, width=CL):
    return [seg(label, cls), seg(' ' + '┄' * (width - len(label) - 1), 'rule')]
def blank(): return []
def selected(segs): return ('sel', segs)


def render():
    left = [
      blank(),
      heading('Running · 1', 'blue'),
      row('⠹', 'blue', '#248', 'Refresh the queue while an agent runs', '04:12', None),
      sub('implementer · this launcher · attempt 1'),
      blank(),
      heading('Needs attention · 1', 'red'),
      selected(row('?', 'red', '#255', 'Keep run scratch outside the checkout', '19h', 'red')),
      selected(sub('implementer · needs-human · decide where scratch lives')),
      blank(),
      heading('Eligible · 2', 'green'),
      row('●', 'green', '⌥252', 'Drop inherited agent variables in tests', 'next', 'green'),
      sub('reviewer'),
      blank(),
      row('●', 'green', '#191', 'Shorter ub-agents doctor output', 'ready', None),
      sub('implementer · 1/5 failures'),
      blank(),
      heading('Recent activity · 4 today', 'dim'),
      row('✓', 'green', '⌥269', 'Send fixable check failures back to the implementer', 'merged', None, dim=True),
      sub('integrator · 07:41 · squash-merged'),
      row('✓', 'green', '⌥269', 'Send fixable check failures back to the implementer', 'approved', None, dim=True),
      sub('reviewer · 07:22'),
      row('✓', 'green', '⌥267', 'Release 0.1.13', 'merged', None, dim=True),
      sub('integrator · 07:05 · squash-merged'),
      row('✓', 'green', '⌥264', 'Open selected GitHub item from its header reference', 'merged', None, dim=True),
      sub('integrator · 06:48 · squash-merged'),
    ]

    right = [
      blank(),
      [seg(' 1 Agent  2 Issue  3 Runs '), seg(' 4 Unblock ', 'tab'), seg(' '), seg('│ ', 'dim'),
       seg('Formatted', 'acc u'), seg('  Raw', 'dim')],
      [seg('┄' * CR, 'rule')],
      [seg('#255 Keep run scratch outside the checkout', 'b')],
      [seg('implementer · needs-human · waiting 19h · since 12:52', 'dim')],
      [seg('┄' * CR, 'rule')],
      [seg('Run scratch sits inside the checkout, so an agent can')],
      [seg('commit stray files. Where should it live?')],
      blank(),
      [seg('To unblock, do one of:')],
      [seg('  1. Maintainer: move scratch to the state directory')],
      [seg('     (recommended)', 'dim')],
      [seg('  2. Maintainer: keep it in the worktree and ignore it')],
      blank(),
      [seg('▸ ', 'acc'), seg('To resume implementer')],
      [seg('▸ ', 'acc'), seg('Reasoning and evidence')],
    ]
    source = [[seg('┄' * CR, 'rule')],
              [seg('Source: action-needed comment · 12:52 · snapshot', 'dim')]]
    right += [blank()] * (len(left) - len(right) - len(source)) + source
    assert len(left) == len(right), (len(left), len(right))

    def top(title, width, focus):
        t = f'─ {title} '
        tail = "─" * (width - 2 - len(t))
        if focus:
            return f'<span class="acc">{glyphs("╭" + t + tail + "╮")}</span>'
        # The title is text, so it gets readable contrast; the frame stays dark.
        return (f'<span class="rule">{glyphs("╭─ ")}</span><span class="dim">{html.escape(title)}</span>'
                f'<span class="rule">{glyphs(" " + tail + "╮")}</span>')
    def bottom(width, focus):
        c = 'acc' if focus else 'rule'
        return f'<span class="{c}">{glyphs("╰" + "─" * (width - 2) + "╯")}</span>'
    def line(segs, width, focus):
        c = 'acc' if focus else 'rule'
        sel = isinstance(segs, tuple)
        if sel: segs = segs[1]
        L = length(segs)
        assert L <= width - 6, (L, segs)
        body = f'{spans(segs)}{" " * (width - 6 - L)}'
        if sel: body = f'<span class="sel">{body}</span>'
        edge = f'<span class="{c}">{glyphs("│")}</span>'
        return f'{edge}  {body}  {edge}'

    out = [top('Work · pass complete', W_L, True) + ' ' + top('Unblock', W_R, False)]
    for a, b in zip(left, right):
        out.append(line(a, W_L, True) + ' ' + line(b, W_R, False))
    out.append(bottom(W_L, True) + ' ' + bottom(W_R, False))
    total = W_L + 1 + W_R
    lft = 'uberblick-ai/ub-agents · running assignment'
    rgt = '1-4 tabs ? keys q quit'
    out.append(f'<span class="dim">{glyphs(lft)}{" " * (total - len(lft) - len(rgt))}{glyphs(rgt)}</span>')
    return '\n'.join(out)
