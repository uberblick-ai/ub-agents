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

When the session has outcomes, a collapsed **Recent activity · N today** row
follows the sections. N counts cached session outcomes dated today in the
viewer's local timezone; older outcomes remain available when expanded. Select
the row and press `Enter` to expand or collapse it. Expanded outcomes have the
same local log access as other own runs. Expansion survives refreshes; collapsing
while an outcome is selected selects the Recent activity row.

Outcome completion and a human blocker are shown separately: a completed step
can still be blocked. Selection, focus and paused log positions survive refreshes,
including when a row moves between sections. A selected row that disappears
remains an earlier local observation. Claims held by another launcher show their
owner and have no log access.

The right pane has Log, Issue and Runs tabs. Issue first uses the snapshot
description or that session's run `context.json`. A shortened or empty cached
description is available and needs no GitHub read. Runs shows the snapshot's
session outcomes, including acceptance and human blockers.

| Key | Action |
| --- | --- |
| `Tab`, arrows, `Enter` | Focus a pane and select a work row |
| `Enter` on Recent activity | Expand or collapse session outcomes |
| `1`, `2`, `3` | Log, Issue, Runs |
| `g` on Issue | Load the selected item's missing title/body, or retry a failed description read |
| `f` | Toggle follow/pause; resuming loads the latest generation |
| `h` | Read an older bounded page, down to byte zero |
| `u` | Toggle formatted/raw projection of the same page |
| `p` | Show the full raw file path; `Escape` closes it |
| `Page Up`, `Page Down`, `Home`, `End` | Scroll the log; scrolling up pauses follow |
| `q` | Close only the view; launcher continues with plain output |
| `Ctrl-C` | Interrupt the launching process with its normal SIGINT handling (exit 130) |

FOLLOW/PAUSED, RAW/FORMATTED, snapshot freshness, unread entries and byte lag are
always visible in the footer, including on Issue and Runs. Pausing freezes the
page and its position while ingestion continues. Revisiting tabs or selected
rows restores that page and position. Resizing and raw-mode changes retain the
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

Attachment reads near the tail, with a visible byte range. Ingestion reads at
most 32 KiB and processes at most 128 records per update; a bounded first-read
selection avoids replaying a tiny-record history before recent output. Retention
is 200 entries per reader. Older reads inspect at most 32 KiB plus identity
anchors and decode at most 200 complete records and one fragment. Rendering keeps
at most 400 wrapped lines, discarding whole older entries with a visible boundary.
Press `h` to recover those bytes from disk. A paused page is independent of reader
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
.venv/bin/python -m unittest discover -v
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
   recent output and cached Issue and session Runs tabs are readable.
   Include today's and older outcomes; check the collapsed Recent activity count
   against the local date. Use `Enter` to expand it, select an outcome and pause
   its log. Publish refreshes and move a selected plan between sections; check
   selection, focus and paused positions. Collapse Recent activity with an outcome
   selected, then expand and revisit it; check header selection and the restored
   paused page. Remove a selected plan and check its earlier observation remains.
2. Pause, scroll, continue appending more than 200 entries and 400 wrapped lines,
   visit Issue/Runs and another work row, then return. The paused page and reading
   position must remain stable; unread and lag must grow.
3. Read older pages toward byte zero, switch raw mode and resize. Check the byte
   range, omission notices and `p` full raw path. Resume follow.
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

Record the terminal type, dimensions, replay or live source, exercised controls,
restoration and launcher-isolation result in the implementation PR. Owned real
PTY checks count as terminal execution; headless screenshots alone do not.
