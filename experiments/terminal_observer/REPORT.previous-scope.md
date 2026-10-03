> Archived run-2 assessment at `e0984a6`. Its broader scope and recommendation
> were superseded by the owner clarification. Retained for quota/packaging provenance;
> use [REPORT.md](REPORT.md) for the current launcher-local decision.

# #97 terminal observer spike — revised assessment

**Recommendation: do not proceed with a production Textual observer now.** A
separate view is feasible in this experiment, but its advantage over existing
status/log tools and its maintenance cost are not established. Complete delivery
history is absent from the existing status contract. Prefer assessing the
no-dependency option before deciding whether the combined interface earns its
cost. This is advice for the owner, not authorization for more work. PR #99 stays
draft with `Refs #97`; no follow-on tickets or implementation were created.

This report supersedes the first run's “proceed with constraints” assessment and
addresses both assigned reviews at `96db2d7`. All code changes are experimental.
Distributed dependencies, `src/ub_agents`, repository README, public docs and
changelog remain unchanged; there is no release-facing change.

The overall operational claim in each acceptance question remains **unverified**.
The narrower evidence below uses only **demonstrated / disproved / unverified**;
a successful fixture test is not a claim about a real launcher deployment.

| Question / claim | Verdict | Evidence and limits |
| --- | --- | --- |
| Observation: reuse existing queue/claim/outcome authority | **demonstrated** in fixtures | Unchanged `status_rows`, planner and coordination parser supply every row. Buckets are presentation only. Current human blocker #6 now stays expanded in Needs attention; its accepted success remains in Runs. Both review regressions reject the former behaviors ([mutation evidence](evidence/regressions.json)). |
| Observation: navigation/redraw make no requests | **demonstrated** in the pilot | The real `GitHub` transport uses `DiscoveryCostRunner`, not a counter on `FakeGitHub`. Selection, tabs, raw/follow and resize add **0 calls**. A deliberate tab-refresh mutation adds **375 calls**, and the zero-call assertion rejects it. Mount refresh costs **125 fixture calls**. [Validation](evidence/validation.json). |
| Observation: quota wait preserves the view | **demonstrated** with a simulated response | An injected HTTP 403 traverses the real error parser, costs **1 call**, retains the previous snapshot, and blocks subsequent refresh/navigation calls. [Transport wait](evidence/transport-quota-wait.svg). A separate [growth demo](evidence/quota-wait.svg) tails local output during a simulated wait. Actual quota exhaustion and hung reads remain **unverified**. |
| Logs: current production formats, partial/long/error output | **demonstrated** on synthetic replay | [Codex human text](evidence/codex-production.svg), [Claude whole-message JSON](evidence/claude-production.svg), and their complete fixture `process.log` files are checked in. Claude includes tool invocation/results, tool errors, system/rate-limit/unknown/error/result records and a **49,439-byte tool-result record**, decoded whole. A split 42KB tool result retains its first-fragment capture time. [Partial preview](evidence/partial-record.svg) appears before newline. Codex human lines stay compact. Raw files keep full bytes; displays explicitly shorten text to 2,048 characters. Records above the 128KiB cap become labeled raw fragments; their readability is **unverified**. |
| Logs: trustworthy producer timestamps / one real claimed run | **unverified** | Synthetic event time is distinct from observer capture time. Neither first-run probe supplied reliable producer timestamps. Historical data stays untimed. Capture means first observer read, not producer write. The original probes used additional format flags and no claims; they do not satisfy the production-format or real-claimed-run requirement. No new runtime probes ran. |
| Responsiveness: selection/tabs, scroll/follow, narrow size, memory bound | **demonstrated** in the headless pilot | While output grows, selection/tabs work; paused [scroll](evidence/paused-scroll.svg) stays stable and follow returns to the end. [72×24](evidence/narrow-72x24.svg) wraps logs; labels/shortcuts still clip. Limits: 200 entries, <128KiB pending bytes, 32KiB read/tick, 400 widget lines, ≤2,048 characters per text/raw projection. A 1,000-line fixture evicts entries. Sustained throughput, RSS, target-terminal/herdr usability and accessibility are **unverified**. |
| Lifecycle: close a separate observer without stopping work | **demonstrated** for an owned dummy worker | A real separate observer process runs in an isolated PTY. Both `q` and Ctrl-C exit 0; the independently supervised Python worker remains alive, finishes naturally and leaves its owned group empty. Ctrl-C is explicitly bound to close the view (Textual defaults to exit help). No supervisor reference or stop control exists in the view. The attached alternative and real host deployment are **unverified**. |
| Lifecycle: existing graceful stop and interrupt semantics | **demonstrated** by unchanged repository checks | 514 repository tests pass, including graceful drain/report/transition/cleanup and interrupt/release tests. No execution or coordination code changed. The experiment never observes or signals operator workers. |
| Local claim attribution and remote restrictions | **demonstrated** in fixtures | Select fixture claim **#42**, resolve `host` + `log_dir/process.log`, and read its distinct file rather than #1's file. [Selected lease](evidence/lease-selected-log.svg). A remote claim has no tail or stop control ([remote view](evidence/remote-owned.svg)). Real claim-to-log-to-accepted-outcome integration remains **unverified**. |
| Operator usefulness: running work and why a human is needed | **demonstrated** in fixtures | Owner/run metadata appears in Runs; [human blocker](evidence/human-needed.svg) explains the decision; [accepted human handoff](evidence/accepted-human-blocker.svg) shows the successful step without hiding the current blocker. Actual operator benefit is **unverified**. |
| Operator usefulness: complete completed-step history from status alone | **disproved** | Regression completes fixture #7, removes its trigger and releases it successfully: it disappears from `status_rows`. The collapsed Recent activity bucket is empty in this fixture. Runs can show the accepted outcome of a retained row, not all completed steps. Runtime tool output is never accepted as a workflow outcome. |

Real refresh cost was measured once in a bounded, read-only audit of this repository
on **2026-10-03 09:23 UTC**. Four manual passes used the production transport and
project configuration; they read no run logs and made no mutations. [Per-call
HTTP statuses, conditional requests and quota headers](evidence/network-refresh.json)
are reproducible with the opt-in `network_audit.py`, which is outside offline checks.
The snapshot had four status rows/plans; costs change with queue inputs,
pagination, triggered PR evidence and comment churn.

| Audit pass | gh calls | REST attempts | Charged REST responses | GraphQL queries | Time | Formula start gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fresh status | 21 | 18 | 18 | 3 | 12.262s | 259.2s |
| Fresh status, same transport ETags | 14 | 11 | 1 | 3 | 7.347s | 30s |
| Cached planner warm-up, same transport | 14 | 11 | 1 | 3 | 7.706s | 30s |
| Cached planner unchanged | 3 | 3 | 1 | 0 | 2.485s | 30s |

Identity discovery is **one additional charged REST call**, excluded from that
table. The UI's first refresh includes it: 22 total calls, 19 charged REST responses,
and an implied 273.6s gap. The second fresh status got ten authenticated 304s;
these do not consume primary REST quota ([GitHub guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests)).
GraphQL queries are separate from REST; attributable point cost is **unverified**
(the audit records headers, not `rateLimit.cost`; other launchers share the account).
At a hypothetical unchanged 30s cadence, the observed fresh-status mix would
make 120 charged REST responses and 360 GraphQL queries/hour. This is an
extrapolation, not an observed hour or a shared-account allowance.

`polling.py` reserves half of 5,000 REST requests/hour across **ten launchers**:
250 charged requests/hour each, or **14.4 seconds/request**, with minimum 30s here,
maximum 3,600s, and low-quota adjustment. The demo now uses `quota_requests`, as
the launcher does, rather than attempted `rest_requests`. For comparison only,
the **45-issue fixture** has no ETags or PRs: 125 calls = 95 REST + 30 GraphQL,
implying **1,368s (22m48s)** between fresh refresh starts. Its cached unchanged
planner costs two fixture calls. Those are neither bounds nor real quota costs.

A separate process cannot currently share launchers' `Discovery` memo, ETag cache,
comment cursor or counters: all are process-local. The demo retains its own
transport ETags, but `status_rows` deliberately creates fresh discovery each time.
Using the same formula gives diagnostic cooldowns; it does not allocate an idle
slot to an observer or coordinate multiple observers. With ten fully allocated
launchers an extra 250/hour consumer exceeds that half-budget. A production
allocation/cache-sharing design and GraphQL budget remain **unverified** and
outside this spike. No policy was changed to make the demo work.

A local observer must run on the lease's recorded host, with the recorded paths
visible, normally **as the loop user on `uberblick`**. `.ub-agent` is created mode
0700; ordinary laptop access as another user will not expose real logs. Existing
file permissions are respected and errors are shown. This experiment uses exact
hostname equality and offers no remote transport. No credentials or permissions
were changed. Installation and use in the actual loop user's herdr pane are
**unverified**. Truncation resets pending bytes/time and replacement checks inode;
truncate-and-regrow between polls, filesystem races, cleanup removal, content
sanitization and prolonged high volume remain risks.

The library is [Textual 8.2.8](https://github.com/Textualize/textual), MIT-licensed,
with a maintained upstream and a useful [headless pilot](https://textual.textualize.io/guide/testing/).
The first-run locked Python 3.14 environment measured **nine extra distributions,
20,625,146 installed bytes (~19.7MiB)** beyond PyYAML ([dependency inventory](evidence/dependencies.json)):
Textual, Rich, markdown-it-py, mdit-py-plugins, platformdirs, Pygments,
typing_extensions, mdurl and linkify-it-py. This is the spike's pinned set, not a
promise about future resolution.

| Option | What it adds / loses | Delivery and maintenance cost |
| --- | --- | --- |
| Existing `ub-agent status` / `status --json`, `launch.log`, `tail -f process.log`, plus standard-library pretty-printer | Status already answers queue/ownership/blocker questions. UTC launcher events offer local execution/activity context without GitHub reads. Codex text works with plain tail; `pretty.py` makes Claude tool results readable, using the same bounded adapter. Separate terminal panes require manual correlation and lack combined selection/tabs. Launcher text is observation, not a substitute authority for accepted outcomes. | No new distribution. Status still costs its existing GitHub scan; local log reads cost zero requests. A runnable experimental pretty-printer is included, and has no Textual import. Actual operator preference remains **unverified**. |
| Textual separate observer | Combines selection, issue/run context, wrapping, scrolling/follow and explicit stale/poll state. It does not add authoritative history or recover producer timestamps. | Nine dependencies plus terminal/layout/testing compatibility; each runtime still needs an adapter. Unproven operational advantage over the option above. |
| Attached foreground UI | Could share in-process snapshots/cache/accounting with a launcher. | Couples closure/signals and UI callbacks to supervision; not prototyped, **unverified**. Separate observer is the recommended lifecycle if any UI is later authorized. |

The [tap formula at `19ac5c2`](https://github.com/uberblick-ai/homebrew-tap/blob/19ac5c2034db262582244c9ce6876cdcf44f891f/Formula/ub-agents.rb)
currently declares only PyYAML and installs into `libexec` with
`virtualenv_install_with_resources`. Shipping Textual there would require nine
additional pinned, checksummed resource blocks (or explicitly provided formula
dependencies), install tests and coordinated updates. Homebrew installs resources
with dependency resolution disabled ([authoring guidance](https://docs.brew.sh/Language-Specific-Formulae#python-dependency-and-resources)).
A pip optional extra alone would not give current `brew install` users a selectable
UI; they would need a separate managed environment. A separate observer package/
formula could keep the core lean, but adds its own release/tests and still maintains
those resources. No packaging option was implemented or tap edited; maintainer
agreement would be needed for new runtime dependencies.

Runtime adapters are still needed regardless of UI: Claude whole-message content,
`tool_use` and `tool_result` blocks plus result/error/unknown fallback; Codex current
human text with compact lines. Optional Codex JSON and Claude deltas remain fallback
paths, not reasons to change production flags. Rich UI is not needed to pretty-print
these formats. Reliable timestamps, complete history, shared budget/cache behavior,
real claim attribution and terminal usability are the remaining decisive gaps.
If later authorized, the smallest split is (1) local lease locator/bounded adapters
and no-dependency observation; (2) decide history and shared-account refresh inputs;
(3) optional UI/package and signal/terminal validation. No part of that future work
is authorized by this report.

Validation: **514 repository tests** passed (31.687s), configuration validation and
whitespace checks passed. **Nine offline experiment checks** pass, including
subprocess closure; both former-bug mutations fail their respective regression,
and the navigation-refresh mutation fails the zero-call assertion. The existing
CI matrix does **not** install Textual or run the experiment. Final candidate SHA
and its exact checks are named in PR #99's body. [Runnable instructions](README.md)
distinguish offline replay, the optional network audit and historical probe evidence.

Effort ledger (cumulative cap: 240 minutes; launcher limit: 180 minutes/run):

- Run 1 at `96db2d7`: **30 minutes charged**, including its handoff; **210 minutes remained**.
- Run 2 started **2026-10-03 09:16:28 UTC**. No runtime probes repeated; earlier
  timings in `probes.json` are archival and do not establish current-format integration.
- Run 2 checkpoint and final charge are recorded below and in every PR update.
  Setup, validation, findings, waiting and handoff are charged conservatively.
  Findings began well before the final 30 minutes of either deadline. Remaining
  questions are **unverified**; this assessment needs an owner decision, not a retry.

Run 2 checkpoint: **2026-10-03 09:39:26 UTC**; this run elapsed **22m58s**,
cumulative charged effort **52m58s** (including run 1's 30m), remaining
**187m02s**. Final conservative charge reserved for checks/CI/handoff:
**40 minutes for run 2, 70 minutes total, 170 minutes remaining**. The final PR
and issue update confirm that charge at handback; no third run or retry is needed.
