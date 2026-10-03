# #111: Claude-first local terminal view

**Recommendation: proceed technically with the bounded two-pane Claude-first
view. Owner usability, combined scope and installation acceptance remain open.**
Keep #111 open and PR #120 draft; this research is not a production delivery or
an integration candidate. Codex formatting is deferred to [#126](https://github.com/uberblick-ai/ub-agents/issues/126)
and does not force hold.

The owner’s 2026-10-03 decision changes the first supported runtime to Claude.
[The first-pass report](REPORT.baseline.md), its [comparison results](evidence/comparison.json)
and all original recordings/screenshots are retained unchanged. `audit` verifies
them against **1fd246e3aef664b953cfa8fbeff084bac0715019** and the 50 archived #99
files against **f9bdba7796e80c977e398dedc139d0717ba55db8**. New evidence is confined
to [evidence/continuation](evidence/continuation). The bounded view variant is
[view.py](view.py); the original adapters, combined view and pretty-printer remain
unchanged. No architecture, GitHub-cost or runtime-installation research was repeated.

## Evidence and observation boundaries

No additional live probes were made. The two permitted probes remain spent.
The owned [Claude recording](evidence/claude/process.log) is complete: Claude Code
**2.1.287**, exit 0, final `result/success`, **18 records / 57,851 sanitized bytes**,
16.657 seconds. Its [exact invocation and provenance](evidence/claude/capture.json)
use `claude --print --output-format stream-json --verbose --model claude-opus-5-5
--effort high`, without a partial-message flag, plus the recorded read-only
isolation arguments. The probe’s permissions/hooks/MCP configuration differ from
operator workers. This is a captured runtime handling an owned synthetic task,
not a complete issue implementation. All records are retained; private paths,
identifiers, signatures and sensitive metadata were sanitized. Observer arrival
metadata is preserved, but controlled replay is not the original producer cadence.

The retained [Codex recording](evidence/codex/capture.json) is **historical human
text**, codex-cli **0.159.3**, 302 lines / 22,946 sanitized bytes, exit 0. The current
launcher at [861423a / #119](https://github.com/uberblick-ai/ub-agents/blob/861423a2ffde212cf142d33e573690696daf492d/src/ub_agents/execution.py)
uses `codex exec --json`. Human-text evidence validates neither that current format
nor its integration. The archived adapter recognizes some optional item events,
but current thread/turn/error/usage shapes, pairing and JSON display are
**unverified**; #126 owns that gap. The earlier requirement to avoid JSON has been
removed from the active audit. This continuation does not integrate Codex.

[Validation measurements](evidence/continuation/validation.json) distinguish
captured Claude byte replay from the separately retained [synthetic history
load](evidence/continuation/synthetic/process.log). SVGs are **Textual 8.2.8 headless
captures**, not owner-terminal screenshots. Formatter transcripts come from owned
PTYs and are checked against the exact same sequential adapter projection.
[Terminal sessions](evidence/continuation/terminal.json) run the exact preview in
real owned `xterm-256color` PTYs at both sizes, send keys, inspect ANSI output,
and verify exit 0 and alternate-screen restoration. An additional worker-owned
110×32 terminal session exercised the slow preview. No owner terminal was observed;
visual comfort, accessibility and owner benefit are **unverified**, not disproved.
The observation pane uses the inherited seeded single-launcher fixture, not the
operator’s live launcher. Actual production attachment remains a downstream check.

## Comparison and findings

| Same input / behavior | Demonstrated, disproved or unverified |
| --- | --- |
| Claude messages, Read/Bash calls, paired IDs/results, intentional missing-file error | **Demonstrated** in formatter and combined view at both sizes. Tool failure is visibly `[error]`; final runtime success is not a workflow outcome. Transport, authentication and process-level failures remain **unverified**. |
| Long command result and 6,000-character payload | **Demonstrated as shortened projection**: the shared 2,048-character adapter shows head/tail plus a shortening marker. Middle row 120 is absent from both displays, present in the full sanitized raw file. `u` selects bounded raw projection; it does not expose all bytes. |
| Same formatter content | **Demonstrated**: pretty PTY output equals all 18 sequential decoded projections; the variant displays those same projections. Pretty uses the full terminal width and external terminal scrollback. It has no built-in pause, item context or page controls. |
| Full recording under sustained/chunked input | **Demonstrated offline**: 256-byte writes every 50ms for about 12 seconds, from empty file to all 18 records, at both sizes. Partial notices occur, follow ends at the consumed tail. Measured byte backlog is small; UI callback timing excludes painting/terminal transport and is not an end-to-end latency guarantee. |
| Pause while real partial output completes | **Demonstrated**: first 60% of captured bytes, then 1KiB/20ms. Visible rows, entire rendered page and viewport dimensions stay identical while paused; `2` then `1` preserves the position; `f` shows the completed tail. |
| Pause under general parser eviction | Baseline stability **disproved**, variant stability **demonstrated**: 200 initial synthetic Claude-shaped events, then 1,600 more. Before/after text and SVGs show baseline drift, while the variant preserves the paused viewport and all rendered rows despite 1,600 parser evictions, at both sizes. |
| Pane revisits and older history | **Demonstrated**: revisits keep the immutable displayed page. Explicit `h` pages recover all **1,800 events / 630,000 bytes** in nine contiguous source ranges. The parser remains bounded at 200 records; older content comes from disk rather than disappearing silently. |
| Existing-file attachment | **Demonstrated for synthetic 630kB file**: starts at byte **368,200**, reads at most the final 256KiB initially, reaches the tail in roughly two seconds, and can page to byte zero. This is bounded near-tail attachment, not instant attachment. Slow disks and larger/rotating files are **unverified**. |
| Benefit over formatter | Combined context, pause/follow and recoverable history are **demonstrated capabilities**. Owner preference and practical usefulness are **unverified** pending the short manual check. |

The variant freezes the displayed page while ingestion continues, reserves fixed
space for status/partial notices, removes the rendered-row eviction limit within
a bounded page, preserves that page across tab revisits, and adds explicit older
pages. Follow coalesces to recent retained records; it can omit intermediate
records during bursts, which remain accessible through disk pages/raw bytes.
Source byte ranges and parser eviction are visible. Historical pages lose live
observer capture labels when re-decoded; event timestamps are retained where present.

At **110×32**, the variant log is **69×22** cells and the Claude projection is
176 rendered rows; the formatter uses 110 columns (136 modeled wrapped rows).
At **72×24**, the variant log is **44×14** cells and 274 rows; the formatter uses
72 columns (166 modeled rows). The fixed partial/status area prevents paused
reading from growing/shrinking as partial bytes complete. Narrow operation is
technically demonstrated but substantially increases wrapping. See [normal tool
result](evidence/continuation/claude-110x32-command-result.svg), [narrow tool
result](evidence/continuation/claude-72x24-command-result.svg), and synthetic
[before](evidence/continuation/before-synthetic-110x32-paused-after.svg) /
[after](evidence/continuation/after-synthetic-110x32-paused-after.svg).

## Smallest useful delivery and stabilization

Retain the agreed **two-pane local presentation**: current assignment and
observations already queried by one launcher on the left; Claude output and
cached item/run context on the right. Keep the standard-library formatter as a
useful fallback. No fleet view, repository history, new discovery or automatic
GitHub polling is needed. Optional explicit missing-description fetching is not
implemented here. This recommendation preserves combined-view scope; if the
owner instead chooses formatter only, the affected #112–#116 scopes must be
revised before accepting that gate.

Proposed first installation route: package the view with the existing project as
an **opt-in UI extra**, running in its own process; retain the base formatter
without UI dependencies. Owner/maintainer agreement on that route is still owed.
The demonstrated preview uses only this worktree’s isolated venv and the existing
UI lockfile. No packaging, dependencies or runtime installations were changed.

Use **110×32 as the proposed minimum for combined-view delivery**; **72×24** is
tested for the formatter and for narrow combined behavior. Owner usability must
confirm the minimum. Attach near the tail with a visible byte range, explicit
older/raw access and FOLLOW/PAUSED plus RAW/FORMATTED state. A pause must retain
its page and viewport regardless of ingestion, parser eviction or pane visits.

Before production acceptance, stabilize file identity and rotation/truncation
recovery, anchor preservation across resize and raw-mode changes, unread/lag
indicators, and bounded IO/rendering on large files and slow disks. The variant
warns about file changes and blocks older reads against a changed file until
follow resumes, but that path has not been comprehensively validated. Records
above 128KiB use the archived raw-fragment fallback; records above a 256KiB page
can be skipped at the alignment boundary and need raw access. Production must
label those omissions and make older navigation progress without false event
interpretation. Long-running retention, installation/resource compatibility,
actual launcher projection attachment and terminal accessibility remain owed.
These are precise downstream acceptance requirements, not claims this prototype
is ready to ship.

## Validation, effort and handoff

Passed locally on Python **3.14.8/Linux**: **649 repository tests** (34.266s),
development-copy `ub-agent check`, whitespace/scope checks, provenance audit,
Claude/synthetic continuation validation and owned terminal validation. Focused
commands and the exact owner preview are in [README.md](README.md). Every owned
process, writer task and headless pilot is closed/awaited. CI covers the standard
package suite, not these experiment commands; candidate SHA and CI results are
recorded in PR #120. No environment limitation blocked the local checks.

Continuation started **2026-10-03 14:47:03 UTC**; experimental changes closed at
**15:12 UTC**, with **20 minutes reserved for evidence, report and handoff**.
Conservative cumulative effort charge: **85/105 minutes** (**40 prior + 45 for
this continuation**), leaving 20 minutes unspent. The PR records actual final
elapsed time; a retry does not reset this charge. No more probes are permitted.

Only experiment files change. Production code, flags, dependencies, root README,
public docs and changelog remain unchanged; no changelog entry applies to research.
No operator workers, other worktrees, credentials or global settings were touched.
End with the installed launcher’s **blocked** report for owner assessment. Never
mark PR #120 ready or send it to integration; keep #111 open until the owner
accepts the proceed result, combined scope and presentation/install decision.
