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
workflow controls. Only an explicit Issue-tab description request uses the user's
existing authenticated `gh` access.

The target look for upcoming changes is in [design/terminal-view.md](design/terminal-view.md).

## Installation

The view is part of every install: `brew install uberblick-ai/tap/ub-agents`, or
`pip install -e .` (or `mise run setup`) in a checkout, which installs Textual
8.2.8 alongside PyYAML.

`ub-agents-ui /path/to/control-checkout --session SESSION_ID` is available for
manual local observation. Without an ID this standalone view opens the only
fresh, unended local session or lists available sessions. Only this manual form
allows an explicit ended/unavailable session; automatic launch attachment is
strict. Ctrl-C in a standalone view closes only that view.

## Using the view

When newer ub-agents code is available, a yellow, one-line banner appears at the
top, above both panes and the shared item header. It takes no focus and truncates
to the terminal width. Installed releases say
`⬆ ub-agents X is available · you run Y · brew upgrade ub-agents,
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

At least **110 columns × 32 rows** are needed for the combined view. The left
pane groups work from the session snapshot into sections, hiding empty sections
and showing each section's row count:

| Section | Work |
| --- | --- |
| Running | The current assignment, once, and plans owned by another launcher, with their owner |
| Needs attention | Blocked plans and parked plans with stop labels or approval gates |
| Eligible | Ready and recovery plans, in the pass's planned order |
| Waiting | Dependency and milestone waits, retry backoff and paused-runtime plans |

Dependency and milestone waits are parked plans whose reasons start with
`Waiting for blockers …` or `Waiting for active milestone #…`. While a pass is
incomplete, a dim `partial` marker appears on the `Launcher work` pane title.
Rows from the previous pass stay visible, with their cached descriptions and item
history, until the new pass completes. Each re-planned item and agent updates in
place, moving sections if its state changes; new rows follow the kept rows in
their section. Completion removes omitted rows and applies the new planned order
within Eligible. A selected removed row remains as an earlier observation.

**Recent activity · N today** always fills the lower half of the Work pane,
including `0 today` when there are no outcomes. N counts cached session outcomes
dated today in the viewer's local timezone. Up to 20 cached outcomes appear
newest first, including older outcomes, as dim two-line rows; the selected row
shows at full brightness. Whole rows that do not fit are cut from the oldest end.
The lower half does not scroll or collapse, and its header cannot be selected.
The live sections fill the upper half and scroll independently. Arrow keys move
between the two halves; `Enter` selects an outcome with the same local log access
as other own runs. With no live rows, the newest outcome is selected first.
A selected outcome pushed out of view remains selected in the right pane.

Outcome completion and a human blocker are shown separately: a completed step
can still be blocked. Until you select a row, the view selects the launcher's own
run whenever one starts. Selection, focus and paused log positions survive refreshes,
including when a row moves between sections. A selected row that disappears
remains an earlier local observation. Claims held by another launcher show their
owner and have no log access.

The right pane has Log, Issue and Runs tabs. Each starts below the tab bar with
the same two-line item header and a dashed rule. The bold first line shows `#N`
for an issue or `⌥N` for a PR, followed by its title. The dim second line joins
the agent, runtime (`cli model effort`), running assignment's `attempt N` or
planned work's `F/M failures`, and a session outcome's linked PR (`⌥N`) with
` · `. Missing values are omitted. Failure counts and the agent's `max-attempts`
come from the launcher's existing coordination reads, within the snapshot limits.
Issue first uses the snapshot description or that session's run `context.json`.
A shortened or empty cached description is available and needs no GitHub read.

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
progress; the agent and a one-line summary shortened with `…`; its host; and its
named outcome or run status. Acceptance and human blockers remain visible in
the outcome column. Relative time steps are `just now`, `N min ago`, `N h ago`,
`yesterday`, `N days ago` within the past week, then `YYYY-MM-DD`. Calendar days
and dates use the viewer's local timezone.

The where column has a fixed 14-column slot. This host reads `this machine`;
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
emphasis, inline code and code blocks. Line breaks, including CRLF and lone CR,
display as real line breaks; tabs are retained. Other control characters stay
visibly escaped. Rich/Textual markup such as `[bold]` stays literal. Links,
images and raw HTML display as text; links cannot be opened with the mouse or
keyboard, and nothing is fetched. The item header, source/age and all notices
remain literal text. Issue does not repeat the item reference or title in its
content. Shortening notices sit outside the Markdown body, including
when a description is cut inside a code fence. Runs and the raw log projection
remain literal text with visibly escaped control characters. Claude's formatted
Log transcript is described below.

| Key | Action |
| --- | --- |
| `Tab`, arrows, `Enter` | Focus a pane and select a work row |
| `1`, `2`, `3` | Log, Issue, Runs |
| `g` on Issue | Load the selected item's missing title/body, or retry a failed description read |
| `f` | Toggle follow/pause; resuming loads the latest generation |
| `h` | Read an older bounded page, down to byte zero |
| `u` | Toggle formatted/raw projection of the same page |
| `p` | Show the full raw file path, byte ranges and retention diagnostics; `Escape` closes it |
| `Page Up`, `Page Down`, `Home`, `End` | Scroll the log; scrolling up pauses follow |
| `?` | Show all keys; `Escape` or `?` closes help |
| `q` | Close only the view; launcher continues with plain output |
| `Ctrl-C` | Interrupt the launching process with its normal SIGINT handling (exit 130) |

The one-line footer shows the snapshot's launcher version and activity on the
left: waiting counts down as `next poll Ns`; other activities say `polling`,
`running assignment` or `stopping`. Stale, ended and malformed snapshots are
labelled there, including the malformed error, without a snapshot-age counter.
Below 110×32, it also shows the minimum-size hint. Main keys appear on the right;
they switch to log keys while the selected log is paused, including on Issue and
Runs. `?` lists all keys in a help overlay.

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
07:41:18  ▸ Read packages/hub/src/directory.ts
07:41:40  Both paths rebuild stubs independently.
          Moving to a shared repairStubs() in schema.
07:41:41  · thinking
07:42:11  ▸ Edit packages/schema/src/directory.ts +48 -0
07:42:30  ▸ Bash pnpm test --filter schema
07:42:31    ✗ Exit code 1
07:43:00  ✓ run finished
```

The time column uses the producer's timezone-aware `timestamp`, converted to local
`HH:MM:SS`. Missing or invalid producer times use a marked capture time
(`~07:42:30`); pre-existing bytes without either time use `--:--:--`. The text
column aligns across these cases. Assistant text is dim and italic; its line
breaks and wrapped continuations align with the text column. All other C0/C1
controls, including terminal escape sequences, remain visibly escaped.

Tool calls show `▸`, the name and the main argument: `file_path` for Read, Edit and
Write, `command` for Bash, and `pattern` for Grep and Glob. Other tools use their
first string input when available. Long or multiline arguments end with `…` and
calls fit one display line. Edit adds green `+N` and red `-N` line counts from
`new_string` and `old_string`; Write adds green `+N` from `content`. Missing fields
have no count. Call IDs and JSON inputs are available only in raw mode.
Counts use LF-separated lines; a trailing LF adds no extra line.

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

Raw mode retains every record, including hidden records. Oversized, split,
unfinished and non-JSON fragments keep their labelled raw display. Unknown
runtimes, including Codex, use the labelled plain/raw fallback (#126). Runtime
output never establishes a workflow outcome.

Log ends with a dashed rule and a one-line status showing the agent and its
process or plan state, plus `no outcome reported` or the reported result and
acceptance. A spinner appears while the process runs. Other cached session
outcomes for the same item appear at the right as `N earlier run(s)`, omitted
when zero. Header, Log notice and status lines are shortened with `…` to fit the
pane's width. A highlighted notice above the log appears only for file or
generation changes, read errors, unfinished records, responses to `h`, or a
runtime's plain/raw fallback.

Only `g` on Issue starts a GitHub read. Attachment, selection, tabs, redraws,
resizes and timers make no GitHub calls. One `gh api graphql` request reads only
the selected issue or PR's title and body, without comments, history, queue scans
or prefetch. No reads or retries happen automatically. There is at most one read
in flight: pressing `g` while any read is pending queues nothing. Input and local
snapshot/log reading continue while it is pending, with a loading notice on Issue.
Closing the view terminates and reaps its owned request processes. A request
supervisor also cleans them up if the view is killed. `q` leaves the launcher
running; Ctrl-C interrupts an attached launcher.

Descriptions show their source and age: snapshot publication time, run context
file modification time, or GitHub load completion time; missing local timestamps
say age unavailable. Each title/body projection is limited to 2,048 characters
plus a visible shortening notice. Successful and failed GitHub reads stay only
in memory, in a 128-item least-recently-used cache shared across rows for the same
repository/item. Revisiting a cached item never calls GitHub; a failed read needs
`g` to retry. Evicted items need an explicit load again. There is no session load
count limit. Each request has a 10-second time limit and a 512 KiB response limit.
After a rate-limit response, Issue shows a global cooldown through the reported
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
and an external pager for the full raw file. Oversized or split records remain
labelled raw fragments; they are never interpreted as complete events.

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
UTC timestamps, to `.ub-agents/launch.log`. Closing with `q` resumes new plain
lines without replaying past output. On launcher exit the view closes, the
terminal is restored, and the final launcher message is visible. A crashed or
killed view produces one diagnostic and resumes plain output. SIGTERM drains the
launcher normally; SIGHUP interrupts it like Ctrl-C. Reopening a closed view from
a running launcher is not supported.

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
   Confirm Running, Needs attention, Eligible and Waiting appear in that order,
   with correct counts and planned order within Eligible. The current assignment
   appears once. Empty sections are hidden. A partial pass has a dim marker on
   the pane title, which disappears when the pass completes. Confirm follow reaches
   recent output and cached Issue and per-item Runs tabs are readable. Check the
   shared two-line header and dashed rule on every tab, omission of missing values,
   and the Log status, spinner, reported acceptance and earlier-run count. In Runs,
   check the filing row, local and foreign hosts, in-progress and completed runs,
   acceptance, blockers, and summary shortening at the minimum width. Select
   another item and confirm its history replaces the prior table without a
   GitHub request; include an item with missing filing data and omitted runs.
   Check the single footer line shows the snapshot's version and activity, including
   the waiting countdown and stopping state, with readable main keys at 110 columns.
   Check stale, ended and malformed snapshots are labelled, with the malformed error,
   and that a smaller terminal shows `minimum 110×32`. There is no snapshot age or
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
   check its earlier observation remains.
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
6. Start a pending or hung description load and quit with `q`; repeat with
   `Ctrl-C`. Verify prompt exit and no request process left behind, as well as
   normal terminal input, cursor and alternate-screen restoration. With `q`,
   confirm the launcher continues and output still grows. With Ctrl-C in an
   automatic launch view, confirm launcher exit 130 and owned execution cleanup.
   A standalone replay observer closes only itself on either key.

7. Exercise `launch`, `--once` and `launch N`, exact-session attachment with another
   fresh snapshot present, stale/version errors, restart, view crash/kill, SIGTERM
   drain and SIGHUP. Confirm final launcher output, unchanged exit codes, reaped UI
   and request processes, and cleaned owned agent groups.

8. Publish both an installed-release update and a checkout update in the replay
   snapshot. Confirm one yellow row above both panes and the shared item header,
   with the activity footer still on one row at the bottom. Check the release age
   at the right and the checkout's restart instruction. Resize narrower and wider;
   neither variant may wrap or take focus, and pane selection and paused reading
   positions must survive. Clear the result and confirm the row disappears.
   Confirm plain output prints each new text once across repeated polls, and
   a pending or failed check leaves the previous successful notice in place.

Record the terminal type, dimensions, replay or live source, exercised controls,
restoration and launcher-isolation result in the implementation PR. Owned real
PTY checks count as terminal execution; headless screenshots alone do not.
