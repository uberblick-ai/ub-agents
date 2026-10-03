# #97 terminal observer spike

**Recommendation: proceed with constraints.** Textual can provide a useful optional
**separate observer** without changing GitHub coordination or supervision. This
experiment demonstrates the presentation and local tailing boundary, not a
production feature. The branch and PR must stay draft for owner assessment.

## Evidence and verdicts

| Question | Verdict | Evidence and limits |
| --- | --- | --- |
| Observation | **demonstrated** in fixtures | `model.py` calls unchanged `cli.status_rows`; its rows preserve planner states/reasons, claims, blockers and run-linked outcomes. Group names are presentation buckets. Navigation and redraws use that snapshot and make zero requests or coordination writes. [Request audit](evidence/validation.json) uses the real GitHub transport construction with the existing recording runner: 45 open items, 30 triggered, 3 commenters produce **125 calls per fresh status refresh** (95 REST, 30 GraphQL). Two unchanged fresh refreshes each cost 125. The existing cached planner costs 125 then **2**. [Quota-wait view](evidence/quota-wait.svg) keeps a stale snapshot while local logs and tab changes continue, with zero calls during the simulated wait. **unverified:** real GitHub pagination, quota exhaustion and network failures. |
| Logs and timestamps | **demonstrated** | [Codex](evidence/codex-sanitized.svg) and [Claude](evidence/claude-sanitized.svg) replay sanitized successful live outputs. Both adapters exercise split records, >40KB records, unfamiliar/error events and lossless raw-file fallback in `validate.py`. A [partial record](evidence/partial-record.svg) is visible before newline. Oversized records become bounded raw fragments; normalized text truncates at 2,048 characters. Synthetic valid event time and first-fragment observer capture time appear separately. Historical data has no invented capture time. The actual probes had no reliable event timestamps, so those stay absent. Tool completion is text evidence only; accepted outcomes come exclusively from coordination records. |
| Responsiveness | **demonstrated** | Keyboard selection and tabs work while replay grows; [paused scrolling](evidence/paused-scroll.svg) stays stable on append and follow returns to the end. [72×24 view](evidence/narrow-72x24.svg) wraps logs, with some left labels/footer shortcuts clipped. Memory retains **200 entries**, <16KiB pending bytes and **400 rendered lines**; each tick reads ≤32KiB. A 1,000-line stress fixture evicts old entries. Hidden tabs retain bounded data and render on return; resizing reflows it. **unverified:** prolonged high-volume throughput, terminal/accessibility diversity, RSS and slow/network filesystems. |
| Lifecycle | **demonstrated** | Closing the separate view leaves a real, independently supervised test worker alive; it finishes naturally with exit 0 and an empty owned group. The existing 514-test suite passes, including SIGTERM draining/report/transition/cleanup and SIGINT/SIGHUP termination/release. No launcher signals, ownership decisions or cleanup code changed. [Remote claim](evidence/remote-owned.svg) offers neither its log nor a stop control. **unverified:** closing during a hung GitHub refresh and binding real claimed run paths. |
| Operator usefulness and cost | **demonstrated** for the three fixture questions | Running work has owner/run metadata; [human-needed view](evidence/human-needed.svg) names the owner decision; [completed step](evidence/recent-accepted.svg) shows accepted success and completed transition. These are faithful fixture records, not acceptance of runtime prose. **unverified:** operator usability study, complete historical activity and real issue-detail hydration. |

## Live probes

Exactly one read-only task ran per installed runtime, using the existing
`execution.supervise`, a private directory inside this worktree and existing auth.
No GitHub claim or workflow mutation was made for either probe; the attached UI's
row #1 was explicitly a fixture. The task read only READ_ME.txt and returned
`SPIKE97_OBSERVER_OK`. See [commands and measurements](evidence/probes.json) and
[sanitized Codex](evidence/codex-sanitized.jsonl) / [Claude](evidence/claude-sanitized.jsonl).

Codex CLI 0.159.3 completed in **12.402s**; Claude Code 2.1.287 in **5.525s**. Both
exited 0, allowed tab navigation during execution and left no owned group members.
First visible output appeared at **0.446s / 0.528s** from probe start respectively;
this includes startup metadata, not time to first model text. The tail interval is
100ms, not a claim of measured end-to-end latency under load. Raw output was
927 / 19,830 bytes. Full raw logs remain local and ignored; published evidence
omits identifiers, system metadata and reasoning. Missing access did not arise.
No live probe was retried.

## Cost, boundaries and risks

[Textual 8.2.8](https://github.com/Textualize/textual) is maintained and MIT-licensed;
its [headless pilot](https://textual.textualize.io/guide/testing/) and
[bounded RichLog](https://textual.textualize.io/widgets/rich_log/) supplied the main
validation tools. The isolated Python 3.14 environment adds **9 UI distributions,
20,625,146 installed bytes** including bytecode/metadata (about 19.7MiB), above the
existing PyYAML. See [versions/dependencies](evidence/dependencies.json) and the
replay lockfile. No runtime dependency, `src/ub_agents` behavior, repository README,
public docs or changelog changed; no user-facing release entry is warranted.

The two runtime adapters remain necessary. Claude uses stream-event deltas,
assistant text/tool blocks, user tool results and result/error messages; Codex
uses item lifecycle/agent-message/command-output records and turn/error events.
Unknown shapes fall back to bounded raw text. Claude deltas and complete messages
currently duplicate text; a production adapter needs careful assembly and
compatibility fixtures. Claude already defaults to stream JSON in `command_for`;
Codex currently defaults to human text, so richer Codex events require an optional
project-owned `--json` argument. Human text still tails through the raw fallback.
Neither format should become an outcome authority or promise event timestamps.

`process.log` is already append-only combined stdout/stderr under the existing
supervisor, with no capture-time sidecar. This supports local tailing without
pipes or supervision changes, but observer capture is **read time**, not producer
write time. Historical capture time cannot be recovered. File replacement,
cleanup removal, truncation races, attribution to the exact lease/run, malformed
records and untrusted escape sequences need continued attention. This demo strips
terminal controls and disables markup; it is not a complete content-security audit.

The grouping heuristic distinguishes some parked reasons by text. It confers no
authority, but stable presentation categorization deserves review. `status_rows`
selects current/run-linked outcomes; it is not a complete recent-history API and
may omit closed work. A fresh status poll's request amplification makes frequent
polling unsuitable: use existing cached discovery and shared budget accounting,
with explicit stale/error state, before production. GraphQL cost remains a separate
resource. Live details, remote transport and all workflow/stop controls are outside
this demo. Prefer a separate observer so closing it has no supervision meaning.

The smallest sensible implementation split, **if subsequently authorized**, is:
(1) a read-only snapshot/local-run locator using existing discovery and quota
contracts; (2) bounded raw tailing plus runtime adapters; (3) optional Textual
presentation and lifecycle validation. Leave policy, claiming and execution in
their existing modules. No follow-on implementation or tickets were created.

## Effort and validation

Start: **2026-10-03 08:26:34 UTC**. One run, no earlier branches. The checkpoint
ledger below includes setup, implementation, validation and findings; the final
PR update records total effort including handoff. The four-hour cap is cumulative,
with the final 30 minutes reserved for findings; this run stops well before both
that reserve and the 180-minute launcher deadline. Open questions above stay
**unverified**, rather than extending this spike to complete the UI.

Standard checks: editable install into this worktree's `.venv`; **514 tests passed**
(32.614s); development `ub-agent check` passed; `git diff --check` passed. The
separate experiment validation passes five checks and regenerates the screenshots.
The [runnable instructions](README.md) distinguish replay from the already-used
live probe allowance. This is ready for human assessment, with a blocked report
and a draft-only `Refs #97` handoff.

Checkpoint: **2026-10-03 08:45:43 UTC**; elapsed active effort
**19m 9s**, remaining hard-cap budget **220m 51s**.
Total effort charged for this spike: **about 25 minutes**, conservatively rounded
up to include final checks, push, PR/issue updates and the installed-launcher report.
No earlier-run effort exists; at most **215 minutes** remain after this charge.
No further spike work is planned.
