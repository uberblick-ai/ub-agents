# Qwen / OpenCode local review spike (#209)

**Result: inconclusive under the resource budget; keep the PR draft for owner
assessment.** OpenCode ran the local model with tools and the requested context,
but swap growth stopped it before it examined surrounding source, ran checks or
returned a review. This establishes working integration and a resource limitation,
not review quality. No ub-agents runtime integration is recommended from this run.

## Revision and blinding

Target: [ub-agents #237](https://github.com/uberblick-ai/ub-agents/pull/237), the
first reviewed head `5db1bc3f2a155d11d513bc342a14594e24e27de0`, with parent/base
`11e9d08b84507df64552d7d5b4b3d87a4697d7d3`. The change adds terminal-pane padding
and spacing, touching six files (8 source lines changed, plus tests/docs).
[Closing issue #231](https://github.com/uberblick-ai/ub-agents/issues/231) last
changed at 08:22:37 UTC, before the reference review at 08:44:53 UTC. Its requirements
are preserved in [requirements.md](requirements.md); project instructions came
from `AGENTS.md` at the reviewed head.

The runner initially considered #239, but accidentally retrieved its current PR
body, which disclosed a review finding. It discarded that target. For #237 the
runner selected only revision/review metadata and closing requirements before the
local attempt; it did not read that PR body, comments or review. The agent received
only the two-commit shallow checkout, instructions and requirements. It had no
remotes, alternates or refs beyond the reviewed head, GitHub credentials, web tools,
external file-content access or external TCP access. Preflight verified those
restrictions. The reference review was fetched only after the model attempt ended.

## Run evidence

Hardware: Apple M4 Pro, Mac16,8, 48 GiB unified memory, arm64, macOS 27.0.1.
Ollama client/server: 0.35.1. OpenCode: 1.18.34, installed only in private scratch.
Model: installed `qwen3.8:27b-mlx`, safetensors / nvfp4; reported parameter count
27,781,081,984. Digest:
`5642e97495e1a088883805981563dcdc4a040c2f53388b7a41d1f24d3622cf7e`.

[run.py](run.py) recreates the checkout, prepares its virtualenv, writes the local
OpenCode configuration and prompt, tests isolation, and starts one attempt using
the documented `@ai-sdk/openai-compatible` Ollama provider. A separate daemon on
the dedicated loopback port applies a 65,536-token context without changing the
operator daemon or persistent configuration. The model was not pulled or replaced.
[The runbook](runbook.md) records reproducible commands and primary documentation.

The exact launcher used for the recorded attempt is at checkpoint
`9a7ab4b253a1f203a0ef50a33a8cc740d43d9473`.
The replay script subsequently gained a signal deadline covering preparation,
a 15-second cleanup reserve within the same 30-minute bound, explicit startup
prune prevention, process-group cleanup for preparation commands, and separate
numeric reasons for the two resource guards.
The recorded daemon log reports zero unused blobs removed. These replay changes
were validated without starting another model review after unblinding.
The replay isolation preflight passed in 4.475 s, the deadline interrupted a
simulated slow preparation step after 2.009 s, and a no-model daemon preflight
confirmed startup with pruning disabled and exited cleanly.

| Attempt | Elapsed | Completion / stop |
| --- | ---: | --- |
| 1 | 4.418 s | Setup failure: Ollama exited because the clean daemon environment omitted its required existing home location; no model request. |
| 2 | 260.261 s | Resource guard: system swap grew from 0 to 2,386.19 MiB, exceeding the 2,048 MiB growth limit. No final review. |

Seven earlier setup-only preflights fixed sandbox startup, native Git runtime
access and absent credential-file handling. The successful preflight took 4.834 s;
none started the model. Attempt 2 preserved the daemon's existing home location
without redirecting it or passing credentials/home to the agent.

Attempt 2 started at 12:44:49.931 UTC. Its wall limit was 1,800 seconds, including
preparation/loading. Context was configured as 65,536 both for the daemon and the
OpenCode model limit. `/api/ps` first reported the loaded model with **effective
context 65,536** at 10.136 seconds and retained that value through the stop. The
effective value, digest, versions, resolved config, invocation and guards are in
[result.json](attempt-2/result.json); [observations.jsonl](attempt-2/observations.jsonl)
contains every five-second sample.

System memory-free percentage fell from 86% before loading to 20% at the stop;
the low-memory guard (at or below 5%) did not fire. Ollama's reported model/VRAM
allocation peaked at 22,066,616,060 bytes (20.55 GiB). RSS observations cover process
group leaders, not the separate MLX worker; the reported model allocation and
system memory/swap are the broader observations. These are not exclusive process
memory measurements. The repository suite ran concurrently during the first
roughly 27 seconds, so this is not an isolated performance benchmark.

Output was flushed to files as OpenCode emitted it. Some events were emitted only
when their part completed. A full session export after stopping confirmed no
additional in-flight tool calls. OpenCode received SIGTERM (exit -15), the private
daemon exited 0 and logged stopping its MLX runner. A process/port check confirmed
no experiment process remained, the private port was closed, and the existing
operator daemon still reported no loaded models. Export to a pipe initially
truncated JSON; [collect.py](collect.py) uses a regular file and validates the export.

## Local output and coverage

[review.md](attempt-2/review.md) records the missing final review and last emitted
text. [trace.md](attempt-2/trace.md) contains all eight calls, inputs, outputs and
statuses, checked against the JSON event stream and exported assistant messages:

- Two completed file reads: `AGENTS.md` and `requirements.md`.
- One denied file read: the runtime's `prompt.md` (OpenCode's outside-directory
  permission). The prompt was already supplied as the user message.
- Five completed commands: bounded Git log, diff stat, commit stat, locating the
  requirements/runtime directory, and the source/test diff.
- No searches through source, no complete source-file reads, no caller-following,
  no test commands and no confirmed or suspected review findings.

**Relevant source beyond the diff: not examined.** The agent's intermediate text
only announced planned work; it made no completed-check claims. An early live
progress update overstated source coverage; this report and the trace give the
verified coverage. Missing output is not treated as a clean review or a false
negative. Sanitized launch config, prompt, sandbox and daemon log are preserved
alongside the trace. Raw state and redundant event/session exports remain private.

## Comparison at the same revision

Reference: [Claude review 5412022281](https://github.com/uberblick-ai/ub-agents/pull/237#pullrequestreview-5412022281),
at the exact head above (attributed to Claude by the assignment; GitHub's author is
the operator account). Its body contains one required visual correction and a
nonblocking truncation note; its inline-comment list is empty. The model/version
of the reference reviewer is not identified in that record.

| Category | Adjudication |
| --- | --- |
| Shared findings | None; the local model never delivered findings. |
| Reference finding absent locally | Active-tab inversion has asymmetric padding. It was not assessed by the stopped local run; this is not a measured quality miss. |
| Additional valid local findings | None produced. |
| False positives | No local claims to assess. Claude's tab asymmetry is reproduced, but classifying it as a blocking requirement is subjective. |

At `src/ub_agents/view_ui.py:379`, `padding: 0 2 0 0` sets zero left and two right
cells for every tab; lines 380–381 invert the active tab. After the blind run,
[adjudicate.py](adjudicate.py) rendered the head's themed fixture at 110×32 and
asserted that the uniformly inverted block is `1 Log  `, with padding `[0, 2]`.
[adjudication.json](adjudication.json) records the compositor output. This confirms
the concrete observation. The closing issue specifies pane inset and indicator
position, but does not explicitly require symmetric inverted-tab padding. It is
a cosmetic design concern, not a demonstrated functional failure. Claude's
blocking verdict therefore is not ground truth. The narrower-header truncation
note is consistent with the issue allowing `…` inside padding; it is not a second
required correction. The pinned screenshot and resize tests both passed after the
run (2 tests, 3.273 seconds), confirming pane widths of 46 and 63 and the one-column
gap. These evaluator checks are not credited to the local agent.

## Recommendation and validation

This configuration is not yet an evidence-based unattended reviewer option for
ub-agents. Tool-loop startup, blinding and effective context worked; useful review
completion, caller inspection, focused tests, quality and repeatability remain
unproven. One resource-limited attempt does not establish that the model cannot
review. The smallest next step is an owner-assessed repeat of this same spike with
an explicit resource budget and host-load observation, before considering an
adapter or queue change. There is insufficient evidence to open a production
integration follow-up. Keep #209 open for the owner to accept or revise this
recommendation.

Branch validation: 1,137 repository tests passed, `ub-agents check` reported valid
configuration, and `git diff --check` passed. Experiment validation: all three
Python scripts compile, isolation preflight passed, the bounded attempt and session
collection ran, event/export counts matched (7 completed / 1 error), and the focused
reference adjudication and two pinned tests passed. All files added by this spike
are under `experiments/`; production code, workflow, README, docs and changelog are
unchanged. No changelog entry is needed for research artifacts.
