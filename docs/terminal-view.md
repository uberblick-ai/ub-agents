# Local terminal view

`ub-agents launch`, `launch --once` and `launch N` open a read-only view of their
own launcher when stdin and stdout are terminals. Use `--no-ui` for plain line
output. Pipes, CI and services always get plain output. The view has no workflow
controls: it shows what the launcher is doing, and you act through the CLI or
GitHub.

## What the view reads

The view runs in its own process on the launcher's host. It reads the launcher's
[session snapshot and item history files](session-snapshots.md), run logs and run context from the
control checkout. If the snapshot is missing, stale, ended or from another
ub-agents version, the launcher prints one line and continues with plain output.
The view opens before the startup checks and shows `checking setup`. Quitting then
claims nothing.

Everything comes from local files except an item's description and its
action-needed comment. The view reads those with your existing `gh` login, and
only when:

- you open Issue (with `2`, a click, or by selecting another row while Issue is
  open) and no description is available locally or from an earlier read;
- you open Unblock and the snapshot has no notice for the item;
- you press `g` on Issue or Unblock.

Results stay in memory while the view runs, so revisiting an item reads nothing.
One read runs at a time. A failed read is retried only when you press `g`. After
a GitHub rate limit, Issue and Unblock wait for the reset. Nothing else reads
GitHub: not attaching, selecting rows on other tabs, redraws or timers.

## Installation

The view is part of every install: `brew install uberblick-ai/tap/ub-agents`, or
`pip install -e .` (or `mise run setup`) in a checkout. Both install Textual
8.2.8 alongside PyYAML.

## Using the view

### Layout

At **110 columns × 32 rows** and above, you see two panes side by side. Work is
on the left, one third of the width within 46 to 64 columns. The selected item's
tabs fill the rest. The focused pane has an accent border.

Below 110 columns or 32 rows, one pane fills the terminal. Work shows first.
`Enter` opens the selected row's tabs, and `Esc` returns to Work with the same
row selected. Resizing keeps your selection, tab, follow state and log position.

Below **60 columns × 16 rows**, the view only asks for a larger terminal. `q` and
Ctrl-C still work.

Two one-line banners can appear above the panes:

- **Update.** For an installed release, the launcher checks GitHub at startup and
  at most once a day, and the banner names the upgrade command. For an editable
  install from the control checkout, the banner asks you to restart when the
  launcher's normal fetch finds commits its running code lacks. The view reads the
  result from the snapshot. Restart the launcher to clear it.
- **Health.** The latest [agent health check](configuration.md#project-health-checks)
  failure or recovery.

The default theme is dark, with a purple accent, blue Running, red Needs
attention and green Eligible. Colors follow the Textual theme, so `textual-light`
also works. `NO_COLOR=1` renders the view in monochrome. The terminal title shows
`ub-agents launch — OWNER/REPOSITORY`.

### Work

The Work pane lists this launcher's work from the snapshot in three sections.
Running always shows. Empty sections are hidden.

| Section | Shows |
| --- | --- |
| Running | This launcher's current assignment, or one dim idle line |
| Needs attention | Plans parked by stop labels or approval gates, blocked plans, and plans that reached their attempt limit |
| Eligible | Up to ten items in claim order: ready and recovery plans first, then delayed plans |

Eligible's heading counts every eligible item, for example
`Eligible · 23 · showing 10`. [Selection order](coordination.md#selection-order)
defines claim order. Plans claimed by other launchers and plans waiting for
blockers do not appear in Work. Runs by other launchers still show in an item's
Runs tab.

The idle line reads `Idle · polling` until a complete pass finds nothing this
launcher can claim. Then it reads `Idle · nothing eligible for this launcher`.
When usage pauses hold eligible work, it names the paused CLIs instead.

Each row starts with a glyph, `#N` for an issue or `⌥N` for a pull request, and
the title. A right column follows:

| Row | Glyph | Right column |
| --- | --- | --- |
| This launcher's assignment | Spinner, or `■` while stopping | Elapsed time (`04:12`), `claiming` until the claim time is known, or `stopping` |
| Parked by a stop label or approval gate | `?` | Waiting time, in red |
| Blocked | `!` | Waiting time, in red |
| Attempt limit reached | `✗` | Waiting time, in red |
| Eligible, ready or recovery | `●` | `next` for the next claim, then `ready` or `recover` |
| Eligible, delayed | `◷` | `backoff`, a usage pause such as `claude paused for 1d 4h`, or `waiting` for health checks and other waits |

In the two-pane layout each row has a second, dim line. It names the agent, then
`this launcher` and `attempt N` for the assignment, or `F/M failures` for a plan
that has failed before. When `queue.priority` is configured, the item's priority
word follows. Urgent, high and low are colored. An item with several eligible
agents gets one row that lists them all. In the single-pane layout each row has
one line, and the tabs carry the detail.

Needs attention's second line is `agent · state · reason`. The state is the stop
labels, `blocked`, or `failed F/M`. Several agents stopped on the same item share
one row that lists each agent and state, then their distinct reasons. The reason
and waiting time come from the same source as [Unblock](#unblock). Waiting time
counts up while the view is open: minutes below an hour, hours below two days,
then days.

While a planning pass runs, the Work title reads `Work · pass partial`. Rows from
the previous pass stay until the pass replans them or completes. Items seen
closed or merged leave at once. After each run, the launcher replans that item
before it claims again, and its rows update immediately.

When the launcher is stopping after its current run, for example after SIGTERM,
Eligible's heading adds `not claimed while stopping`. Ready rows show `held`, and
the footer reads `stopping`.

**Recent activity** fills the lower half of Work. It lists up to 20 recorded
outcomes from this session, newest first, with a green `✓`, red `✗` or neutral
`○`. The detail line shows `agent · time · summary`, adding `opened ⌥N` when the
run handed off to a new PR. Each half scrolls on its own. Arrow keys move between
them.

Until you pick a row, the view selects the launcher's own run whenever one
starts. Your selection, tab, focus and log position survive refreshes. A
selected row that leaves Work stays in the right pane as an earlier observation
until you select another.

### Item tabs

The right pane has `1 Agent`, `2 Issue` and `3 Runs`, plus `4 Unblock` while the
selected row is in Needs attention. Every tab starts with the same header. The
first line is `#N` or `⌥N` and the title. The dim second line joins the agent,
runtime (`cli model effort`), attempt or failure count, and any linked PR.

Click `#N` or `⌥N` in the header to open the issue or PR in your browser. This
needs a terminal that passes mouse clicks to the app. The inert
`Formatted  Raw (u)` indicator beside the tabs shows the log mode.

#### Agent

Agent shows the selected run's local `process.log` and follows new output.
Scrolling up or pressing `f` pauses. The page then stays still while output keeps
arriving, and a pill at the bottom shows `⏸ PAUSED` with the unread count. Press
`f` again to jump to the latest output.

- `h` reads an older page, back toward the start of the file.
- `g` reloads the log at its end and follows it.
- `p` shows the full file path and byte diagnostics. Open that file in a pager to
  read everything.
- `u` switches between formatted and raw output.

Formatted mode turns Claude and Codex output into a compact transcript:

```text
          · earlier output skipped · h older
 07:41:18 ▸ Read packages/hub/src/directory.ts
 07:41:40 Both paths rebuild stubs independently.
 07:41:41 · I'll check the callers before editing the shared helper.
 07:42:11 ▸ Edit packages/schema/src/directory.ts +48 -0
 07:42:30 ▸ Bash pnpm test --filter schema · 1m
 07:43:31   ✗ Exit code 1
 07:43:32 ✓ run finished
```

- Times are local. `~07:42:30` marks a supervisor observation time for a record
  without its own timestamp (including Codex), or the view's capture time for
  live output. These times remain available when reopening a run, paging back
  with `h` or reloading with `g`. Runs supervised before timing was recorded have
  no historical times. Producer timestamps appear without `~`; continuation
  lines keep a blank time column.
- `▸` is a tool call with its main argument. Edit and Write add green `+N` and
  red `-N` line counts. `· 1m` is a Claude call's elapsed time.
- Assistant text is italic. A dim `·` line is a thinking summary, an unfamiliar
  record type, or a notice about skipped or omitted output.
- A red `✗` line is a failed tool call, command or run, with its first error
  line. `✓ run finished` marks a successful end.

Successful tool output, IDs and usage appear only in raw mode. Other runtimes
show raw lines.

Below the log, a status line shows the agent, its process or plan state and the
reported result, with `N earlier runs` at the right.

#### Issue

Issue shows the row's state and reason, then the item's description as Markdown.
Links, images and HTML stay as text, and nothing opens or loads from them. A
section headed `Pointers` moves into a collapsed **Pointers** fold below the
body. Long descriptions are shortened with a notice.

The description comes from the run's `context.json` or the snapshot when either
has it. Otherwise the view [reads GitHub](#what-the-view-reads). A dim line names
the source and its age. For a Needs attention row, Issue also shows the Unblock
content below the description, fully expanded.

#### Runs

Runs reads the selected item's local history file and shows every known run,
oldest first, including runs by other launchers. A dim line above the table gives `closes #N` for a PR, `filed by
LOGIN` and the run count. When the filing is known, the first row shows who filed
the issue and when.

Each run row has:

- when: the outcome time, or the claim time while running, as `just now`,
  `N min ago`, `N h ago`, `yesterday`, `N days ago`, then a date;
- a green `✓` for success, a red `✗` for a failed, rejected, blocked or abandoned
  run, or a spinner while it runs or awaits acceptance;
- the agent and summary;
- `this machine`, or the other host's short name;
- the outcome, with `BLOCKED: …` for human blockers and `· N denied` when the run
  recorded [permission denials](coordination.md).

Every row fits on one line. The run count matches the run rows shown, excluding
the filing row. Histories have no run limit and are outside the snapshot's size
budget.

#### Unblock

Unblock appears while the selected row is in Needs attention. Its header line
adds the agent, state, a red `waiting …` and `since HH:MM`. It shows the first
available source, the same one that sets the Work row's reason and waiting time:

1. The item's latest trusted action-needed notice since the last claim or reset.
2. The finished outcome that added a stop label still on the item.
3. The approval gate's reason, with no waiting time.
4. For a blocked or failed plan, the last finished run's reported actions or
   summary.

`ub-agents status` uses the same rule. Only notices from accounts the launcher
already trusts for coordination records count.

A notice shows its reason, asks and numbered options, with the recommended
option first. Its resume steps sit in a collapsed fold:

- on a pull request, the visible line "Merging or closing #N finishes this item;
  nothing else is needed." and a **To send it back to AGENT instead** fold;
- on an issue, a **To resume AGENT** fold.

Approval-gate notices keep their resume steps visible. Supporting detail sits in
a collapsed **Reasoning and evidence** fold. A dim source line gives the notice's
time and whether it came from the snapshot or GitHub. Below it, a dim hint reads
"Or decide in Claude Code or Codex with the ub-agents-unblock skill", with
[`ub-agents-unblock`](https://skills.uberblick.ai/skills/ub-agents-unblock) as a
terminal hyperlink. The hint also appears in fallback, loading and error states.

Without a notice, Unblock marks its content
**Fallback: action-needed comment unavailable.** It shows the plan reason and the
reason from the rule above. For a blocked plan, the same folds hold
`ub-agents retry N --agent AGENT --reason "Human resolved the blocker"`. With
several blocked agents, their commands share one **To resume these agents** fold,
or **To send it back to these agents instead** on a PR. The fallback stays while a
read is pending, and when GitHub has no trusted notice or the read fails.

### Footer

The footer's left side shows the launcher's version, repository and activity:
`ub-agents v0.2.1 · uberblick-ai/ub-agents · next poll 26s`. Activity is
`checking setup`, `polling`, `running assignment`, `stopping`, or a `next poll Ns`
countdown while waiting. Stale, ended and malformed snapshots are labelled there
too. Each active usage pause adds a segment such as `claude paused for 1d 4h`.
[Usage limits](https://agents.uberblick.ai/docs/configuration/runtimes.html#usage-limits)
explains pauses. The right side lists the main keys for the current pane and tab.

`r` starts the next poll now, or refreshes the queue during a continuous launch's
run. It never claims or changes the running assignment. It works at most once
every 10 seconds and shows `poll now available in Ns` until then. During a GitHub
rate limit, the footer shows `rate limited for 12m · r unavailable`.

On macOS, `c` toggles keep awake. The launcher then holds an idle-sleep
assertion, and display sleep stays allowed. It starts off at every launch, and
`☕` appears in the footer while it is on. It ends when you toggle it off or the
launcher exits.

### Keys

Press `?` for every key. `Escape` or `?` closes it. Open a fold by clicking its
title, or focus it and press `Enter`.

`q` stops the launcher after the current run, and Ctrl-C stops it now.
[Stopping and restarting](operations.md#stopping-and-restarting) covers both.

Drag with the mouse to select text. Releasing copies it through OSC 52, and `y`
copies the selection again. In tmux, OSC 52 needs the setting in
[On a server](https://agents.uberblick.ai/docs/guides/add-a-launcher.html#on-a-server).
On macOS, when neither `SSH_CONNECTION` nor `SSH_TTY` is set, the view also
copies with `pbcopy`. Option-drag uses the terminal's own selection.

### Limits and exit

The snapshot bounds what the view can show: ten Eligible items, 20 recent
outcomes and shortened text. The Runs tab lists every run the launcher knows for
the selected item.
[Session snapshots](session-snapshots.md) gives the limits and what is trimmed
first.

Launcher lines keep appending to `.ub-agents/launch.log` while the view is open.
When the launcher exits, the view closes, restores the terminal and leaves the
final launcher message visible. If the view crashes, the launcher prints one line
and continues with plain output.

The real-terminal tests are in `tests/test_view_terminal.py` and
`tests/test_view_unblock_terminal.py`.
