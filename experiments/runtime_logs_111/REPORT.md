# #111: complete runtime logs in the local view

**Hold the combined-view gate.** Complete owned Claude and Codex runs decode
successfully with the launcher's normal output formats. The combined view adds
selection and pause/follow, but bounded history can move paused Codex text and
lose earlier output on revisiting a pane. A benefit over the formatter in an
actual operator terminal remains **unverified**. This draft is evidence for the
owner, not a merge candidate or authorization to implement downstream work.

## Inputs and observation boundaries

The [pinned #99 tree](../terminal_observer/REPORT.md) is reused with source/report
bytes unchanged and only archival SVG trailing whitespace normalized;
`audit` verifies all 50 files against `f9bdba7`. Its existing real recordings are
curated subsets with different flags, so neither was eligible as a complete run.
No other worktrees/operator logs were read. One new read-only probe per runtime
used an owned synthetic workload and the launcher's `command_for`/`supervise`.
The workload reads its own script, prints 240 rows and a 6,000-character payload,
then reads an intentionally missing file. There was no implementation task,
permission expansion, authentication change or retry.

| Recording | Version / invocation format | Completeness |
| --- | --- | --- |
| [Claude](evidence/claude/capture.json) | Claude Code **2.1.287**, `claude --print --output-format stream-json --verbose --model claude-opus-5-5 --effort high` plus narrowly scoped read-only arguments | **Demonstrated:** exit 0, final `result/success`, 18 records, 57,851 sanitized bytes, 16.657s |
| [Codex](evidence/codex/capture.json) | codex-cli **0.159.3**, `codex exec --model gpt-6.1-sol --config model_reasoning_effort="xhigh"` plus read-only sandbox/isolation arguments; no JSON mode | **Demonstrated:** exit 0 and final message, 302 text records, 22,946 sanitized bytes, 22.289s |

Both ended within 120 seconds; both owned process groups were empty after
supervision. [Coverage](evidence/coverage.json) and exact argv/hash/arrival metadata
are committed. All output records are retained after sanitizing paths, identifiers,
opaque signatures, account quota values and credential-like metadata. The runs are complete **fixture
runs**, not complete issue implementations or representative prolonged workloads.
User configuration/hooks/MCP were excluded; operator workers' permission/settings
arguments were not reproduced. Existing authentication was used without reading
or copying it. Process-level failures, transport failures and runtime upgrades are
**unverified**. Official [Codex non-interactive guidance](https://learn.chatgpt.com/docs/non-interactive-mode)
and the installed CLI help were used only to check the read-only invocation.

## Findings on the same input

[Reproduction commands](README.md), [measurements](evidence/comparison.json),
full pretty transcripts and screenshots are offline replay evidence. Combined
captures use a Textual **8.2.8 headless pilot**. Pretty output comes from an **owned
PTY** at each size; its SVGs are modeled autowrap replays of the final viewport.
There was **no actual operator-terminal observation**.

| Finding | Verdict and evidence |
| --- | --- |
| Real messages, Read/Bash and command results, diagnostics and failed read | **Demonstrated** for both complete probes. Claude labels the failed tool result `[error]`; Codex preserves the command's `exited 1` and missing-file text. Neither runtime's success prose accepts a workflow outcome. |
| Long output remains readable | **Demonstrated as a bounded projection**, not full content. Both adapters show head/tail shortening. Claude's 20,540-byte command result becomes at most 2,048 characters, omitting middle rows. Codex prints all 240 row records but shortens its long single line. Both presentations use the same adapter and leave full sanitized bytes in the input file. |
| Formatter fidelity and complete sequential access | **Demonstrated** in owned PTYs: every decoded record is printed, exactly matching the sequential adapter projection at both sizes (Claude 18 / Codex 302). Complete bytes still require the raw file; terminal scrollback retention itself was not assessed. |
| Complete Codex history stays accessible in the combined view | **Disproved** for this modest fixture: 102/302 decoded records are evicted. At 110×32 the initial rendered rows can still include the beginning, but `2` then `1` rebuilds from the last 200 records and loses it. At 72×24 the 400-rendered-line bound also discards earlier rows. [Pane revisit](evidence/codex-110x32-combined-revisit.svg) shows the bounded view. |
| Partial bytes, follow versus paused reading | **Demonstrated in controlled byte replay**, not original producer cadence: the first 60% of the real recording, then 1KiB chunks every 20ms. Partial previews appear for both. Claude keeps the top/shared visible rows; its viewport grows when the partial notice clears. Codex keeps the numeric scroll position while the visible text moves at the 400-line bound. `f` returns both to the consumed end. [Before](evidence/codex-110x32-combined-before-pause-append.svg), [after](evidence/codex-110x32-combined-paused.svg) and paused text files make this reviewable. |
| Combined view helps more than pretty.py | Extra context/navigation controls are **demonstrated** in the reused fixture projection; an operator benefit is **unverified**. Actual launcher attachment, visual comfort, accessibility, external scrollback and prolonged runs were not observed. |

At **110×32**, the combined log has **69×24** cells; pretty uses all 110 columns.
At **72×24**, the combined log has **44×14** cells (only 10 rows while Claude's
partial preview is visible); pretty uses all 72 columns. Claude renders 176 versus
274 rows in the combined view, compared with 136 versus 166 modeled pretty rows.
Codex's full formatter projection is 327 versus 346 modeled rows. The narrower
combined layout increases wrapping and reaches rendered-history limits sooner.
Compare [normal combined](evidence/claude-110x32-combined-tail.svg) /
[formatter](evidence/claude-110x32-pretty.svg), and
[narrow combined](evidence/claude-72x24-combined-tail.svg) /
[formatter](evidence/claude-72x24-pretty.svg). These are layout measurements and
headless artifacts, not an operator usability verdict.

## Presentation and install decision

The **smallest viable candidate is formatter only**, with raw-file access and
visible shortening. For assessment, run the existing standard-library
`python3 -m experiments.terminal_observer.pretty PATH` from this branch. If the
owner chooses that scope, the proposed install route is to bundle the adapter and
formatter modules with the existing ub-agents installation, without Textual or a
second environment. This route is a decision proposal, not implemented packaging.
The combined experiment's install route remains an isolated local venv using its
existing lockfile; no production dependency or installation change is proposed.

Use **72×24 as the tested minimum for the formatter** and **110×32 as the minimum
candidate size for a combined-view assessment**. The combined view functions at
72×24 but leaves just 44 columns and frequently much less reading space. A hard
usability minimum for an actual operator terminal is **unverified**.

**Attaching should show recent output immediately**, with an explicit path to
older/raw output and a visible follow/paused state. The pinned Tail reads from the
beginning in bounded ticks; these small recordings drained in 0.3–1.2s. Immediate
recent attachment for a large existing file is **unverified**, and no seek strategy
was implemented here.

## Validation, effort and handoff

Passed: **649 repository tests** (33.628s, Python 3.14.8/Linux), `ub-agent check`,
whitespace checks, offline coverage/provenance checks and all four PTY/headless
comparisons. Every owned probe, formatter process, supervisor thread and headless
pilot was awaited. CI runs the standard package checks; the experiment is validated
by its documented offline commands. No platform/quota/architecture audit was run.
No changelog entry applies to this research-only branch.

This single attempt began **2026-10-03 12:51:18 UTC**; research closed at
**13:08 UTC**, leaving the final report/check/handoff phase. No earlier #111
attempt is present in the assignment context. **Conservative cumulative charge:
40/90 minutes**, including at least 20 minutes reserved for the report and handoff;
**50 minutes remain**. The draft PR records the publication checkpoint's actual
elapsed time. Neither probes nor audits will be repeated to spend the allowance.

PR remains **draft**, begins `Refs #111`, and is not sent to integration. The
installed launcher's final report is **blocked** for owner assessment. Issue #111
stays open until the owner explicitly accepts a proceed result, combined-view
scope, and presentation/install decisions. If the owner selects formatter only,
revise or cancel affected downstream issues before closing the gate.

**Recommendation: hold.**
