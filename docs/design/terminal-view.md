# Terminal view: reference design

This is the target look for the terminal view that `ub-agents launch` opens. It
guides the polish issues (#159–#166). [terminal-view.md](../terminal-view.md)
describes what the view does today. When an issue lands, update both files.

The view stays read-only. It reads the launcher's local session snapshot and run logs
and makes no GitHub calls except opening Issue without a cached description,
`g` on the Issue and Unblock tabs, or activating Unblock with a missing snapshot
notice and no cached load result. It has no workflow controls,
so there is no retry key and no filter. `q` stops the launch after the current run;
Ctrl-C stops it immediately. The view stays open through launcher cleanup (#287).

## Layout

Two panes at 110×32 and above: **Work** on the left and the selected item on the
right, with tabs. Work's outer width, including its border, is one third of the
terminal width, rounded down and clamped to 46–64 columns. A one-column gap
separates the borders, and the item pane takes the remaining width. The width
follows terminal resizes: 110 columns gives Work 46 columns, 150 gives 50, and
200 gives 64. A one-line footer sits below both. The focused
pane has an accent border with its title set into the border.

Both panes have two columns of padding inside each side border and one blank row
under the top border. Work section rules and Recent activity share this inset.
The item pane applies it to the tab bar, Formatted/Raw indicator, tab rule, shared
header, every tab's content and log status lines; the indicator follows the labels
on the same row. Right-aligned columns and `…` truncation stay inside the padding.
Below 110×32 the single full-width pane keeps the same padding, with no gap.

Every scrollable area (live Work, Recent activity, Log output, Issue, Runs, Unblock
and the `p`/`?` overlays) hides its scrollbar at rest (`scrollbar-visibility: hidden`). An
overflowing area reserves one column for its vertical bar; showing or hiding it
never moves or rewraps content. Log output keeps `scrollbar-gutter: stable`.
The bar uses the muted theme color (`$view-muted`), including during hover and
dragging. Wheel input, scroll keys and cursor movement that scrolls the area
show only that area's bar. It hides 1.5 seconds after the last scroll, remaining
visible while hovered or dragged; leaving the bar or ending a drag starts the
countdown once neither interaction holds it. Follow-mode log appends, item/tab
switches, snapshot refreshes, saved-position restores and resizing do not show a bar.

```text
╭─ Work · pass complete ─────────────────────╮ ╭─ Log ───────────────────────────────────────────────────────╮
│                                            │ │                                                             │
│  Running · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   1 Log  2 Issue  3 Runs │ Formatted  Raw                   │
│  ⠹ #163 Compact timestamped Claude… 04:12  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│    implementer · this launcher · attempt…  │ │  #163 Show Claude log entries as compact timestamped lines  │
│  Needs attention · 2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │  implementer · codex · attempt 1                            │
│  ? #156 Drop old coordination record… 19h  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│    integrator · needs-human · maintainer…  │ │  11:41:02 launcher claimed #163 · lease 30m                 │
│  ✗ #126 Codex structured run-log for… 24m  │ │  11:41:03 launcher worktree ready · start codex             │
│    implementer · failed 3/3 · runtime ex…  │ │                                                             │
│  Eligible · 2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │  11:41:18 ▸ Read src/ub_agents/log_format.py                │
│  ● #165 Render issue descriptions a… next  │ │  11:41:40 Thinking blocks are rejected as unfamiliar; I’l…  │
│    implementer                             │ │  11:41:41 · thinking                                        │
│  ● ⌥170 Compact timestamped Claude… ready  │ │  11:42:11 ▸ Edit src/ub_agents/log_format.py +48 -12        │
│    reviewer                                │ │  11:42:30 ▸ Bash unit suite ✓ 41 passed                     │
│                                            │ │                                                             │
│  Recent activity · 4 today ┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │                                                             │
│  ✓ ⌥167 Group the work list into … merged  │ │                                                             │
│    integrator · 11:52 · squash-merged      │ │                                                             │
│  ✓ ⌥167 Group the work list int… approved  │ │                                                             │
│    reviewer · 11:38                        │ │                                                             │
│  ✓ #159 Group the work list i… handed off  │ │                                                             │
│    implementer · 11:20 · opened ⌥167       │ │                                                             │
│  ✗ #126 Codex structured run-log … failed  │ │                                                             │
│    implementer · 10:02 · timed out after…  │ │                                                             │
│                                            │ │                                                             │
│                                            │ │                                                             │
│                                            │ │                                                             │
│                                            │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│                                            │ │  ⠹ implementer running · no outcome reported 1 earlier run  │
╰────────────────────────────────────────────╯ ╰─────────────────────────────────────────────────────────────╯
```

## Work pane

The upper half holds Running, Needs attention and Eligible, in that
order, each with its current row count. Running always appears with only this
launcher's assignment and a count of 0 or 1. With no assignment it shows a dim
`Idle · nothing eligible for this launcher` placeholder, which has no item content.
Other launchers' claims are omitted; their runs remain in an item's Runs tab.
Parked dependency and milestone waits are omitted from rows and section counts.
Eligible merges plans for each item after ordering ready/recovery plans first,
then retry backoff and paused-runtime plans, preserving planned order within
each subgroup. Each item keeps its first plan's position, glyph and right column;
the count counts items. Selection stays on the item while it remains eligible,
including when its agents change. Running and Needs attention stay per agent.
Other empty sections are hidden. It scrolls
on its own when it overflows; row changes do not alter the pane width (#160).
The canvas above is illustrative; the Work pane's outer width follows the Layout
rule: one third of the terminal width, rounded down and clamped to 46–64 columns
including the border, at 110×32 and above.

Every live row has two compact lines. Line 1 contains the glyph, item reference,
title shortened with `…`, and a right-aligned waiting time or state:

| Row | Glyph | Right column |
| --- | --- | --- |
| This launcher's assignment | `⠹` spinner, `■` stopping | Elapsed claim time (`04:12`, `1:04:12`), `claiming` before the claim is known, `stopping` |
| Needs attention, parked | `?` | Waiting time in red (`24m`, `19h`, `3d`) |
| Needs attention, blocked | `!` | Waiting time in red |
| Needs attention, attempt limit reached | `✗` | Waiting time in red |
| Eligible, ready or recovery | `●` | `next` on the first ready/recovery row, otherwise `ready` or `recover` |
| Eligible, delayed | `◷` | `backoff`, or `waiting` for paused runtimes; never `next` |

Needs attention's dim line 2 is `agent · state · reason`. Parked state names the
stop label(s), joined by `, `; the other states are `blocked` and `failed F/M`.
The reason is the parking run or notice summary without the `Stop label … is
present` prefix or retry instructions, and is omitted if no summary is known.
Long lines end in `…`, with the full reason on Issue.

The launcher puts a waiting start time on every Needs attention snapshot row.
It starts at the newest action-needed comment from an already verified trusted
launcher account that parked the item, whichever launcher posted it, since the
last claim or reset. Without one, it starts at the finalized outcome that added a
currently present stop label. Blocked or failed rows with neither start at the
item's latest finished run's release time. Unknown times leave the right column
blank. Waiting updates while the view is open, rounding down: `0m`–`59m`, then
`1h`–`47h`, then days starting at `2d`. The launcher uses comments and author
roles already read in the pass; the launcher and view add no GitHub reads.
Narrow one-line Work rows keep the glyph, reference, title and waiting time.

Merged Eligible rows' line 2 lists agents comma-separated in that order, with each
failure count after its agent: `reviewer 1/3 failures, integrator`. A single-agent
row keeps `agent · F/M failures`. Issue and Runs remain per item, and running
agents drop out of Eligible as before.

Other live rows' line 2 is indented and contains the agent, `this launcher` for the assignment,
and count, joined with ` · ` and omitting missing parts.
The count is `attempt N` for the assignment, or `F/M failures`
for a plan with at least one failure. Long second lines end in `…`; neither line
wraps at 110×32. The separate reason leaf is removed, and the full reason stays
on the Issue tab. Arrow keys move one row at a time; either line selects the same
row with the mouse. Selection, focus, section counts, order across polls and
navigation into and out of Recent activity remain as before.

The assignment's elapsed time comes from the item's cached run history and
updates while the view is open. This adds no GitHub reads or snapshot fields.
Priority markers are outside this design because the snapshot has no priority.

Numbers say what they are: `#N` is an issue and `⌥N` is a pull request, in rows,
item headers and dim detail lines alike. `⌥` is in the accent color.

The lower half is always **Recent activity**: up to 20 cached session outcomes,
including older outcomes, newest first, with
`N today` in its header. Its rows are dimmed; a selected one shows at full
brightness. It never collapses. It scrolls vertically independently of the live
sections above it, so wheel input, arrows and clicks can reach all retained rows.
Both halves keep their sizes. A line below the header says `N older outcomes not
retained` when the snapshot counts evicted outcomes; rows outside the viewport
remain reachable by scrolling, while evicted outcomes cannot be recovered.
The header rule and notice stay fixed above the scrolling rows, so the section
boundary and retention count remain visible at the oldest retained outcome.

| Glyph | Meaning | Right column |
| --- | --- | --- |
| `✓` | accepted outcome | `merged`, `approved`, `handed off`, `prepared` |
| `✗` | failed run | `failed` |

Recent activity rows keep their two-line layout: glyph, item reference, shortened
title and result; then agent · time · summary. A row keeps its place and selection
across polls. New outcomes leave the visible older rows in place when scrolled
away from the top; at the top the newest row appears first. Resize keeps the
selected retained outcome visible without showing its scrollbar. Recent activity
uses the same reserved-column, user-scroll-only scrollbar convention as live
Work. An item appears once in the live sections.

The rounded pane border's title shows the pass state: `Work · pass complete` or
`Work · pass partial`, followed by any omitted count. The tree has no separate
root line. Work section headings and Recent activity use thin dashed rules;
there are no interior pane boxes. The focus border and its title use the accent
color; the other border and title are dim.

## Item pane

The current tabs read `1 Log  2 Issue  3 Runs │ Formatted  Raw`. Formatted/Raw is
an inert indicator of the selected log's `u` mode, visible on every tab. The active
tab is inverted with one space on either side of its label, the others are dim,
and a thin dashed rule replaces Textual's underline. The tab bar starts at the
pane's two-column inset; labels sit one column right of the shared header.
The pane's rounded border is titled `Log`, `Issue` or `Runs`.
Unblock appears only for Needs attention rows. Each tab
starts with the same item header: `#N title` (issue) or `⌥N title` (PR) in bold,
then agent · runtime · attempt · PR in dim text and a dashed rule. The header's
number, including `#` or `⌥`, is an underlined link that opens the item on GitHub.
It keeps the title color (with `⌥` in accent) at rest and turns link blue on hover;
the rest of the title stays unchanged. Without a GitHub URL, it has no link styling.
Missing values are omitted. The running assignment shows `attempt N`; planned work shows
`F/M failures`. A session outcome's linked PR shows as `⌥N` (#162).

### Log

```text
╭─ Log ────────────────────────────────────────────────────────────────────────────╮
│                                                                                  │
│   1 Log  2 Issue  3 Runs │ Formatted  Raw                                        │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  #163 Show Claude log entries as compact timestamped lines                       │
│  implementer · codex gpt-6.1-sol xhigh · attempt 1                               │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  11:41:02 launcher claimed #163 · lease 30m                                      │
│  11:41:03 launcher worktree .ub-agents/worktrees/78849e0b · start codex          │
│                                                                                  │
│  11:41:18 ▸ Read src/ub_agents/log_format.py                                     │
│  11:41:40 Thinking blocks are rejected as unfamiliar; I'll project them…         │
│  11:41:41 · thinking                                                             │
│  11:42:11 ▸ Edit src/ub_agents/log_format.py +48 -12                             │
│  11:42:30 ▸ Bash .venv/bin/python -m unittest tests.test_log_format ✓ 41 passed  │
│  11:44:02 ▸ Bash .venv/bin/python -m unittest discover                           │
│           ✗ exit 1 · FAIL test_thinking_marker (tests.test_log_format)           │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  ⠹ implementer running · no outcome reported                      1 earlier run  │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

- One line per entry: local `HH:MM:SS`, then the content. Launcher lines say
  `launcher` in the accent color.
- Tool calls: `▸ Tool main-argument`, such as a path, command or pattern. Edit and
  Write show `+added -removed`. A successful result folds into its call. A failure
  adds one red line under it.
- Assistant text is italic and keeps its line breaks. Thinking is a dim `· thinking`
  line. System task records fold into their tool call. The final runtime result is
  one line.
- A one-line status sits below the log: the agent and process or plan state,
  then `no outcome reported` or the reported result and acceptance. A spinner
  appears only while running; other session outcomes for the item are counted
  as earlier runs on the right, omitted when zero (#162).
- Paused: a `⏸ PAUSED · N new ↓ · f follow` pill at the bottom right. The footer keys
  switch to the log keys. Exceptional log notices, including replaced or
  truncated files, use one highlighted line above the transcript (#162).
- Raw mode (`u`) shows the full records. Byte ranges, eviction counts and render
  limits are only on the `p` raw-access screen.

### Issue

```text
╭─ Issue ─────────────────────────────────────────────────────────────────────────╮
│                                                                                 │
│   1 Log  2 Issue  3 Runs │ Formatted  Raw                                       │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  #156 Drop old coordination record formats                                      │
│  issue · ready · prepared by issue-preparer                                     │
│                                                                                 │
│  Outcome                                                                        │
│  Launchers read only the current record format. Older markers and branch        │
│  names are ignored, as no backward compatibility is kept.                       │
│                                                                                 │
│  Acceptance                                                                     │
│  • Remove the readers for `ub-agent/…` markers and branches.                    │
│  • The changelog's Upgrading note says to stop all launchers first.             │
│  ┌──────────────────────────────────────────────┐                               │
│  │ <!-- ub-agent:record v1 -->  ignored         │                               │
│  └──────────────────────────────────────────────┘                               │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  Source: GitHub · 15s old                                                        │
╰─────────────────────────────────────────────────────────────────────────────────╯
```

The body renders as Markdown. Links and HTML show as text and are never opened.
The source and age line is last.
Plan snapshots contain no description text. Opening Issue with `2`, a tab click,
or a selection change while Issue is active loads missing title/body through the
view's existing GitHub cache and shows `Loading #N…`. Cached reads and run context
need no repeat request; a cached failure requires `g` to retry. Bodies retain the
2,048-character limit and separate shortening notice.

### Unblock

Only for items in Needs attention. It shows the action-needed comment that parked
the item, so a team member sees at once what has to happen. Acting on it from the
view comes later; for now the tab is read-only.

```text
╭─ Unblock ────────────────────────────────────────────────────────────────────────╮
│                                                                                  │
│   1 Log  2 Issue  3 Runs  4 Unblock │ Formatted  Raw                             │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  ⌥168 Render Issue descriptions as Markdown                                      │
│  integrator · blocked · waiting 24m · since 12:12                                │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  Local CI is red on ea4068a: one test reads the launcher environment.            │
│                                                                                  │
│  To unblock, do one of:                                                           │
│    1. Maintainer: run CI outside supervision (recommended)                        │
│       ┌──────────────────────────────────────────────────────────────────────┐   │
│       │ mise run ci ea4068a…                                                  │   │
│       └──────────────────────────────────────────────────────────────────────┘   │
│    2. Maintainer: isolate `UB_AGENTS_RUN_CONFIG` in tests.                         │
│                                                                                  │
│  Merging or closing #168 finishes this item; nothing else is needed.              │
│                                                                                  │
│  ▸ To send it back to integrator instead                                          │
│  ▸ Reasoning and evidence                                                        │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  Source: action-needed comment · 12:12 · snapshot                                │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

The body is the latest `<!-- ub-agents:action-needed RUN -->` comment on the item,
rendered as Markdown like the Issue tab, without its marker and Action needed
title. New notices keep Claim/Outcome links with the folded evidence; earlier
formats omit their standalone links line. The header gives the agent, the state
and how long it has waited. For an
item whose trusted notice the launcher already observed, the text comes from the
snapshot. Activating Unblock with `4` or a tab click loads a missing or omitted
snapshot notice from GitHub when no result is cached; `g` loads or retries it.
Successful and failed results share the existing in-memory cache, so reopening
Unblock does not repeat the read. A cooldown or another pending read queues
nothing; a later activation can try again if no result was cached. Snapshot
notices excluded by trust checks do not auto-load. Selection changes, redraws
and timers start no reads, including when Unblock stays active.
Its `waiting … · since HH:MM`
uses the same published start time and minute/hour/day format as the Work row,
even after a GitHub load. Unknown times are omitted.

To fit the 64 KiB snapshot, notices outside Needs attention are omitted first,
then surplus history runs, older outcomes and plans outside the visible Work
sections are trimmed. Running, Needs attention and the first ten Eligible items
in claim order are kept, with Eligible's full count. Needs attention notices are
omitted next; visible display text is shortened further before visible plans can
be omitted if the remaining metadata still cannot fit.
Omitted notices retain an `omitted` marker without text in the published copy;
the launcher's retained state is unchanged. Their status reads "Comment left out
of the snapshot to save space; loading from GitHub…" while their read is pending,
or "Comment left out of the snapshot to save space; press g to load from GitHub."
when no read is pending. Loads share the single read slot, cooldown and launcher
author verification checks used by `g`.

New notices show the reason first: the first action without options, or the
summary's first sentence with options, normalized to one line and cut at 300
characters with `…`. Independent asks follow, then "To unblock, do one of:" with
numbered options and "(recommended)" on the first. An option's trailing
single-backtick command after a colon and space renders in its own code block;
other single-backtick code stays inline and other ask Markdown is escaped.
PR run-outcome notices show "Merging or closing #N finishes this item; nothing
else is needed." visibly. Their retry and trigger instructions, or stop-label
and trigger steps for outcomes adding a stop label, stay in a separate collapsed
"To send it back to AGENT instead" control. Issue notices and notices whose item
type could not be read keep visible "Then resume AGENT:" steps. Approval-gate
notices keep their visible resume steps. The summary, candidate, review, CI and
links stay in a collapsed "Reasoning and evidence" section. Without options,
asks retain their previous layout, followed by the same item-specific resume
structure. v0.1.13's "Reasoning, evidence and resume instructions" fold and earlier
prose comments still render as written.

When a blocked row has no available notice, Unblock shows its published reason.
For a PR it also shows the completion line and a collapsed "To send it back to
AGENT instead" control with the retry command and trigger and stop-label hints.
Issues and unknown item types keep the fallback resume steps visible. An
available trusted notice replaces the fallback.

### Runs

The item's history, oldest first: who filed the issue, then every run from any
launcher with when, result, agent and summary, where it ran, and the outcome. For a
PR the first row is the issue it closes.

```text
╭─ Runs ─────────────────────────────────────────────────────────────────────────────────╮
│                                                                                        │
│   1 Log  2 Issue  3 Runs │ Formatted  Raw                                              │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  ⌥167 Group the terminal view's work list into sections                                │
│  closes #159 · filed by bk-one · 3 runs                                                │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  when        result  agent · summary              where         outcome                │
│  2 days ago  ✓       filed by bk-one              GitHub        filed                  │
│  52 min ago  ✓       implementer · sections fro…  bens-macbook… handed-off             │
│  34 min ago  ✓       reviewer · changelog entry…  uberblick     approved · 4 denied    │
│  20 min ago  ✓       integrator · squash-merged…  this machine  merged                 │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

- `when` is relative (`just now`, `20 min ago`, `3 h ago`, `yesterday`, then a date).
- `where` is `this machine` for this host and the launcher's host name otherwise
  (dim), in a fixed 14-column slot: the domain part is dropped (`build-01.tail9c.ts.net`
  shows as `build-01`) and longer names are shortened with `…`.
- Result glyphs: `✓` green, `✗` red, spinner for a run in progress.
- Every run and filing row occupies one line at any pane width, including 60
  columns. Text too long for any cell is shortened with `…`, never wrapped.
- The outcome cell shows the outcome or status, then any `BLOCKED: …` human
  blockers, then ` · N denied` for a positive denial count. Outcome and blocker
  text shorten first to keep the count whole. Unaccepted successes show a spinner
  while their lease is live, then a red `✗` if it expires, is released or is withdrawn
  before acceptance. Rejected, blocked and retry results remain red.

## Footer and states

```text
 ub-agents v0.1.11 · next poll 26s     ↑↓ select ⏎ open 1-3 tabs r poll now ? keys q quit

After r:
 ub-agents v0.1.11 · polling          ↑↓ select ⏎ open 1-3 tabs r poll now ? keys q quit

r within the 10-second cooldown, or during a GitHub rate-limit wait:
 ub-agents v0.1.11 · next poll 26s · poll now available in 8s
 ub-agents v0.1.11 · rate limited until 18:02 · r unavailable

Paused log (pill at the bottom right of the log output, above the run status;
footer keys switch to log keys):
                                                              ⏸ PAUSED · 37 new ↓ · f follow
 ub-agents v0.1.11 · next poll 27s     f follow h older u raw PgUp/PgDn scroll r poll now ? keys q quit

Update available (themed banner above both panes):
 ⬆ ub-agents 0.1.12 is available · you run 0.1.11 · brew update && brew upgrade ub-agents, then restart the launcher   released 2 days ago
 ⬆ This launcher runs code 3 commits behind origin/main · restart the launcher

External SIGTERM (normal panes remain visible):
 ub-agents v0.1.11 · stopping                                ↑↓ select 1-3 tabs ? keys q quit

After q (whole screen, centered):

                    Shutting down the launcher

          Waiting for ⌥284 (implementer, 04:12) to finish.
          No new work will be claimed. Press Ctrl-C to stop now.

After Ctrl-C (whole screen, centered):

                    Stopping the launcher

          Terminating ⌥284 (implementer) and releasing its claim…
```

The shutdown screens replace both panes, the item header, update banner, footer
and any open overlay, including below the minimum terminal size. `#N` identifies
an issue and `⌥N` a PR. The graceful screen's elapsed assignment time keeps updating
through the run or recovery. With nothing running, either screen's middle line
reads `No run in progress.`; an idle graceful stop exits 0 promptly. Ctrl-C from
anywhere in the view, including the graceful screen, stops immediately (exit 130).
Repeated `q` presses do nothing on either screen; repeated Ctrl-C presses do
nothing on the Stopping screen. Only launcher exit closes the view and restores
the terminal, leaving the final launcher message visible. A standalone view still
closes immediately on either key. The `?` list describes `q   Stop after run` and
`Ctrl-C   Stop now`, with each line also saying it closes a standalone view.

`r` is available only with an attached launcher, on every tab and pane, and is
listed in `?`. It wakes idle polling or a continuous launch's read-only queue
planning during an assignment, with the next scheduled pass counted from that
refresh. It does not claim or interrupt during a run. In-flight presses are
dropped; forced passes show `running assignment · polling` until they complete,
fail or are cancelled.
The view shows this immediately when the snapshot confirms a polling waiter and
no cooldown or rate limit applies; the next snapshot replaces local feedback.
Reopened views show the active forced refresh, and repeated presses keep its
label. Scheduled in-run refreshes keep
`running assignment`. Forced passes have a shared 10-second cooldown.
`launch --once` and `launch N` have no in-run queue planning; presses during their
assignments are dropped without local polling feedback.
Attached views show rate-limit resets in local
time, alongside `running assignment` when queue planning is rate limited during a
run. Rate-limit waits and poll-retry backoff cannot be shortened. Countdown text
remains `Ns`. Narrow footers may omit `r poll now` before shortening existing keys.
Standalone views omit the key and ignore it, keeping the `next poll Ns` countdown
during rate-limit waits; `--no-ui` launches are unaffected.

The update banner is one themed row above both panes and the shared item header,
with the release age at the right when space permits. It truncates to the terminal
width and takes no focus. The activity footer remains at the bottom.
Pip releases name `pip install -U ub-agents` instead of the Homebrew command.
The launcher supplies the result in its session snapshot; the view does not read
GitHub for it. Plain launch output prints each new banner text once (#183).

Installed releases check the latest GitHub release at startup and at most daily.
The control-checkout editable install instead compares the started source with
the result of the latest normal fetch, which already fast-forwards the checkout;
the action is to restart. It makes no release request or extra fetch. An idle
launcher that has not fetched, or an editable install elsewhere, shows no notice.
Slow or failed checks retain the last successful result without delaying work.

- Idle: Running shows the idle line, and the status reads `○ Idle · waiting for the next poll`.
- Stopping (SIGTERM): the running row shows `■ stopping`, Eligible reads `not claimed
  while stopping`, and the footer reads `stopping`.
- Below 110×32: the Work pane only, at full width. ⏎ opens the selected item's tabs
  full width on the last active tab and Esc returns; overlays close first. Work
  rows use one line. Resizing keeps selection, tab, follow/pause and log position,
  showing the previously focused pane when narrowing. The footer shortens to
  `v0.1.11 · poll 26s`. Below 60×16, only a centered enlargement request appears;
  q/Ctrl-C still work and growing restores the prior state.
- `?` lists every key. The footer shows only the main ones.

## Colors

```text
Colors, as Textual theme variables with these dark-theme values:
  background #0d1016 · panel/footer #161a22 · text #d4d9e1 · dim #6b7484
  assistant text (view-assistant, italic without dimming) #c8cdd6
  accent (focus border, pane titles, launcher lines, ⌥) #b79cff · selection row #1b2030
  link hover (view-link, underlined header number) #6cb6ff
  Running #6cb6ff · Needs attention #ff8b7f · Eligible #7ee2a0
  diff + #7ee2a0 · diff - #ff8b7f
Focused pane: accent border with its title in the border ("Work", "Log", "Issue"); unfocused: #2a303b border.
Tab bar: active tab inverted (dark text on light), others dim; "Formatted" underlined in accent when on.
```

The view registers this palette as its default `ub-agents` Textual theme. Custom
variables fall back to the active theme's colors, so `textual-light` recolors
all panes, notices and retained log/Runs content. `NO_COLOR=1` renders in
monochrome. The terminal window title is `ub-agents launch — OWNER/REPOSITORY`,
using the session's repository. It updates when the title changes and clears on exit.
