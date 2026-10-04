# Terminal view: reference design

This is the target look for the terminal view that `ub-agents launch` opens. It
guides the polish issues (#159–#166). [terminal-view.md](../terminal-view.md)
describes what the view does today. When an issue lands, update both files.

The view stays read-only. It reads the launcher's local session snapshot and run logs
and makes no GitHub calls except `g` on the Issue and Unblock tabs. It has no workflow controls,
so there is no retry key and no filter. `q` quits the view and stops the launch, like
Ctrl-C (#182).

## Layout

Two panes at 110×32 and above: **Work** on the left (about 50 columns) and the
selected item on the right, with tabs. A one-line footer sits below both. The
focused pane has an accent border with its title set into the border.

```text
╭─ Work · pass complete ───────────────────────────╮
│ Running · 1                                     │
│ ⠹ #163 Compact timestamped Claude log lin… 04:12 │
│   implementer · this launcher · attempt 1        │
│ Needs attention · 2                             │
│ ? #156 Drop old coordination record form… parked│
│   integrator                                    │
│ ✗ #126 Codex structured run-log form… failed 3/3│
│   implementer · 3/3 failures                    │
│ Eligible · 2                                    │
│ ● #165 Render issue descriptions as Markdo… next │
│   implementer                                   │
│ ● ⌥170 Compact timestamped Claude log li… ready  │
│   reviewer                                      │
├─ Recent activity ─────────────────────── 4 today ┤   (lower half, always shown, dimmed)
│ ✓ ⌥167 Group the work list into sections  merged │
│   integrator · 11:52 · squash-merged             │
│ ✓ ⌥167 Group the work list into sectio… approved │
│   reviewer · 11:38                               │
│ ✓ #159 Group the work list into sect… handed off │
│   implementer · 11:20 · opened ⌥167              │
│ ✗ #126 Codex structured run-log formatti… failed │
│   implementer · 10:02 · timed out after 180m     │
╰──────────────────────────────────────────────────╯
```

## Work pane

The upper half holds Running, Needs attention, Eligible and Waiting, in that
order, each with its current row count. Running always appears with only this
launcher's assignment and a count of 0 or 1. With no assignment it shows a dim
`Idle · nothing eligible for this launcher` placeholder, which has no item content.
Other launchers' claims are omitted; their runs remain in an item's Runs tab.
Other empty sections are hidden. It scrolls
on its own when it overflows; the pane keeps its current width for the row change
(#160), independently of the roughly 50-column design canvas above.

Every live row has two compact lines. Line 1 contains the glyph, item reference,
title shortened with `…`, and a short right-aligned state:

| Row | Glyph | Right column |
| --- | --- | --- |
| This launcher's assignment | `⠹` spinner, `■` stopping | Elapsed claim time (`04:12`, `1:04:12`), `claiming` before the claim is known, `stopping` |
| Needs attention, parked | `?` | `parked` |
| Needs attention, blocked | `!` | `blocked` |
| Needs attention, attempt limit reached | `✗` | `failed F/M` |
| Eligible, in planned order | `●` | `next` on the first row, otherwise `ready` or `recover` |
| Waiting | `◷` | `backoff`, or `waiting` for runtime, dependency and milestone waits |

Line 2 is indented and contains the agent, `this launcher` for the assignment,
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
brightness. It never collapses. Rows that do not fit are cut from the oldest end.

| Glyph | Meaning | Right column |
| --- | --- | --- |
| `✓` | accepted outcome | `merged`, `approved`, `handed off`, `prepared` |
| `✗` | failed run | `failed` |

Recent activity rows keep their two-line layout: glyph, item reference, shortened
title and result; then agent · time · summary. A row keeps its place and selection
across polls. An item appears once in the live sections.

The pane title shows the pass state: `Work · pass complete`, or a dim `pass partial`.

## Item pane

Tabs `1 Log  2 Issue  3 Runs`, plus `4 Unblock` when the item needs attention, then `│ Formatted  Raw u` on the Log tab. Each tab
starts with the same item header: `#N title` (issue) or `⌥N title` (PR) in bold,
then agent · runtime · attempt · PR in dim text and a dashed rule. Missing values
are omitted. The running assignment shows `attempt N`; planned work shows
`F/M failures`. A session outcome's linked PR shows as `⌥N` (#162).

### Log

```text
 1 Log   2 Issue   3 Runs  │  Formatted  Raw u
 #163 Show Claude log entries as compact timestamped lines
 implementer · codex gpt-6.1-sol xhigh · attempt 1
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
 ⠹ implementer running · no outcome reported                  1 earlier run
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
 1 Log   2 Issue   3 Runs
 #156 Drop old coordination record formats                       (bold)
 issue · ready · prepared by issue-preparer                       (dim)

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
The source and age line is last.

### Unblock

Only for items in Needs attention. It shows the action-needed comment that parked
the item, so a team member sees at once what has to happen. Acting on it from the
view comes later; for now the tab is read-only.

```text
 1 Log   2 Issue   3 Runs   4 Unblock
 ⌥168 Render Issue descriptions as Markdown                      (bold)
 integrator · blocked · waiting 24m · since 12:12                (dim, "waiting 24m" red)
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 Local CI not run for ea4068a, so no signoff gate. `mise run ci ea4068a…`
 failed: mise reports this worktree's mise.toml is not trusted. Running
 `sh bin/ci.sh SHA` was denied by the session permission policy. Other gates
 pass: PR head matches the candidate, it is mergeable on top of main, the
 reviewer approved this SHA, and the changelog entry is accurate.

 Needs: trust mise.toml for integrator worktrees, or allow bin/ci.sh, then
 re-run integration.                                             (bold)

 Candidate ea4068a · review: no decision · CI: no checks or statuses   (dim)

 After resolving the blocker                                     (heading, accent color)
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ ub-agents retry --number 168 --agent integrator --reason "Human resolved…" │
 └────────────────────────────────────────────────────────────────────────────┘
 Restore a matching trigger if absent: `ready-to-merge`; remove any stop label.
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 Source: action-needed comment · 12:12 · snapshot                (dim)
```

The body is the latest `<!-- ub-agents:action-needed RUN -->` comment on the item,
rendered as Markdown like the Issue tab, without its marker and Claim/Outcome
links. The header gives the agent, the state and how long it has waited. For an
item this launcher parked the text comes from the snapshot; for one parked
elsewhere, `g` loads the comment from GitHub.

### Runs

The item's history, oldest first: who filed the issue, then every run from any
launcher with when, result, agent and summary, where it ran, and the outcome. For a
PR the first row is the issue it closes.

```text
 1 Log   2 Issue   3 Runs
 ⌥167 Group the terminal view's work list into sections          (bold)
 closes #159 · filed by bk-one · 3 runs                          (dim)
 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 when        result  agent · summary                         where         outcome
 2 days ago  ✓       filed by bk-one                         GitHub        filed
 52 min ago  ✓       implementer · sections from snapshot …  bens-macbook… handed-off
 34 min ago  ✓       reviewer · changelog entry tightened    uberblick     approved
 20 min ago  ✓       integrator · squash-merged at d41f0a2   this machine  merged
```

- `when` is relative (`just now`, `20 min ago`, `3 h ago`, `yesterday`, then a date).
- `where` is `this machine` for this host and the launcher's host name otherwise
  (dim), in a fixed 14-column slot: the domain part is dropped (`build-01.tail9c.ts.net`
  shows as `build-01`) and longer names are shortened with `…`.
- Result glyphs: `✓` green, `✗` red, spinner for a run in progress.

## Footer and states

```text
 ub-agents v0.1.11 · next poll 26s     ↑↓ select ⏎ open 1-3 tabs ? keys q quit

Paused log (pill at the bottom right of the log output, above the run status;
footer keys switch to log keys):
                                                              ⏸ PAUSED · 37 new ↓ · f follow
 ub-agents v0.1.11 · next poll 27s     f follow h older u raw PgUp/PgDn scroll ? keys q quit

Update available (yellow banner above both panes; the view is otherwise unchanged):
 ⬆ ub-agents 0.1.12 is available · you run 0.1.11 · brew upgrade ub-agents, then restart the launcher   released 2 days ago
 ⬆ This launcher runs code 3 commits behind origin/main · restart the launcher

Stopping:
 ub-agents v0.1.11 · stopping                                ↑↓ select 1-3 tabs ? keys q quit
```

The update banner is one yellow row above both panes and the shared item header,
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
  full width and Esc returns. The footer shortens to `v0.1.11 · poll 26s`.
- `?` lists every key. The footer shows only the main ones.

## Colors

```text
Colors, as Textual theme variables with these dark-theme values:
  background #0d1016 · panel/footer #161a22 · text #d4d9e1 · dim #6b7484
  accent (focus border, pane titles, launcher lines, ⌥) #b79cff · selection row #1b2030
  Running #6cb6ff · Needs attention #ff8b7f · Eligible #7ee2a0
  diff + #7ee2a0 · diff - #ff8b7f
Focused pane: accent border with its title in the border ("Work", "Log", "Issue"); unfocused: #2a303b border.
Tab bar: active tab inverted (dark text on light), others dim; "Formatted" underlined in accent when on.
```
