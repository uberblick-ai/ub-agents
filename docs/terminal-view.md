# Local terminal view

`ub-agents launch`, `launch --once` and `launch N` open a read-only view of their
own session when stdin and stdout are terminals. `launch --no-ui` keeps plain line
output. Pipes, CI and services remain plain. Other commands do not import the UI
packages.

The view runs in its own process. It reads local files on the same macOS or Linux
host, as the same user as the launcher, from the **control checkout** rather than
an agent's private worktree. A launcher passes its exact session ID; it never
selects another fresh session. A missing, stale (over 30 seconds), ended or
incompatible snapshot, or a version mismatch after an upgrade under a running
launcher, produces a one-line error and plain output continues. The view has no
workflow controls. Pressing `g` on Issue or Unblock, or activating Unblock with
a missing snapshot notice and no cached load result, uses the user's existing
authenticated `gh` access.

The target look for upcoming changes is in [design/terminal-view.md](design/terminal-view.md).

## Installation

The view is part of every install: `brew install uberblick-ai/tap/ub-agents`, or
`pip install -e .` (or `mise run setup`) in a checkout, which installs Textual
8.2.8 alongside PyYAML.

## Using the view

The Work pane and active tab each have one rounded border, with the title in its
top edge. Work shows `Work · pass complete` (or the latest pass state) and any
omitted-plan count. The focused pane's border and title use the accent color;
the other pane's border is dim. Dashed rules separate the Work sections, Recent
activity, and the tab bar from the shared item header.

Both panes leave two columns inside each side border and one blank row under the
top border. Work rows, section rules and Recent activity share this padding. The
item pane uses it for the tab bar and its Formatted/Raw indicator, the tab rule,
shared header, every tab's content and log status lines. Right-aligned columns
and text shortened with `…` end inside the right padding.
Tab labels have one space on either side, so labels sit one column right of the
shared header and the inverted active tab has equal padding on both sides.

Scrollbars stay hidden at rest in Work, Log output, Issue, Runs, Unblock and the
`p`/`?` overlays. Scrolling with the wheel or keys, moving the cursor enough to
scroll, or dragging a visible bar shows only that area's muted, one-column bar.
It hides 1.5 seconds after the last scroll. Hovering or dragging keeps it visible;
the countdown starts when neither holds it. An overflowing area reserves the
column even while hidden, so content never moves or rewraps when the bar appears.
Follow-mode log appends, switching items or tabs, restoring log positions and
resizing do not show a bar.

An ordinary mouse click on the shared header's `#N` or `⌥N` reference opens that
issue or PR in the default browser, using the attached session's repository.
This works on Log, Issue, Runs and Unblock, including the narrow item view and
shortened titles. Only the marker and number open a page; the title, metadata
(including linked handoff PRs), rule and blank space do not. Missing or malformed
repository or item numbers leave the reference inert. The terminal must deliver
mouse clicks to the app and the host must be able to open a browser; terminals
that intercept clicks or hosts without a browser cannot use this action.
No Command-click is required.

The default `ub-agents` Textual theme has a dark background, purple focus and
selection accents, blue Running headings and glyphs, red Needs attention, and
green Eligible. Selection uses a shaded row. All colors follow the current
theme, including update notices, Runs marks and log diff counts. Textual's
`textual-light` theme recolors the view; `NO_COLOR=1` renders it in monochrome.
The terminal title is `ub-agents launch — OWNER/REPOSITORY`, from the session.
The view writes it when it changes and clears it on exit.

When newer ub-agents code is available, a themed, one-line banner appears at the
top, above both panes and the shared item header. It takes no focus and truncates
to the terminal width. Installed releases say
`⬆ ub-agents X is available · you run Y · brew update && brew upgrade ub-agents,
then restart the launcher`, or name `pip install -U ub-agents` for pip installs;
the release age appears at the right when space permits. The launcher makes one
GitHub REST request at startup and at most one per day while running.

An editable install from the control checkout instead says `⬆ This launcher runs
code N commits behind origin/main · restart the launcher` after a normal launcher
fetch discovers commits the started code lacks. Refresh already fast-forwards
that checkout, so restarting loads the newer code. An idle launcher that has not
fetched, or an editable install from another directory, shows no banner. Checkout
installs make no release request or extra fetch.

Checks run in the background; a slow or failed check keeps the last successful
result without delaying work. The result is included in the session snapshot;
the view makes no GitHub reads for updates. Plain launch output prints each new
banner text once. Restart with the current code to clear the notice.

At **110 columns × 32 rows** and above, the combined view shows both panes. At that size
and above, the Work pane's outer width, including its border, is one third of the
terminal width, rounded down and clamped to 46–64 columns. A one-column gap
separates the panes; the item pane takes the remaining width. Below either
dimension, one pane fills the terminal width with the same padding: Work, or the
selected item's tabs. `Enter` on a live or Recent activity row opens its tabs on
the last active tab; `Esc` returns to Work with the same row selected. Help and
raw-access overlays close first on `Esc`. The item view keeps the shared header,
tab bar, formatted/raw indicator and Log status line.

Narrowing shows the item view when the item pane had focus, otherwise Work.
Widening restores both panes with focus on the pane that was showing. Selection,
active tab, follow/pause, raw mode and log position survive these changes.
Below **60 columns × 16 rows**, only a centered request for a larger terminal
appears. `q` and Ctrl-C still work; growing the terminal restores the prior view.

The left pane groups work from the session snapshot into sections and shows each
section's row count. Running always appears first; other empty sections are hidden:

| Section | Work |
| --- | --- |
| Running | Only this launcher's current assignment, with a count of 0 or 1 |
| Needs attention | Blocked plans and parked plans with stop labels or approval gates |
| Eligible | At most ten items in claim order, merging ready/recovery plans before retry backoff and paused-runtime plans |

Work sections stay expanded and cannot be collapsed. Their headers and Running's
idle line cannot be selected or take the cursor; clicking them leaves the cursor
and selection unchanged.

Eligible's heading counts all eligible items: `Eligible · 7`, or
`Eligible · 23 · showing 10` when truncated. While stopping it adds
` · not claimed while stopping`; the heading shortens with `…` to fit the pane.
Claim order puts existing work first, then milestone, priority, age and number.
Continuous launch refreshes the queue with complete read-only planning passes
while an assignment runs; a failed pass keeps the previous rows.

With no assignment, Running shows one dim placeholder line,
`Idle · nothing eligible for this launcher`. It is not a work item and has no
log, Issue or Runs content. The line truncates to the pane width when needed.
When no row is selected, the Log status line reads
`○ Idle · waiting for the next poll`. Plans claimed by other launchers do not
appear in the Work pane; their runs remain in an item's Runs tab.

In the combined layout, each live work row occupies two compact lines. The first shows a status glyph,
`#N` for an issue or `⌥N` for a pull request from the snapshot's kind, the title
shortened with `…`, and a right column:

| Row | Glyph | Right column |
| --- | --- | --- |
| This launcher's assignment | Animated spinner, or `■` while stopping | Elapsed claim time (`04:12`, `1:04:12`), `claiming` before the claim is known, or `stopping` |
| Parked and needing attention | `?` | Waiting time in red |
| Blocked | `!` | Waiting time in red |
| Attempt limit reached | `✗` | Waiting time in red |
| Eligible, ready or recovery | `●` | `held` while stopping; otherwise `next` for the first ready/recovery row in planned order, then `ready` or `recover` |
| Eligible, delayed | `◷` | `backoff` for retry backoff; `waiting` for paused runtimes; never `next` |

Needs attention's second line is `agent · state · reason`, indented and dim.
Parked state names the stop label(s), joined by `, `; blocked state is `blocked`,
and an exhausted attempt limit is `failed F/M`. The reason uses the notice or
parking outcome's asks when available, without Markdown bold or bullet markers.
Notices with options lead with the summary's first sentence instead of an ask.
New notices without a recorded action show the concise request to review the blocker
and decide the next step; approval notices show the required authorization. Full
reasoning and technical details stay in Unblock's collapsed section. Without a
notice or recorded action, the reason falls back to the parking run's summary,
without the `Stop label … is present` prefix or retry instructions; it is omitted
when neither is known. Legacy notice summaries remain readable.

The launcher publishes `waiting_since` for each Needs attention row. It uses the
newest action-needed comment from an already verified trusted launcher account,
whichever launcher posted it, since the last claim or reset. Without that comment,
it uses the finalized outcome that set a currently present stop label. Blocked or
failed rows with neither use the latest finished run's release time. Unknown start
times leave the right column blank. Waiting updates locally while the view is open,
rounded down to minutes under an hour (`0m`–`59m`), hours under two days
(`1h`–`47h`), then days (`2d`, `3d`). These observations reuse the pass's comment
and author-role reads; showing or updating a waiting time adds no GitHub reads.

Eligible merges an item's plans after ordering them, keeping the position, glyph
and right column of its first agent. Its second line lists agents comma-separated
in that order; each failure count follows its agent, such as
`reviewer 1/3 failures, integrator`. A single-agent row keeps `agent · F/M failures`.
The section count counts items. Selection stays on the item while it remains
eligible, including when its agents change; Issue and Runs still show that item's
description and history. Running and Needs attention keep one row per agent, and
running agents are omitted from Eligible as before.

Line 2 appends the item's effective priority word after ` · `, for example
`issue-preparer · urgent` or `reviewer 1/3 failures, integrator · high`.
The word is the configured label after its last `:` and has no glyph. Without
`queue.priority`, or without a label or default, only the usual agent details
appear. In the default dark theme, urgent is `#e0524a`, high is `#c98a86` and low
is `#86a891`; medium and other words use the usual muted line color. The
`view-priority-urgent`, `view-priority-high` and `view-priority-low` theme variables
derive equivalent colors for other Textual themes. `NO_COLOR=1` leaves words
uncolored.

Other live rows' second line is indented and joins the agent, `this launcher` for the
assignment, and count with ` · `. The count is `finishing run` for a stopping
assignment, `attempt N` for other assignments, or `F/M failures` for plans with
at least one failure. Missing parts are omitted. Both lines fit
the current pane width at 110×32; long second lines end in `…`.
There is no separate reason leaf: the full reason remains on the Issue tab.
In every Work section, one blank row separates consecutive two-line items. In
both layouts, exactly one blank row precedes each displayed Needs attention and
Eligible heading, including after Running's idle line. Hidden sections add no
separator. There is no gap within an item, after a heading, before Running or
before Recent activity. The cursor highlights only the item's lines. Arrow keys
skip headers, the idle line and blank rows, and either item line can be clicked
to select it; blank rows have no cursor or hover highlight and clicking them
changes no selection or cursor.
Polls keep the highlight on its item when rows are added, removed or reordered,
including when the item moves sections or changes to a related row. If the
highlighted item leaves the Work list, the highlight returns to the item shown
in the right pane when that item is still in the list. The right pane keeps its
item, active tab, focus and log reading position while that item remains available.
In the narrow Work list, live and Recent activity rows use only their first line:
glyph, item reference, title shortened with `…`, and right-aligned waiting time
for Needs attention or state for other live rows. Detail
information remains on the item's tabs, and consecutive single-line items have
no blank row between them. Sections, counts, the idle line, stopping
state and the fixed upper/lower split behave the same in both layouts.
The assignment spinner advances through `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` one frame every
0.1 seconds, like the visible Runs tab and the log status line. Stopping and other
row glyphs remain static. Elapsed time stays in whole seconds, uses the item's
cached run history and updates at least once a second while the view is open;
the view retains an observed claim time when a report updates the history.
If that claim time is unavailable, the row shows `claiming`.
Rendering these rows requires no extra GitHub reads.

After SIGTERM, the view uses the snapshot's `activity.state: stopping` to show
that this launcher is finishing its current run and will claim nothing new.
Eligible's rule reads `Eligible · N · not claimed while stopping`, adding
` · showing 10` before the stopping note when truncated, shortened
with `…` at narrow widths like other headings. Ready and recovery rows show
`held`; delayed rows keep `backoff` or `waiting`. The footer reads `stopping`.

Dependency and milestone waits are parked plans whose reasons start with
`Waiting for blockers …` or `Waiting for active milestone #…`. They are omitted
from the Work pane and all section counts. There is no Waiting section.
Retry backoff and paused-runtime plans remain in Eligible because this launcher
can run them once their delay passes. Within Eligible, ready/recovery rows keep
their planned order, followed by delayed rows in their planned order.
While a pass is incomplete, the Work border title shows `Work · pass partial`.
Visible rows from the previous pass remain, with their cached descriptions and item
history, until the new pass completes, except when discovery observes that the item
is closed or merged, or an Eligible row's item no longer carries that agent's trigger.
Those rows disappear even before planning reaches them. Open Needs attention rows,
including `needs-human` handoffs without a trigger, remain until replanned or the pass
completes. A new plan for the same item and agent, including recovery, replaces the
prior row. Each re-planned item and agent updates in
place, moving sections if its state changes; new rows follow the kept rows in
their section, with ready/recovery rows always preceding delayed rows in Eligible.
Completion removes omitted plans and applies the new planned order within each
Eligible subgroup before merging plans for the same item. A selected removed
Needs attention row leaves the Work list and count; its Issue and Runs details
remain as an earlier observation until selection moves. Other selected removed
plans remain as earlier observations. Closed or merged items with only a released
retry or blocked run no longer need attention. Live or unfinished leases, pending
outcomes or transitions, unconfirmed cleanup and invalid coordination history still
require resolution. Recent activity contains recorded outcomes only; removing a
completed item adds no outcome.

**Recent activity · N today** always fills the lower half of the Work pane,
including `0 today` when there are no outcomes. N counts cached session outcomes
dated today in the viewer's local timezone. Up to 20 cached outcomes appear
newest first, including older outcomes, as dim two-line rows in the combined layout; the selected row
shows at full brightness. Only whole items with their intervening blank rows fit;
items that do not fit are cut from the oldest end. Narrow lists have no blank rows.
As an exception to the muted row color, outcome icons and status labels use the
theme's green success color for `✓` and red error color for `✗`, retaining row
dimming and cursor highlighting. Neutral `○` outcomes keep the row style.
`NO_COLOR=1` keeps these cues monochrome.
The lower half does not scroll or collapse, and its header cannot be selected.
The live sections fill the upper half and scroll vertically, with no horizontal
scrollbar. Unchanged worker results leave Work and Recent activity untouched;
the running spinner and elapsed times continue to update on the view's clock.
Arrow keys move between the two halves; `Enter` selects an outcome with the same
local log access as other own runs. With no live rows, the newest outcome is selected first.
A selected outcome pushed out of view remains selected in the right pane.

Recent activity uses `#N` for issues and `⌥N` for PRs, from the cached outcome or
item history. Its detail line shows `agent · time · opened ⌥N` when the outcome
hands off to a PR other than its own item, followed by ` · summary` when present.
Other outcomes show `agent · time · summary`, omitting missing parts.

Every PR marker (`⌥`) generated by the view uses the accent color in live rows,
Recent activity rows and details, and both item header lines. The number keeps
the line's existing style, including dim text and selection highlighting.
Issue references remain `#N`, including `closes #N` on Runs. Agent summaries,
issue descriptions and log lines keep their verbatim text and existing styles.

Outcome completion and a human blocker are shown separately: a completed step
can still be blocked. Until you select a row, the view selects the launcher's own
run whenever one starts. Selection, focus and paused log positions survive refreshes,
including when a row moves between sections. A selected claiming assignment stays
selected when its run ID appears; Log picks up and follows its output as soon as
the local `process.log` exists. A selected row that disappears
remains an earlier local observation in the right pane. A previous assignment or
a plan now claimed by another launcher or parked for dependencies or a milestone
is omitted from the live work sections.

The right pane has `1 Log`, `2 Issue` and `3 Runs` tabs, plus `4 Unblock` while the
selected row is in Needs attention. The active tab is inverted
and the others are dim. After a `│` separator, the inert `Formatted  Raw` indicator
shows the selected log's `u` mode: Formatted is underlined in accent when active,
and Raw is highlighted when active. The indicator has no key or focus target.
Each tab starts below the tab bar with
the same two-line item header and a dashed rule. The bold first line shows `#N`
for an issue or `⌥N` for a PR, followed by its title. The dim second line joins
the agent, runtime (`cli model effort`), running assignment's `attempt N` or
planned work's `F/M failures`, and a session outcome's linked PR (`⌥N`) with
` · `. Missing values are omitted. Failure counts and the agent's `max-attempts`
come from the launcher's existing coordination reads, within the snapshot limits.
Issue first uses the snapshot description or that session's run `context.json`.
A shortened or empty cached description is available and needs no GitHub read.

Unblock shows the latest trusted action-needed comment for a parked or blocked
item, including an exhausted attempt limit. Its bold header keeps the item
reference and title. Its dim second line shows the agent and the same state as the
Work row, then red `waiting …` and `since HH:MM` in local time. Both displays use
the row's published `waiting_since` and the same minute/hour/day format, counting
while the view is open. Unknown times are omitted; loading a comment
does not change this start time.
The dashed rule follows as on the other tabs. Changing selection or refreshing
the item out of Needs attention hides Unblock and returns an active Unblock pane
to Log. `4` has no effect for other rows.

The comment body uses Issue's inert Markdown rules and 2,048-character limit,
with a visible shortening notice. The action-needed marker, `**Action needed**`
title are removed first. New notices keep Claim/Outcome links with the folded
evidence; earlier formats omit their standalone links line, including the
no-outcome variant. New notices show the reason, any independent asks, numbered alternative
options with the first recommended, and visible "Then resume" steps. Option
commands render as separate code blocks; single-backtick inline code is preserved.
Without options the asks keep their previous layout and resume steps stay visible.
The "Reasoning and evidence" control starts collapsed; click its title or focus it
and press Enter to expand it. The bounded supporting Markdown and SHAs stay intact
inside. v0.1.13 notices keep their "Reasoning, evidence and resume instructions"
fold, and earlier prose notices remain readable. The last dim line names the
action-needed comment, its local creation time, and `snapshot` or
`GitHub · loaded Ns ago`. Opening Unblock with `4` or a tab click loads a missing
or omitted snapshot notice if no successful or failed result is cached for that
item. Reopening the tab does not repeat a cached read; `g` retries failures.
An uncached comment offers `press g to load from GitHub`. A notice omitted for
size says `Comment left out of the snapshot to save space; loading from GitHub…`
while its read is pending, or `Comment left out of the snapshot to save space;
press g to load from GitHub.` otherwise. A cooldown or another pending read
shows why the load cannot start and queues nothing; a later activation may try
again while no result is cached. Selecting another item while Unblock stays
active starts no read.

The session snapshot retains comments this launcher posts or finds in the pass's
already-read item comments from verified trusted launcher accounts. Claims and resets clear
them. Text and item counts are bounded. To fit the 64 KiB snapshot, notices for
items outside Needs attention (including items without a row) are omitted first,
then description previews are shortened and surplus history runs trimmed. Needs
attention notices are omitted next, before plans and outcomes are trimmed. Each
omitted notice keeps an `omitted` marker without text in the published snapshot;
the retained launcher state keeps its text. GitHub loads accept
only comments whose body starts with `<!-- ub-agents:action-needed ` and whose
author the launcher has already verified for coordination records. No role read
is added. Unverified snapshot authors are excluded with a reason and do not
auto-load; `g` remains available. A load without a trusted
match says so. Existing verification changes also remove a cached comment from
display. The tab is read-only.

Runs shows the **selected item's history**, oldest first, from coordination
records the launcher has already read, including runs by other launchers. Below
the shared header, a dim line shows `closes #N` for a PR with a local closing issue,
`filed by LOGIN` when known, and the run count. The count includes omitted runs
and excludes the filing row. For a PR with multiple local closing references,
the lowest issue number supplies the filing row. A PR without a closing reference
has no filing row.

The first table row shows who filed the issue and when, with `GitHub` and `filed`
in the where and outcome columns. On a PR, this is the closing issue's filing,
only when that issue's author and creation time were already read during the
launcher's pass. Missing filing data leaves out both the row and `filed by`.

Each run shows its outcome time, or claim time until an outcome exists; a green
`✓` for success, red `✗` for a failed or abandoned run, or a spinner while in
progress; the agent and summary; its host; and its named outcome or run status,
followed by any human blockers (`BLOCKED: …`). Unaccepted successes show a spinner
while their lease is live, then a red `✗` if it expires, is released or is withdrawn
before acceptance. Rejected, blocked and retry results remain red.
Every run and filing row occupies exactly one line at any pane width, including
60 columns; text too long for any cell is shortened with `…`.
Relative time steps are `just now`, `N min ago`, `N h ago`,
`yesterday`, `N days ago` within the past week, then `YYYY-MM-DD`. Calendar days
and dates use the viewer's local timezone.

When an outcome records permission denials, its outcome column also shows
` · N denied`, counting recorded entries plus `denials_omitted`, for example
`approved · 4 denied`. Outcome and blocker text shorten first so the count stays
whole on the same line. Denied commands remain available through
`ub-agents status --json`; `ub-agents status` also shows the count. Up to 10 entries
are recorded, with commands truncated to 200 characters. Claude runs supply these
fields only after a final `result` event; a zero count, missing or malformed fields,
and Codex runs show no count. Denials are display-only and do not change the run's result.

The where column uses up to 14 columns and shrinks in narrow views. This host reads `this machine`;
other hosts are dim, with their domain removed and long names shortened with
`…`. Host data comes from the claim, falling back to an outcome's recorded host
for handoff copies without a claim; older records without either show `unknown`.
A claim and its outcome, including handoff copies, count as one run. The snapshot
retains at most 20 runs per item and stays within its shared 64 KiB limit, with
a notice counting any earlier runs omitted. Under byte pressure, long plan
descriptions are shortened to 256-character previews before the globally oldest
runs are omitted. Each item's newest run is retained; if necessary, later plan
rows and older session outcomes are then omitted with their unreferenced histories.
The current assignment's history remains. These reductions affect only published
snapshots; the next snapshot can use the launcher's retained data again.
Unreadable coordination records preserve an item's cached history from the
previous polling pass.
An item with no filing or run data shows `No item history cached.` Recent activity
rows each show their item's history.

Issue renders only the description body as Markdown, including headings, lists,
emphasis, strikethrough, inline code, code blocks and GitHub tables. Wide tables
shrink their columns and wrap cell text within the pane. Line breaks, including
CRLF and lone CR, display as real line breaks; tabs are retained. Other control
characters stay visibly escaped. Rich/Textual markup such as `[bold]` stays literal. Links,
images, raw HTML and entities display as source text, including in table cells;
links cannot be opened with the mouse or keyboard, and nothing is fetched.
The item header stays literal text with only its reference clickable;
source/age and all notices remain inert literal text.
Issue does not repeat the item reference or title in its
content. Shortening notices sit outside the Markdown body, including
when a description is cut inside a code fence. Runs and the raw log projection
remain literal text with visibly escaped control characters. The formatted
Claude and Codex Log transcripts are described below.

| Key | Action |
| --- | --- |
| `Tab`, arrows, `Enter` | Focus a pane and select a work row |
| `Enter` in the narrow Work list | Open the selected live or Recent activity item's tabs at full width |
| `Esc` in the narrow item view | Return to Work; close help or raw access first |
| `1`, `2`, `3` | Log, Issue, Runs |
| `4` on Needs attention | Unblock; ignored for other rows |
| `g` on Log | Reload the selected row's log from the latest local snapshot, attach at the end and follow; no GitHub request |
| `g` on Issue | Load the selected item's missing title/body, or retry a failed description read |
| `g` on Unblock | Load the latest trusted action-needed comment, or retry a failed comment read |
| `f` | Toggle follow/pause; resuming loads the latest generation |
| `h` | Read an older bounded page, down to byte zero |
| `u` | Toggle formatted/raw projection of the same page |
| `p` | Show the full raw file path, byte ranges and retention diagnostics; `Escape` closes it |
| `r` (attached launcher) | Poll GitHub now while idle, or refresh the queue read-only during a continuous launch's run; at most once per 10 seconds |
| `Page Up`, `Page Down`, `Home`, `End` | Scroll the active tab or overlay; scrolling up in Log pauses follow |
| Mouse drag and release | Copy the selected text to the clipboard through OSC 52, including in the `p` and `?` overlays |
| `y` | Copy the current selection again; do nothing without a selection |
| `?` | Show all keys; `Escape` or `?` closes help |
| `q` | Stop after the current run or recovery, with no new claims (exit 0); close a standalone view |
| `Ctrl-C` | Stop now with the launcher's normal SIGINT handling (exit 130); close a standalone view |

Each copy briefly shows `copied N characters` in the footer. Plain clicks and
empty selections copy nothing. OSC 52 works over ssh and inside herdr when the
terminal accepts it. In iTerm2, enable **Applications in terminal may access
clipboard** for OSC 52. Terminal.app does not support OSC 52.

On macOS, the view also uses `pbcopy` when it is on PATH and neither
`SSH_CONNECTION` nor `SSH_TTY` is set. This covers local terminals that do not
accept OSC 52. A failed or slow `pbcopy` does not delay the view or prevent the
OSC 52 copy. Use **Option-drag** for the terminal's own selection. Ctrl-C remains
the stop key.

On an attached view, either stop key replaces the panes, header, update banner,
footer and any overlay with a centered full-screen message until the launcher
exits. After `q`, it reads `Shutting down the launcher`, names the running item,
agent and updating elapsed assignment time, and says
`No new work will be claimed. Press Ctrl-C to stop now.` Ctrl-C shows
`Stopping the launcher` and names the item and agent being terminated and its
claim being released. Both screens say `No run in progress.` when idle.
Repeated `q` presses do nothing on either screen, and repeated Ctrl-C presses do
nothing on the Stopping screen. External SIGTERM keeps the normal panes with
the existing `■ stopping` row state.

The one-line footer shows the snapshot's launcher version and activity on the
left: waiting counts down as `next poll Ns`; other activities say `polling`,
`running assignment` or `stopping`. Stale, ended and malformed snapshots are
labelled there, including the malformed error, without a snapshot-age counter.
In an attached view, `r` starts the next poll immediately or requests a read-only
queue refresh during a continuous launch's run, without claiming or changing the
running assignment. `launch --once` and `launch N` have no in-run queue planning,
so presses during their assignments are dropped and keep `running assignment`.
During that forced refresh, the footer shows `running assignment · polling`,
including in a view reopened while it runs. The view shows `· polling` immediately
on `r` when the snapshot confirms a polling waiter and no cooldown or rate limit
applies. The next launcher snapshot replaces that local feedback. The label clears
when the refresh completes, fails or is cancelled. Scheduled refreshes during a
run keep `running assignment`.
The next regular poll or planning refresh is counted from that forced pass.
Presses during a pass are dropped; a repeated press during a forced refresh keeps
`· polling`. After the refresh ends, pressing again within 10 seconds of the
accepted request shows
`poll now available in Ns`; rate-limit waits show
`rate limited until HH:MM · r unavailable`, in local time, alongside
`running assignment` when queue planning is rate limited during a run. The key
never shortens rate-limit waits or poll-retry backoff, and does not change attempt
limits or the regular poll interval. Standalone views do not offer or act on `r`,
and keep the `next poll Ns` countdown during rate-limit waits.
In the narrow layout, the `ub-agents` prefix is omitted and `next poll Ns` becomes
`poll Ns`,
for example `v0.1.11 · poll 26s` or `v0.1.11 · running assignment · polling`.
Other activity and diagnostic labels keep their
text. The list's right side reads `↑↓ select ⏎ open ? keys q quit`; the item view
reads `Esc back 1-3 tabs ? keys q quit`. On Log, the footer adds `g reload`,
dropping it at narrow widths when it does not fit. Reloading before the log exists
keeps the existing empty-log notice. A paused item uses the existing log keys,
including on Issue and Runs, shortened only when they do not fit. Needs attention
uses `1-4 tabs`; on Unblock the footer also includes `g load`, including when the
log is paused. `?` includes `4` and `g on Unblock` only for Needs attention rows,
alongside the other keys and narrow `Enter` and `Esc`.
Attached views also include `r poll now` among the main footer keys on every tab
and pane. Narrow layouts drop it before shortening the existing keys when it
does not fit.

A pill at the bottom right of the log output, above the run status, appears only
when paused or behind. It shows PAUSED or BEHIND, nonzero unread entries and byte
lag, and RAW when that projection is on. There is no FOLLOW badge. Log notices
appear only when applicable: file changes or an earlier generation, read errors,
unfinished records, unknown-runtime fallback and older-page notices. Pausing
freezes the page and its position while ingestion continues. Notices and the
pill can change the pane's height without rewrapping or moving the paused page.
The separate run status describes the process and reported outcome. Revisiting
tabs or selected rows restores that page and position. Width changes and
raw-mode changes retain the
entry at the reading position, with a proportional position within wrapped text.
When the anchored record is hidden in formatted mode, the pane shows the next
visible entry (or the preceding entry at the end of a page). Its original byte
position remains the anchor until scrolling moves away; `u` restores that raw
record, including across resizes. A page containing only hidden records is empty
in formatted mode and still available with `u`.
Byte ranges, lag, older-page boundaries and render-limit counts include every
record within the line budget, including records hidden by the formatted projection.

Claude's formatted Log pane shows a compact transcript:

```text
          · earlier output skipped · h older
 07:41:18 ▸ Read packages/hub/src/directory.ts
 07:41:40 Both paths rebuild stubs independently.
          Moving to a shared repairStubs() in schema.
 07:41:41 · thinking
 07:42:11 ▸ Edit packages/schema/src/directory.ts +48 -0
 07:42:30 ▸ Bash pnpm test --filter schema · 1m
 07:43:31   ✗ Exit code 1
 07:43:32 ✓ run finished
```

The time column uses the producer's timezone-aware `timestamp`, converted to local
`HH:MM:SS`. Missing or invalid producer times use a marked capture time
(`~07:42:30`); pre-existing bytes without either time leave the time column blank.
The fixed nine-column time slot reserves its first place for the capture marker:
exact times appear as ` 07:42:30`, capture times as `~07:42:30`. One space separates
this slot from the text, so entry text always starts in column 11, including
assistant continuations. Assistant text, including the closing summary, is italic
without dimming, using `view-assistant` (`#c8cdd6` in the default dark theme).
Themes without this variable derive a color close to their foreground. Its line
breaks and wrapped continuations align with the text column. User prompt text
keeps its plain italic style. All other C0/C1
controls, including terminal escape sequences, remain visibly escaped.

Tool calls show `▸`, the name and the main argument: `file_path` for Read, Edit and
Write, `command` for Bash, and `pattern` for Grep and Glob. Other tools use their
first string input when available. Long or multiline arguments end with `…` and
calls fit one display line. Edit adds green `+N` and red `-N` line counts from
`new_string` and `old_string`; Write adds green `+N` from `content`. Missing fields
have no count. Call IDs and JSON inputs are available only in raw mode.
Counts use LF-separated lines; a trailing LF adds no extra line.

`tool_progress` records add no formatted line. For a retained call, its latest
progress adds a dim elapsed suffix: `· 45s` in whole seconds below a minute,
then `· 1m` in whole minutes. This value updates as progress arrives and stays
on the call after it finishes. The elapsed suffix and Edit/Write counts stay
visible when a long call is shortened to the pane width. Progress for calls
outside the retained page is hidden. Paused pages keep their displayed values
until follow resumes.

Successful tool results add no line. Failed results show one red, indented `✗`
line with the first error line. When another visible line has intervened, it
includes the tool name, or `tool:` if the call is outside the retained pairing
history. Thinking
blocks show one dim `· thinking` line; unfamiliar blocks show a dim type label and
the rest of the message still renders. System records, including initialization,
thinking-token updates and task notifications, and rate-limit events are hidden.
Other complete JSON records show a dim type/subtype label. A final successful
result shows `✓ run finished`; a failed result shows a red `✗`, its subtype and
first error line, without repeating the last assistant message. Runtime errors
also show a red `✗` line.

In Claude's formatted pane, a cut-off first record at a tail start or an older
page's starting byte boundary shows one dim `· earlier output skipped · h older`
line, with none of its bytes in formatted mode. `h` reads toward earlier output.
Raw mode retains that fragment's bytes and every record, including hidden progress records;
the `p` raw-access screen is unchanged. Other split, oversized, unfinished and
non-JSON fragments keep their labelled raw display.

Codex's formatted Log pane uses the same time column, symbols, assistant styling,
red failures, escaping and projection limits. It is checked against complete,
owned `codex exec --json` recordings from **codex-cli 0.160.0** (#126):

| Event | Formatted projection |
| --- | --- |
| `thread.started`, `turn.started` | Hidden; retained in raw mode |
| `item.completed` / `agent_message` | Assistant text with aligned line breaks |
| `item.started`, `item.updated`, `item.completed` / `command_execution` | `▸ Bash` and the command; failed completion adds `✗`, exit code when present and the first output line |
| Same item events / `mcp_tool_call` | `▸ server.tool`; failed completion adds the first error message or text result line |
| Same item events / `file_change` | `▸ file add/update/delete` and each path; a failed completion adds `✗ file change failed` |
| `item.completed` / `error`, `error`, `turn.failed` | Red `✗` and the first message line |
| `turn.completed` | `✓ run finished`; usage remains in raw mode |

Repeated activity updates and successful results add no line when their start
is in the bounded item-ID cache. An unpaired completion shows its command, tool
or file paths, so near-tail attachment and history pages remain readable. A
delayed failure includes its tool name. The recordings contain starts and
completions; synthetic tests exercise same-shape `item.updated` records.

This CLI emits no producer timestamps: newly captured bytes show `~HH:MM:SS`,
and pre-existing bytes leave the time column blank. An explicit timezone-aware
top-level `timestamp`, if present, uses the same producer-time rules as Claude.
No time is extracted from IDs, item fields or message text. File records carry
operations and paths, without diffs or line counts; none are invented. MCP arguments,
successful command/tool output, token usage and IDs remain available in raw
mode. Reasoning, plans, web searches and other unvalidated item shapes show a
dim event/item-type label. Errors whose message is itself JSON stay message
text; nested payloads are not interpreted. Changed or unfamiliar complete
records also show a dim type label instead of assuming a newer CLI's semantics.
Claude-shaped `tool_progress` records are unvalidated for Codex and use that
dim label; Codex records supply no elapsed call time, so none is invented.
Malformed Codex lines that begin with `{`, including records below the size
limit, show a dim
`· incomplete or unrecognized Codex record omitted · full record in Raw` notice.

Oversized Codex JSON objects produce one compact projection when their event
fields can be validated within the existing bounds. Large text is shortened;
command outcomes are checked at the record's end, so failures still show a red
`✗` with the first output line. Successful output stays in Raw. Activity labels
and item-ID deduplication work as for smaller records. JSON-looking text inside
output, messages and errors remains inert text.

If the shape cannot be validated within the bounds, Formatted shows one dim
`· oversized Codex record omitted · full record in Raw` notice. Tail attachment
and history-page boundaries show a compact omission notice for each affected
span, including pages entirely inside a large record. Unfinished records show
an incomplete-record notice until their newline arrives; then their single
projection replaces the notice. The next complete record formats normally.

Raw mode retains every record and bounded fragment, including hidden records,
with its existing escaping, shortening and labels. The bytes on disk and raw
access are unchanged. Oversized lines that do not begin as JSON objects, such as
diagnostic text, keep their labelled raw display in Formatted too. Unknown
runtimes use the labelled plain/raw fallback. Read, pending-buffer, retained-entry,
projection and page limits still apply; validation across fragments keeps bounded
state. Runtime output never establishes a workflow outcome.

Log ends with a dashed rule and a one-line status showing the agent and its
process or plan state, plus `no outcome reported` or the reported result and
acceptance. A spinner appears while the process runs. Other cached session
outcomes for the same item appear at the right as `N earlier run(s)`, omitted
when zero. Header, Log notice and status lines are shortened with `…` to fit the
pane's width. A highlighted notice above the log appears only for file or
generation changes, read errors, unfinished records, responses to `h`, or a
runtime's plain/raw fallback.

While stopping, selecting the current assignment replaces its Log status with
`■ Stopping after this run (SIGTERM) · no new claims`. Other selected items keep
their usual process or plan status. The stopping screen uses only the existing
session snapshot and makes no GitHub reads.

Pressing `g` on Issue or Unblock starts a GitHub read. Activating Unblock with
`4` or a tab click also loads a missing or omitted snapshot notice when that
item has no cached load result. Attachment, selection, other tab activations,
redraws, resizes and timers make no GitHub calls. Each load makes one
`gh api graphql` request: Issue reads only the selected issue or PR's title and
body; Unblock reads its most recent comments (at most 100), with author, creation
time and body, without paging. There are no history reads, queue scans, prefetch
or extra role reads. Automatic loads happen only on Unblock activation and never
retry a cached failure. There is at most one read in flight: pressing `g` or
activating Unblock while any read is pending queues nothing. Input and local
snapshot/log reading continue while it is pending, with a loading notice on the requesting tab.
Closing the view terminates and reaps its owned request processes. A request
supervisor also cleans them up if the view is killed. `q` drains an attached
launcher and Ctrl-C interrupts it; either key closes only a standalone view.

Descriptions show their source and age: snapshot publication time, run context
file modification time, or GitHub load completion time; missing local timestamps
say age unavailable. Each title/body projection is limited to 2,048 characters
plus a visible shortening notice. Successful and failed GitHub reads stay only
in memory, in a 128-item least-recently-used cache shared by Issue and Unblock across rows for the same
repository/item. Revisiting a cached item never calls GitHub; a failed read needs
`g` to retry. Evicted items can load again through `g` or Unblock activation.
There is no session load
count limit. Each request has a 10-second time limit and a 512 KiB response limit.
After a rate-limit response, both tabs show a shared cooldown through the reported
reset or `Retry-After` time. Loads and retries make no call during it. If GitHub
provides neither a future reset nor retry time, the cooldown is 60 seconds.

Attachment reads near the tail; `p` shows the displayed and retained page's byte
ranges, full raw path, eviction, skip and shortening counts, and the rendered-line
limit and hidden-entry count. These diagnostics do not occupy rows above the log.
Ingestion reads at
most 32 KiB and processes at most 128 records per update; a bounded first-read
selection avoids replaying a tiny-record history before recent output. Retention
is 200 entries per reader. Older reads inspect at most 32 KiB plus identity
anchors and decode at most 200 complete records and one fragment. Rendering keeps
at most 400 wrapped lines, discarding whole older entries. `p` shows this rendered
limit and hidden-entry boundary, plus eviction, skip and shortening counts.
Press `h` to recover earlier bytes from disk. A paused page is independent of reader
and renderer retention. Text is limited by the formatter to 2,048 characters per
projection, with shortening markers. `u` is also a bounded projection: use `p`
and an external pager for the full raw file. Oversized and split records are never
interpreted as complete events. Except for the skipped-output marker at a page's
start, their fragments retain the labelled raw display.

Replacement, rotation and truncation reset the reader without mixing generations.
A paused older generation remains visibly labelled until follow resumes. Older
reads validate the file identity and byte anchor before and after reading; a
changed file's page is rejected. Snapshot, context and log reads run on one daemon
thread with single-slot request/result mailboxes. A slow read never blocks input
or grows a backlog. The separate GitHub transport reads bounded nonblocking pipes.
No file writes or launcher execution imports occur in the view. Cached own-run readers are
limited to 21, matching the session's bounded
20 recent outcomes plus its current assignment.

Launcher lines written while the view is open continue to append, with the same
UTC timestamps, to `.ub-agents/launch.log`. Stopping with `q` lets the current run
or recovery finish its report, label transitions and cleanup; Ctrl-C interrupts
and cleans up owned runs. On launcher exit the view closes, the
terminal is restored, and the final launcher message is visible. A crashed or
killed view produces one diagnostic and resumes plain output without replaying
past lines. SIGTERM drains the launcher normally; SIGHUP interrupts it like Ctrl-C.
Reopening a crashed or killed view from a running launcher is not supported.

## Validation

The post-merge GitHub run builds a wheel and source distribution, installs the
wheel into a clean environment, checks that help and plain launch do not import
Textual, and resolves the view entrypoint. Local checks:

```sh
.venv/bin/python -m tests
.venv/bin/ub-agents check
git diff --check
```

An actual terminal acceptance check is also required, separately from headless
Textual pilots or screenshots. In a real 110×32 terminal, attach to a live launcher
or a local replay that appends to a session's `process.log` and publishes snapshots:

1. Publish a replay with the current assignment, owned, blocked, parked stop/approval,
   ready/recovery, dependency/milestone wait, backoff and paused-runtime plans.
   Confirm Running, Needs attention and Eligible appear in that order, with
   correct counts and no Waiting section. Dependency/milestone waits must be
   absent from rows and counts. Within Eligible, ready/recovery rows keep their
   planned order, followed by backoff and paused-runtime rows with `◷` and their
   `backoff`/`waiting` states; delayed rows never show `next`, even without any
   ready/recovery rows. The current assignment appears once. Empty sections are
   hidden except Running. A partial pass has a dim marker on
   the pane title, which disappears when the pass completes. Confirm follow reaches
   recent output and cached Issue and per-item Runs tabs are readable. Check the
   shared two-line header and dashed rule on every tab, omission of missing values,
   and the Log status, spinner, reported acceptance and earlier-run count. Confirm
   the assignment spinner animates without moving the row, selection or scroll
   position, while elapsed time changes in whole seconds and stopping stays static.
   In Runs, check the filing row, local and foreign hosts, in-progress and completed runs,
   blockers and denial counts. Confirm every row stays on one line with long text
   shortened at the minimum width and denial counts kept whole. Select
   another item and confirm its history replaces the prior table without a
   GitHub request; include an item with missing filing data and omitted runs.
   Check the single footer line shows the snapshot's version and activity, including
   the waiting countdown and stopping state, with readable main keys at 110 columns.
   Check stale, ended and malformed snapshots are labelled, with the malformed error,
   without a minimum-size hint. There is no snapshot age or
   Local files/GitHub diagnostic line. Open `?`, check every key, and close it with
   both `?` and `Escape` without losing selection or the reading position.
   Show and hide a notice while paused; the reading position must not move.
   Include today's and older outcomes; check Recent activity's count against the
   local date and its newest-first, dim two-line rows. Confirm its header cannot
   be selected and there is no collapse key. At 110×32 and a larger size, empty
   and overflow the live sections: the split must remain halfway down the Work
   pane and upper scrolling must not move Recent activity. Check `0 today` with
   no outcomes, whole-row clipping from the oldest end without lower scrolling,
   and the newest outcome selected first with no live work. Use arrows across
   the split and `Enter` to select an outcome; it must show at full brightness.
   Pause its log, publish refreshes and move a selected plan between sections;
   check selection, focus and paused positions. Revisit the outcome and check
   the restored paused page. Add newer outcomes until the selected outcome is
   clipped; its right pane must keep showing it. Remove a selected plan and
   check its earlier observation remains. Remove a selected Needs attention row;
   it must leave the list and count while Issue and Runs keep their cached details,
   without adding Recent activity. Park a selected plan for dependencies
   or a milestone; it must leave the Work rows and counts while its cached
   details remain in the right pane.
2. Pause, scroll, continue appending more than 200 entries and 400 wrapped lines,
   visit Issue/Runs and another work row, then return. The paused page and reading
   position must remain stable; unread and lag must grow in the Log pill, which
   appears only when paused or behind. Check the footer switches to log keys on
   every tab, and the pill stays in Log.
3. Read older pages toward byte zero, switch raw mode and resize. Check older-page
   notices and the pill's RAW marker. Open `p` and check the full raw path, byte
   ranges, eviction/skip/shortening counts and rendered-line limit there. Those
   diagnostics no longer occupy the Log header. Resume follow; the pill disappears
   when caught up, and no FOLLOW badge appears.
   With `tests/fixtures/runtime_logs/claude.log` as the replay source, check the
   compact timestamps, thinking markers, tool calls, red failed Bash result and
   final success line. Pause in raw mode on a `system` or `rate_limit_event`
   record, toggle `u`, resize and toggle back; the same raw record must return.
   Repeat on the failed tool result, and confirm successful results and system
   records are visible only in raw mode.
   Repeat with `tests/fixtures/runtime_logs/codex.log`, `codex-tools.log` and
   `codex-error.log`: check assistant text, command/file/MCP activity, red failed
   results, runtime errors and run completion. Newly appended Codex records use
   capture times; pre-existing records leave the time column blank. Toggle raw
   mode on a hidden `thread.started` and a failed result, and verify the anchor survives. The
   status line must still say `no outcome reported` when no workflow report exists.
4. Replace or truncate the replay log. Check generation recovery and refusal of
   older reads from the prior generation.
5. On Issue, select a row with no local description and press `g`. Confirm loading,
   then source/age and the loaded description or a cached failure. Visit other
   tabs/rows and return; confirm no further call. Retry a failure explicitly.
   Use a description with headings, lists, emphasis, inline code, code blocks,
   LF/CRLF/lone-CR line breaks and tabs. Check that only the body is formatted,
   `[bold]` and an ESC sequence stay literal/inert, and links, images and HTML
   remain text without opening or fetching anything on click or keyboard input.
   Repeat with snapshot and run-context descriptions, and with a body shortened
   inside an unclosed code fence; the plain shortening notice must remain visible.
6. Select parked, blocked and exhausted rows and check `4 Unblock`, its contextual
   footer/help keys, header, advancing waiting time, dashed rule, Markdown and dim
   provenance line. Check snapshot run and approval-gate comments, both links-line
   variants, and shortening inside a code fence. With no cached comment, press
   `g`; verify one bounded comments request and the latest trusted match, no-match
   and unverified-author explanations. Try `g` on Issue during a pending Unblock
   read, and vice versa; neither queues a second read. Check cache revisits,
   explicit retry and the shared cooldown. Move to an eligible row, or publish a
   refresh that resumes the selected item; Unblock must disappear and return to
   Log if active. `4` must do nothing outside Needs attention.
7. Start a pending or hung description or comment load and press `q`; repeat with
   `Ctrl-C`. In an attached view, confirm that `q` replaces any overlay with the
   centered shutdown screen and waits for the run's report, label transitions and
   cleanup before exiting 0. Check the item reference, agent and updating elapsed
   assignment time, plus the idle `No run in progress.` case. Ctrl-C from the normal
   view or shutdown screen must show the Stopping screen, terminate owned execution
   and release its claim before exiting 130. Repeated stop keys must do nothing.
   Both screens must retain terminal ownership until launcher exit; confirm the
   final launcher message, reaped request processes, normal terminal input, cursor
   and alternate-screen restoration. A standalone replay observer closes only
   itself promptly on either key.

8. Exercise `launch`, `--once` and `launch N`, exact-session attachment with another
   fresh snapshot present, stale/version errors, restart, view crash/kill, SIGTERM
   drain and SIGHUP. During SIGTERM drain, confirm the assignment keeps `■` and
   `stopping`, its detail changes from `attempt N` to `agent · this launcher ·
   finishing run` (shortened to fit), and Eligible's rule includes `not claimed
   while stopping`. Ready/recovery rows must show `held`, while delayed rows
   keep `backoff`/`waiting`. Select the assignment and check
   `■ Stopping after this run (SIGTERM) · no new claims`; select another item
   and check its normal Log status. The footer must read `stopping`, and the
   launcher must stay alive until the current run finishes without claiming new
   work. Confirm final launcher output, unchanged exit codes, reaped UI
   and request processes, and cleaned owned agent groups.

9. Publish both an installed-release update and a checkout update in the replay
   snapshot. Confirm one yellow row above both panes and the shared item header,
   with the activity footer still on one row at the bottom. Check the release age
   at the right and the checkout's restart instruction. Resize narrower and wider;
   neither variant may wrap or take focus, and pane selection and paused reading
   positions must survive. Clear the result and confirm the row disappears.
   Confirm plain output prints each new text once across repeated polls, and
   a pending or failed check leaves the previous successful notice in place.

10. Resize to 109×32 and 110×31, then 80×24 and 60×16. Work must fill the full width
   with single-line live and Recent activity rows, shortened titles and aligned
   states; sections, counts, idle/stopping states and the upper/lower split stay
   intact. Move across the split with arrows. Open live and recent rows with
   `Enter`, switch tabs, and return with `Esc`; reopening must keep the last tab
   and selection. Check the full-width item header, tab bar and Log status line,
   all log/Issue keys, and narrow list, item and paused footer keys.
   Pause and scroll a log, toggle raw mode, and resize across 110×32 in both
   directions with focus first on Work, then on the item pane. Selection, tab,
   follow/pause and log position must survive; widening keeps focus on the pane
   that was showing. Open help/raw access and confirm `Esc` closes it before
   returning to Work. At 59×16 and 60×15, check only one centered enlargement
   request appears, including with an overlay open. Grow back and check the
   previous state returns. Quit below the floor with `q`; repeat with Ctrl-C,
   checking the shutdown screens, stop behavior and terminal restoration as above.

11. In iTerm2, ordinary clicks on the shared header's `#N` and `⌥N` references
   must open the selected issue and PR in the attached session's repository in
   the default browser. Change selection and repeat on Log, Issue, Runs and
   Unblock, in the combined and narrow item layouts, including a shortened title.
   A title click must open nothing; also check metadata, the rule and blank space.
   Command-click alone does not pass this check. Record any failure to deliver
   mouse clicks or open the browser as a terminal/host limitation.

Record the terminal type, dimensions, replay or live source, exercised controls,
restoration and launcher-isolation result in the implementation PR. Owned real
PTY checks count as terminal execution; headless screenshots alone do not.
