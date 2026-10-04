# Development terminal view

The optional view reads one launcher's local files in a separate process. It uses
no GitHub client, and cannot approve, retry, merge, stop or otherwise change work.
The launcher and base package work without Textual. This development entrypoint
is not a new `ub-agents` command or a supported Homebrew installation route.

Install the extra in a development checkout's own virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[ui]'
.venv/bin/python -m ub_agents.view /path/to/control-checkout
.venv/bin/python -m ub_agents.view /path/to/control-checkout --session SESSION_ID
```

Use the **control checkout**, whose `.ub-agents/sessions/` and `runs/` directories
belong to the launcher, rather than an agent's private worktree. With no ID, the
view opens the only fresh, unended session (including an idle launcher). Otherwise
it lists local sessions as live, idle, stale, ended or malformed and exits.
An explicit ID can open an ended or unavailable session. Missing timestamps are
stale with unknown freshness; a heartbeat older than 30 seconds is stale.

At least **110 columns × 32 rows** are needed for the combined view. The left
pane contains the current assignment, the latest pass (marked partial until
complete) and recent outcomes. Outcome completion and a human blocker are shown
separately: a completed step can still be blocked. Selection and focus survive
refreshes. A selected row that disappears remains an earlier local observation.
Claims held by another launcher show their owner and have no log access.

The right pane has Log, Issue and Runs tabs. Issue uses only the cached snapshot
description or that session's run `context.json`; absent descriptions say they
are unavailable. Runs shows the snapshot's session outcomes, including acceptance
and human blockers. No on-demand GitHub description reads are implemented.

| Key | Action |
| --- | --- |
| `Tab`, arrows, `Enter` | Focus a pane and select a work row |
| `1`, `2`, `3` | Log, Issue, Runs |
| `f` | Toggle follow/pause; resuming loads the latest generation |
| `h` | Read an older bounded page, down to byte zero |
| `u` | Toggle formatted/raw projection of the same page |
| `p` | Show the full raw file path; `Escape` closes it |
| `Page Up`, `Page Down`, `Home`, `End` | Scroll the log; scrolling up pauses follow |
| `q`, `Ctrl-C` | Quit only the view |

FOLLOW/PAUSED, RAW/FORMATTED, snapshot freshness, unread entries and byte lag are
always visible in the footer, including on Issue and Runs. Pausing freezes the
page and its position while ingestion continues. Revisiting tabs or selected
rows restores that page and position. Resizing and raw-mode changes retain the
entry at the reading position, with a proportional position within wrapped text.
Claude uses the #112 formatter. Unknown runtimes, including Codex, use a labelled
plain/raw fallback. Runtime output never establishes a workflow outcome.

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
or grows a backlog. No file writes, process control or launcher imports occur in
the view. Cached own-run readers are limited to 21, matching the session's bounded
20 recent outcomes plus its current assignment.

## Validation

CI runs the base suite without the extra, then installs `.[ui]` and runs the same
suite with the UI regressions enabled. Local checks:

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/ub-agents check
git diff --check
```

An actual terminal acceptance check is also required, separately from headless
Textual pilots or screenshots. In a real 110×32 terminal, attach to a live launcher
or a local replay that appends to a session's `process.log` and publishes snapshots:

1. Confirm follow reaches recent output and the current assignment, partial pass,
   cached Issue and session Runs tabs are readable.
2. Pause, scroll, continue appending more than 200 entries and 400 wrapped lines,
   visit Issue/Runs and another work row, then return. The paused page and reading
   position must remain stable; unread and lag must grow.
3. Read older pages toward byte zero, switch raw mode and resize. Check the byte
   range, omission notices and `p` full raw path. Resume follow.
4. Replace or truncate the replay log. Check generation recovery and refusal of
   older reads from the prior generation.
5. Quit with `q`, repeat with `Ctrl-C`, and verify normal terminal input, cursor and
   alternate-screen restoration. Confirm the launcher/replay process is still
   running and output still grows. Stop only the replay owned by this check.

Record the terminal type, dimensions, replay or live source, exercised controls,
restoration and launcher-isolation result in the implementation PR. Owned real
PTY checks count as terminal execution; headless screenshots alone do not.
