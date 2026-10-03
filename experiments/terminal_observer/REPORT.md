# #97: launcher-local output formatting decision

**Recommendation: proceed with constraints.** A separate local view can make
Claude messages/tools/results and Codex human text readable while reusing one
launcher's observations. The experiment demonstrates this with recorded subsets,
synthetic replay and an actual Loop path using an owned dummy worker. It does
**not** establish real runtime integration or usability in the operator's terminal.
Keep PR #99 draft with `Refs #97`, for human assessment only.

This reassessment follows the owner's launcher-local scope. The prior no-go
assessment concerned independent polling, complete history and production shipping.
Those are not prerequisites here. The [earlier report](REPORT.previous-scope.md)
and quota/dependency evidence remain archival; neither audit nor live runtime
probes were repeated. Production code, flags, dependencies, README, public docs
and changelog remain unchanged. No release-facing changelog entry is warranted.

## Evidence and verdicts

[Runnable replay/harness instructions](README.md),
[launcher-local measurements](evidence/local-validation.json), and the existing
[adapter/regression evidence](evidence/validation.json) make these results reviewable.
Screenshots are headless Textual captures, not photographs of an operator session.

| Decision criterion | Verdict | Demonstration and boundary |
| --- | --- | --- |
| Claude whole-message output | **demonstrated** in synthetic replay; recorded subset | [Messages, tools and results](evidence/local-live-claude.svg) decode production-style `stream-json --verbose` without partial-message flags. The 49,439-byte tool result stays one structured record, with visible head/tail shortening. Multiline results, tool errors, runtime errors, unknown records, malformed shapes and ordinary text are exercised. [Three real sanitized records](evidence/local-claude-recorded.svg) contain assistant/tool/result shapes; their invocation differed from production and the stream is curated. Complete real current-format output is **unverified**. |
| Codex current human text | **demonstrated** in synthetic replay | [Human-text output](evidence/local-codex-human.svg) preserves compact ordinary lines, command text and diagnostics without invented structured events or `--json`. A 42,032-byte line shortens visibly. The existing real Codex recording is four curated **optional JSON** records ([replay](evidence/local-codex-recorded.svg)); real current human-text recording is **unverified**. |
| Streaming/partial/oversized content | **demonstrated** in replay; readable structure above 128KiB **disproved** | Split records, UTF-8, a partial preview, multiline content and mixed text are covered. Above 128KiB the adapter deliberately uses labeled raw fragments, not reliable structured formatting. Full raw bytes remain in the file; producer timestamps are unavailable in the real recordings. Live labels are **capture time at observer read**, and historical bytes stay untimed. |
| Scroll, follow, panes and narrow resize | **demonstrated** in headless pilot | [Paused append](evidence/local-paused.svg) holds position; follow returns to the consumed end. Selection/panes and a [72×24 resize](evidence/local-live-narrow.svg) work while output grows. Narrow labels/shortcuts still clip. Bounds can evict paused scrollback. Actual operator readability/accessibility is **unverified**. |
| Responsive bounded processing | **demonstrated** for the short synthetic flood | A 2,088,000-byte/24,000-line burst had maximum log-tick time **64.3ms**, median **16.9ms**; tab dispatch **9.2–60.8ms**, maximum 20ms-heartbeat gap **105.9ms**. Tracemalloc/debug/pilot add overhead. The multi-key sequence plus resize took **12.0s including pilot idle waits**, not measured terminal key latency. Retained traced growth was **1.93MB**, peak **11.19MB**; overall RSS, prolonged memory plateau and slow-filesystem UI responsiveness are **unverified**. |
| Consuming sustained output faster than limits | **disproved** | The same burst left **787,785 bytes** unread/queued at the checkpoint ([flood view](evidence/local-flood-narrow.svg)). Bounded local lag preserves the worker/raw file but cannot promise real-time consumption at arbitrary throughput. Follow follows consumed data. |
| Existing observations, zero additional background calls | **demonstrated** in the single-launcher harness | UI off/on each made **70 recording-transport calls**, in identical method/endpoint order, with identical durable results. Startup, redraws, timers, tailing, loaded navigation and resize added **0**. Only plan #1 was yielded; 29 other triggered fixture plans stayed unevaluated. A deliberate navigation-read defect made **3** extra calls and failed the zero-call assertion. This is offline recording transport, not a new live quota audit. |
| Optional explicit missing details | **demonstrated** in the harness | [Loading](evidence/local-detail-loading.svg) leaves navigation/worker active. One explicit `o` caused **one GET** for the selected item; [cached revisit](evidence/local-detail-cached.svg) caused **0**. Loaded details caused **0**. HTTP 429 stops further reads until reset; errors remain cached without automatic retry. A 20-open session cap rejects the 21st request. Real network/UI detail loading is **unverified**. |
| Local recent activity and outcome authority | **demonstrated** in the harness | [Unaccepted report](evidence/local-unaccepted.svg) and [accepted finalized result](evidence/local-accepted.svg) remain distinct. Twenty bounded local released runs can survive disappearance from the next discovery, without repository history reads. The launcher’s own [finalized human handoff](evidence/local-human-handoff.svg) stays visible in Needs attention alongside its accepted history, without waiting for another discovery. The retained blocker-precedence regression still passes. Complete repository history and cross-restart persistence are not required or claimed. |
| Closure, drain, interrupt and worker isolation | **demonstrated** with owned dummy processes | A separate snapshot-fed OS process in an isolated PTY exited 0 on both q and Ctrl-C while the launcher worker continued, adding **0** transport calls. Actual Loop stop/interrupt callbacks preserved the same released-success/accepted and released-retry/unaccepted results with UI off/on; owned process groups ended empty. Disk publication and tap-callback failures did not veto worker success. Real launcher/runtime attachment, actual OS signals with this tap and an attached foreground UI are **unverified**; existing repository signal tests still pass. |

The adapter keeps 200 entries, 2,048 displayed characters/entry and <128KiB of
unfinished-record data plus one 32KiB read. Each 100ms tick parses at most 128
records and formats at most 32; RichLog retains 400 rendered lines. A 32KiB burst
of empty lines takes **256 polls**, proving the per-callback parse bound rather
than silently freezing on tens of thousands of records. Shortening/eviction and
the full raw path are visible. Raw-file capacity/retention follows existing behavior.

## Smallest experimental seam and costs

`local.py` supplies an experiment-only `TapLoop`: it yields the existing iterator
unchanged and observes successful coordinator returns/mutated arguments. It
makes no GitHub calls, does not exhaust discovery, and does not accept outcomes.
`harness.py` uses actual discovery/claim/supervision/finalization/release with the
existing recording fake extended to hold writes. Checkout refresh is stubbed and
an owned Python process writes synthetic runtime bytes; the dummy's report is
simulated through the transport. No real assignment/LLM was launched.

The tap publishes at most 64 encountered rows/details and 20 local recent runs
through one pending snapshot to an atomic file. Coalescing under a blocked writer
kept the newest snapshot and did not wait on disk. With 64 collected details,
measured observation callbacks were **0.47ms median / 0.82ms maximum** in the
short fixture benchmark. Files are capped at 2MiB; malformed/unavailable snapshots
show stale/unavailable state. Explicit detail results/errors are separately cached
for at most 20 opens, with 8,192-character text limits plus a shortening notice. No extra background
budget, ETag cache sharing or GraphQL allocation is needed for this path.

This seam depends on seven coordinator method contracts and copies a small
presentation of plan/history fields. Recovery and configuration reload behavior
with the tap remain **unverified**. Coalescing can omit transient states; it is a
last-observation projection, not an event journal. Local tail file IO is still
synchronous in the view and may stall on a slow filesystem, while the separate
worker remains independent. Quitting during an explicit network read may wait
for its 20s timeout as the thread shuts down; this case remains unverified. A maintained production seam has not been implemented.

Run the view on the recorded launcher host, normally as the loop user on
`uberblick`, where its projection and lease `log_dir/process.log` are readable.
Another launcher's claim is only observed ownership and offers no log in this
local view, even on the same host. Permission failures are shown; no remote
transport or permission change is proposed. Target-terminal deployment remains
**unverified**. No operator logs/processes/worktrees were used by this continuation.

Textual **8.2.8**, the existing experiment pin, provides the
[scrolling log widget](https://textual.textualize.io/widgets/rich_log/) and
[headless pilot](https://textual.textualize.io/guide/testing/).
The prior inventory remains **nine extra UI distributions / ~19.7MiB**. Adapter
maintenance is required with either presentation: Claude content/tool/result shapes
and raw fallback; Codex compact text and optional JSON compatibility. Future
shipping through the tap would still require maintaining pinned resources or a
separate managed package. Packaging was not changed or researched again.

The standard-library `pretty.py` remains the lower-cost option for the primary
formatting question, alongside `launch.log` and raw tails. Textual adds combined
selection, issue/run context, pause/follow and wrapping; an operator benefit over
separate panes is **unverified**. Existing `status` is useful for its ordinary
repository scan, but it is not the input to this revised view. The recommended
constraints are: local projection only, optional explicit detail reads, raw fallback
and visible bounds/staleness, a separate process, and honest real-runtime/terminal
validation before any production decision. This report authorizes no follow-on
feature, ticket, packaging change, merge or release.

## Validation and effort

This continuation passed **514 repository tests** (38.260s), configuration and
whitespace checks, **ten launcher-local checks** (35.999s), **nine retained
experiment checks** (22.634s), and both former-bug mutations. The zero-request
mutation was also rejected. Candidate SHA and CI results are named in PR #99's
body; CI runs package checks only, not the experiment. Every owned worker,
publisher, subprocess and check was awaited. No live probe/quota audit was run.

| Checkpoint (UTC) | This continuation elapsed | Cumulative effort including prior 70m | Remaining 240m allowance |
| --- | ---: | ---: | ---: |
| 2026-10-03 10:21:07, setup | 0m29s | 70m29s | 169m31s |
| 2026-10-03 10:39:04, evidence | 18m26s | 88m26s | 151m34s |
| 2026-10-03 10:44:03, findings | 23m25s | 93m25s | 146m35s |
| 2026-10-03 10:48:25, checks | 27m47s | 97m47s | 142m13s |
| 2026-10-03 11:03:34, final human-handoff check | 42m56s | 112m56s | 127m04s |

Run 3 started **2026-10-03 10:20:38 UTC**. Findings began well before the last
30 minutes of the allowed continuation and the separate 180-minute run timeout.
Conservative final charge reserved for all setup, code, checks, findings, CI and
handoff: **50 minutes this continuation; 120 minutes cumulative; 120 minutes
remain**. The final PR/issue handoff confirms the charge and actual elapsed time.
Unused budget is not a reason to run more probes or claim unverified results.
This revised spike is ready for human assessment with the installed launcher's
`ub-agent report --status blocked`; PR #99 stays draft and issue #97 stays open.
