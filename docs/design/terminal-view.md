# Terminal view: reference design

This is the target look for the terminal view that `ub-agents launch` opens. It
guides the polish issues (#159–#166). [terminal-view.md](../terminal-view.md)
describes what the view does today. When an issue lands, update both files.

The view stays read-only. It reads the launcher's local session snapshot and run logs
and makes no GitHub calls except `g` on the Issue tab. It has no workflow controls,
so there is no stop or retry key and no filter.

## Layout

Two panes at 110×32 and above: **Work** on the left (about 50 columns) and the
selected item on the right, with tabs. A one-line footer sits below both. The
focused pane has an accent border with its title set into the border.

```text
╭─ Work · pass complete ───────────────────────────╮
│ Running ─────────────────────────────────────  1 │
│ ⠹ issue #163 Compact timestamped Claude l… 04:12 │
│   implementer · this launcher · attempt 1        │
│ Needs attention ─────────────────────────────  2 │
│ ? issue #156 Drop old coordination … needs-human │
│ ✗ issue #126 Codex structured run-lo… failed 3/3 │
│ Eligible ────────────────────────  planned order │
│ ● issue #165 Render issue descriptions as … next │
│ ● PR    #170 Compact timestamped Claude … review │
│                                                  │
├─ Recent activity ─────────────────────── 4 today ┤   (lower half, always shown, dimmed)
│ ✓ PR    #167 Group the work list into se… merged │
│   integrator · 11:52 · squash-merged             │
│ ✓ PR    #167 Group the work list into … approved │
│   reviewer · 11:38                               │
│ ✓ issue #159 Group the work list int… handed off │
│   implementer · 11:20 · opened PR #167           │
│ ✗ issue #126 Codex structured run-log fo… failed │
│   implementer · 10:02 · timed out after 180m     │
╰──────────────────────────────────────────────────╯
```

## Work pane

The upper half holds the live sections, in this order, each with a header rule and a
count. Empty sections are hidden, except Running, which shows `Idle · nothing
eligible for this launcher`. The upper half scrolls on its own when it overflows.

| Section | Rows | Glyph | Right column |
| --- | --- | --- | --- |
| Running | at most one: this launcher's assignment (one run at a time for now) | `⠹` spinner, `■` stopping | elapsed time, `stopping` |
| Needs attention | blocked, parked or withdrawn plans; failed outcomes | `?` needs a person, `‖` approval, `✗` failed | short reason: `needs-human`, `approve input`, `failed 3/3` |
| Eligible | ready plans in planned order | `●` | `next`, or priority (`▲ high`) |

Dependency and milestone waits are not shown.

Every row says whether its number is an issue or a pull request: a fixed-width
`issue` or `PR` tag before `#N`, so the numbers line up. `issue` is dim and `PR` is in
the accent color. The item header on the right spells out `Issue #N` or `PR #N`.

The lower half is always **Recent activity**: today's outcomes, newest first, with
`N today` in its header. Its rows are dimmed; a selected one shows at full
brightness. It never collapses. Rows that do not fit are cut from the oldest end.

| Glyph | Meaning | Right column |
| --- | --- | --- |
| `✓` | accepted outcome | `merged`, `approved`, `handed off`, `prepared` |
| `✗` | failed run | `failed` |

Rows are two lines: glyph, `#N` (or `PR #N`), title shortened with `…`, right column;
then agent · owner or time · attempt or result. Eligible rows may use one line. A
row keeps its place and selection across polls. An item appears once in the live
sections.

The pane title shows the pass state: `Work · pass complete`, or a dim `pass partial`.
A stale snapshot dims the pane and shows `last seen HH:MM:SS`.

## Item pane

Tabs `1 Log  2 Issue  3 Runs`, then `│ Formatted  Raw u` on the Log tab. Each tab
starts with the item header: `Issue #N title` or `PR #N title` in bold, then
agent · runtime · attempt · PR in dim text.

### Log

```text
 1 Log   2 Issue   3 Runs  │  Formatted  Raw u
 Issue #163 Show Claude log entries as compact timestamped lines
 implementer · codex gpt-6.1-sol xhigh · attempt 1 · no PR yet
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 11:41:02 launcher claimed #163 · lease 30m
 11:41:03 launcher worktree .ub-agents/worktrees/78849e0b · start codex

 11:41:18 ▸ Read src/ub_agents/log_format.py
 11:41:40 Thinking blocks are rejected as unfamiliar; I'll project them…   (italic)
 11:41:41 · thinking                                                       (dim)
 11:42:11 ▸ Edit src/ub_agents/log_format.py +48 -12                       (+ green, - red)
 11:42:30 ▸ Bash .venv/bin/python -m unittest tests.test_log_format ✓ 41 passed
 11:44:02 ▸ Bash .venv/bin/python -m unittest discover
          ✗ exit 1 · FAIL test_thinking_marker (tests.test_log_format)     (red, under its call)
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 ⠹ Implementing · no outcome reported                         1 earlier run
```

- One line per entry: local `HH:MM:SS`, then the content. Launcher lines say
  `launcher` in the accent color.
- Tool calls: `▸ Tool main-argument`, such as a path, command or pattern. Edit and
  Write show `+added -removed`. A successful result folds into its call. A failure
  adds one red line under it.
- Assistant text is italic and keeps its line breaks. Thinking is a dim `· thinking`
  line. System task records fold into their tool call. The final runtime result is
  one line.
- A one-line status sits below the log: the step and whether an outcome was
  reported, with earlier runs on the right.
- Paused: a `⏸ PAUSED · N new ↓ · f follow` pill at the bottom right. The footer keys
  switch to the log keys. A replaced or truncated file shows one red notice line
  above the status.
- Raw mode (`u`) shows the full records. Byte ranges, eviction counts and render
  limits are only on the `p` raw-access screen.

### Issue

```text
 1 Log   2 Issue   3 Runs
 Issue #156 Drop old coordination record formats                 (bold)
 issue · needs-human · issue-preparer asked a question           (dim)
 ┃ Waiting for a team member: answer on GitHub, then remove needs-human.   (red callout, only for attention rows)

 Outcome                                                         (heading, accent color)
 Launchers read only the current record format. Older markers and branch
 names are ignored, as no backward compatibility is kept.        ("no backward compatibility" bold)

 Acceptance
 • Remove the readers for `ub-agent/…` markers and branches.     (inline code in yellow)
 • The changelog's Upgrading note says to stop all launchers first.
 ┌──────────────────────────────────────────────┐
 │ <!-- ub-agent:record v1 -->  ignored         │                (code block on a darker panel)
 └──────────────────────────────────────────────┘
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 Source: snapshot · 15s old · press g to load from GitHub        (dim)
```

The body renders as Markdown. Links and HTML show as text and are never opened.
Rows that need a person show a one-line callout saying what is needed. The
source and age line is last.

### Runs

A table of this session's runs for the item: time, result glyph, agent and summary,
outcome.

## Footer and states

```text
 ub-agents v0.1.11 · ↻ refreshed 4s ago · next poll 26s     ↑↓ select ⏎ open 1-3 tabs ? keys q close view ^C stop launcher

Paused log (pill at the bottom right of the log pane, footer keys switch to log keys):
                                                              ⏸ PAUSED · 37 new ↓ · f follow
 ub-agents v0.1.11 · ↻ refreshed 3s ago · next poll 27s     f follow h older u raw PgUp/PgDn scroll ? keys q close view

Stale snapshot (banner above both panes, work pane dimmed, footer warns):
 ⚠ Launcher snapshot is 2m 10s old. Showing the last known state; the launcher may be busy or stuck.
 ⚠ snapshot 2m old · ub-agents v0.1.11 · stopping            ↑↓ select 1-3 tabs ? keys q close view ^C stop launcher now
```

- Idle: Running shows the idle line, and the status reads `○ Idle · waiting for the next poll`.
- Stopping (SIGTERM): the running row shows `■ stopping`, Eligible reads `not claimed
  while stopping`, and the footer reads `stopping`.
- Below 110×32: the Work pane only, at full width. ⏎ opens the selected item's tabs
  full width and Esc returns. The footer shortens to `v0.1.11 · ↻ 4s · poll 26s`.
- `?` lists every key. The footer shows only the main ones.

## Colors

```text
Colors, as Textual theme variables with these dark-theme values:
  background #0d1016 · panel/footer #161a22 · text #d4d9e1 · dim #6b7484
  accent (focus border, pane titles, launcher lines, PR tag) #b79cff · selection row #1b2030
  Running #6cb6ff · Needs attention #ff8b7f · Eligible #7ee2a0
  diff + #7ee2a0 · diff - #ff8b7f
Focused pane: accent border with its title in the border ("Work", "Log", "Issue"); unfocused: #2a303b border.
Tab bar: active tab inverted (dark text on light), others dim; "Formatted" underlined in accent when on.
```
