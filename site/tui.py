"""The terminal view snapshot on the landing page, drawn as HTML from an example session."""
import html

W_L, W_R = 46, 66  # outer widths incl. borders
CL, CR = W_L - 6, W_R - 6  # content widths (border + 2 padding each side)

def seg(t, c=None): return (t, c)
def length(segs): return sum(len(t) for t, _ in segs)
def esc(t):
    return ''.join(f'<i class="g">{ch}</i>' if ord(ch) > 127 and not (0x2500 <= ord(ch) <= 0x257F) and ch not in '·…' else html.escape(ch) for ch in t)
def spans(segs): return ''.join(esc(t) if not c else f'<span class="{c}">{esc(t)}</span>' for t, c in segs)

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


def render():
    left = [
      blank(),
      heading('Running · 1', 'blue'),
      row('⠹', 'blue', '#248', 'Refresh the queue while an agent runs', '04:12', None),
      sub('implementer · this launcher · attempt 1'),
      blank(),
      heading('Needs attention · 1', 'red'),
      row('?', 'red', '#255', 'Keep run scratch outside the checkout', '19h', 'red'),
      sub('implementer · needs-human · decide where scratch lives'),
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
      [seg(' 1 Log ', 'tab'), seg(' 2 Issue  3 Runs '), seg('│ ', 'dim'), seg('Formatted', 'acc u'), seg('  Raw', 'dim')],
      [seg('┄' * CR, 'rule')],
      [seg('#248 Refresh the queue while an agent runs', 'b')],
      sub('implementer · claude · attempt 1', CR + 2)[0:1] and [seg('implementer · claude · attempt 1', 'dim')],
      [seg('┄' * CR, 'rule')],
      [seg('07:48:02 ', 'dim'), seg('launcher claimed #248 · lease 30m')],
      [seg('07:48:03 ', 'dim'), seg('launcher worktree ready · start claude')],
      blank(),
      [seg('07:48:19 ', 'dim'), seg('▸ ', 'acc'), seg('Read src/ub_agents/view_work.py')],
      [seg('07:48:31 ', 'dim'), seg('▸ ', 'acc'), seg('Read src/ub_agents/polling.py')],
      [seg('07:48:52 ', 'dim'), seg(fit("The queue only refreshes between runs. I'll poll read-only while the agent works and claim nothing until it is idle.", CR - 9))],
      [seg('07:49:40 ', 'dim'), seg('▸ ', 'acc'), seg('Edit src/ub_agents/view_work.py '), seg('+48', 'green'), seg(' '), seg('-12', 'red')],
      [seg('07:50:12 ', 'dim'), seg('▸ ', 'acc'), seg('Edit tests/test_view_ui.py '), seg('+31', 'green')],
      [seg('07:51:30 ', 'dim'), seg('▸ ', 'acc'), seg('Bash python -m tests '), seg('✓ passed', 'green')],
      [seg('07:51:44 ', 'dim'), seg('▸ ', 'acc'), seg('Bash git push -u origin ub-agents/248-implementer')],
      blank(), blank(), blank(), blank(), blank(), blank(), blank(),
      [seg('┄' * CR, 'rule')],
      [seg('⠹ ', 'blue'), seg('implementer running · following', 'dim')],
    ]
    n = max(len(left), len(right))
    left += [blank()] * (n - len(left)); right += [blank()] * (n - len(right))

    def top(title, width, focus):
        c = 'acc' if focus else 'rule'
        t = f'─ {title} '
        return f'<span class="{c}">╭{html.escape(t)}{"─" * (width - 2 - len(t))}╮</span>'
    def bottom(width, focus):
        c = 'acc' if focus else 'rule'
        return f'<span class="{c}">╰{"─" * (width - 2)}╯</span>'
    def line(segs, width, focus):
        c = 'acc' if focus else 'rule'
        L = length(segs)
        assert L <= width - 6, (L, segs)
        return f'<span class="{c}">│</span>  {spans(segs)}{" " * (width - 6 - L)}  <span class="{c}">│</span>'

    out = [top('Work · pass complete', W_L, True) + ' ' + top('Log', W_R, False)]
    for a, b in zip(left, right):
        out.append(line(a, W_L, True) + ' ' + line(b, W_R, False))
    out.append(bottom(W_L, True) + ' ' + bottom(W_R, False))
    total = W_L + 1 + W_R
    lft = 'ub-agents · running assignment'
    rgt = '↑↓ select ⏎ open 1-3 tabs ? keys q quit'
    out.append(f'<span class="dim">{esc(lft)}{" " * (total - len(lft) - len(rgt))}{esc(rgt)}</span>')
    return '\n'.join(out)

