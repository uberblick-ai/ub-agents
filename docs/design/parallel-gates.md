# Parallel gates

Design spike for [#616](https://github.com/uberblick-ai/ub-agents/issues/616),
tested on 2026-10-22 against `9b1b6b4afb8bb034b81f4c355334ec77d34f1275`.
Internal design notes only; no behavior, configuration or default changes.

**Recommendation:** Build parallel PR gates using two existing serial launchers
in separate control checkouts, compatible reader leases and a join backed by
current-head outcome records. Keep fan-out and join implicit in the label graph;
group names are unnecessary. The proposed YAML is a useful starting point, but
needs destination label removal, opening provenance and neutral cancellation.
The difficult work is durable completion and recovery, rather than starting two
processes. Estimate **11–18 engineering days**, plus review and deployment time,
for the bounded first implementation below. This is a planning estimate, not
measured delivery time.

## Prototype and results

The prototype is on
[`spike/616-parallel-gates`](https://github.com/uberblick-ai/ub-agents/tree/spike/616-parallel-gates).
It has **no PR and must not be merged**. The
[dry-run harness](https://github.com/uberblick-ai/ub-agents/blob/spike/616-parallel-gates/spikes/parallel_gates_dry_run.py)
and its
[recorded results and limits](https://github.com/uberblick-ai/ub-agents/blob/spike/616-parallel-gates/spikes/parallel-gates.md)
live on that branch. This findings PR contains only this note; no prototype code
is proposed for `main` and no implementation issues have been filed.
The tested prototype commit is
[`f02cdca2bf33d3f3df92e3b46c944a49df3a2fb2`](https://github.com/uberblick-ai/ub-agents/commit/f02cdca2bf33d3f3df92e3b46c944a49df3a2fb2).

Run in an isolated checkout of the prototype branch:

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
mkdir -p .ub-agents/spike-scratch
.venv/bin/python -m spikes.parallel_gates_dry_run --scratch .ub-agents/spike-scratch
```

On Linux with Python 3.14, **10 dry-run tests passed**. The existing **2,168-test
suite**, `ub-agents doctor --offline` and `git diff --check` also passed on the
prototype. Doctor reported zero required failures and zero warnings; it checked
the existing repository configuration, not the proposed YAML syntax.

| Required scenario | Observed result through the real Loop |
| --- | --- |
| Both gates pass | Two leases are live together; the first pass leaves the join closed; after the second release, `tick_item` executes the integrator. |
| Changes requested while a sibling runs | The negative transition removes the sibling trigger; its later report settles as cancelled, with unchanged attempt effect, no accepted pass and no integrator. |
| Head moves between passes | Work assigned before the move is rejected even when its report observes the new head; a fresh second pass leaves mixed-head labels present but the join waiting. |
| Two gates in one flow | One pair passes, an intermediate join opens a second distinct pair, and only that second pair starts the final integrator. |

Six additional checks cover compatible claim races, duplicate-role claim election,
recovery beside a live sibling without reexecution, same-head reopening, linked
issue branch exclusion and issue-to-PR removal semantics. Reopening invalidates
the earlier opening's passes even if someone restores their labels.

The harness uses `tests.support.FakeGitHub` with separate real `Loop` instances,
real planning, claims, reports, transitions, release and expiry recovery. Gate
completion order is controlled; the integrator uses a recording executor. Two
tests race claim writes using threads. This establishes overlapping leases and
the four required completion traces, **not** concurrent real model execution or
concurrent label-finalization safety.

The branch deliberately leaves YAML parsing, doctor wiring checks, status/view
changes, automatic rearming, early process cancellation and the final protocol
version unimplemented. It uses experimental v3 fields, per-trigger opening run
identities and report-ID ordering for serialized invalidations. These are not a
production recovery protocol. No macOS, multi-host, network-partition, concurrent
finalization or crash-between-label-writes experiment was performed.

## 1. Leases, claim election and recovery

Today [`Coordinator.plan`, `claim` and `_owned`](../../src/ub_agents/coordination.py)
all select one live owner. Changing just discovery would still make the second
agent lose its election or renewal. Use one compatibility rule at every boundary:

- Both leases explicitly opt into parallel PR review and refer to the same PR
  and assigned head, but different agent names.
- A second run of the same agent remains exclusive. Non-parallel agents wait
  until **every** live reader and any unfinished cleanup have ended.
- Legacy or unknown lease modes remain exclusive. A head change makes new-head
  readers incompatible with old-head readers until the old runs stop.

Each contender still writes its own tentative record. Sort live contenders by
GitHub comment ID, admitting each only if compatible with every already admitted
winner. This preserves the current earliest-claim rule for exclusive work and
admits both distinct readers. A losing contender withdraws only its own record.
Renewal, reporting, acceptance, release and recovery must test membership in that
same elected set, rather than compare with the first lease. The scheme gives no
fairness guarantee to an exclusive contender repeatedly racing readers.

Pin the mode, assigned head and opening identity in the lease. Recovery uses
those recorded values rather than today's config or head. Extend the current
recovery election so an expired reader's report can settle beside a compatible
sibling; recoverers of the same source still exclude one another and permanently
supersede that source. An incomplete report/transition never counts as a pass.
The prototype recovers a visual pass while security remains live, without
starting another execution.

Keep [`shared_branch_owner`](../../src/ub_agents/coordination.py) conservative
across items: an issue implementer continuing a PR branch must exclude all PR
readers, and those readers must exclude that writer. Do not disable the branch
check just because a role is parallel. The prototype retains and tests this
exclusion. The existing lowest-ID cross-item claim check and unconfirmed-cleanup
guard must apply to all relevant owners; races across the issue and its PR need
production tests. Parallel permission applies only within the same PR, never
across two items that happen to share a branch.

Parallel roles must use private, candidate-pinned workspaces and act as readers:
they report findings and declared outcomes, without pushing candidate changes,
merging or editing sibling state. [`Worktree`](../../src/ub_agents/execution.py)
already creates detached per-run PR worktrees. Require `kind: pr`,
`worktree: true` and no branch-mutating role for the first implementation;
incompatible writers keep exclusive leases. Configuration and role instructions
provide cooperative discipline, not a credential-level write sandbox.

The existing [cooperative limitations](../coordination.md#claim-election) remain:
GitHub comments are not compare-and-swap, clocks must agree reasonably, and a
lost launcher cannot be strictly fenced from external writes. Keep ownership
through confirmed process and worktree cleanup; unconfirmed cleanup must block
replacement work rather than disappear behind a sibling's successful release.

## 2. Execution

**Use two launchers first**, each in its own control checkout, configured with
the same workflow and upgraded protocol. Operators can dedicate one to each
review role with the existing `launch --agent NAME`, or let both discover work.
The compatibility election ensures they do not run the same role twice. Two
private run worktrees inside one shared control checkout are insufficient:
instruction refresh, runtime maintenance and startup cleanup also operate there;
concurrent launchers sharing one checkout are currently outside the supported
boundary in [coordination](../coordination.md).

No multi-assignment scheduler is needed. A launcher still runs one assignment
and renews one lease. Two launchers cost another control checkout, process,
runtime capacity and discovery traffic; peak model usage becomes two simultaneous
reviews rather than the sum of their durations. The fake dry run measures none
of those runtime or API costs.

One launcher supervising several assignments is a separate, larger change:
`Loop` holds one renewal, active GitHub lease, finalization cache, planning worker
and interrupt path; observations and the terminal hold one local assignment.
Sharing a single Loop between worker threads would race those fields. A future
supervisor could own separate run contexts or isolated Loop workers, but needs
bounded concurrency, cancellation routing, cleanup and UI aggregation. Defer it.

## 3. Joins and current-head passes

**Yes, with durable evidence; label presence alone cannot do this.** Add a
normalized trigger representation: existing strings/lists retain their current
OR meaning; `trigger: {all: [visual-ok, security-ok]}` requires every label and
every corresponding valid pass. For the first version restrict these joins to
PR gates, reject empty/duplicate/nested forms, and keep a join non-parallel.

At discovery, immediately before claim, and before an integrator's success is
accepted, require for each input:

1. The label is present and a trusted, declared parallel producer supplied it.
2. Its outcome is accepted, unrejected and transition-complete, tied to its real
   source lease, with confirmed successful release or successful recovery.
3. Both the source assignment SHA and reported candidate SHA equal the current
   PR head. `report` currently observes the destination head at reporting time;
   checking only `candidate_sha` would let old assigned work claim a new head.
4. The pass belongs to the latest opening of its producer trigger. The required
   passes share that fan-out opening; no newer opening, invalidation, relevant
   reset or cancellation supersedes them.

Use the opening outcome's durable identity, such as source run/lease plus ordered
transition intent, rather than a new configured group name. Pin it when claiming
each gate and carry it into recovery and evidence. Resolve issue-to-PR handoff
copies back to the same source identity, without treating a copied comment ID as
a second opening. Don't infer transition ordering from outcome creation time:
agents can report early and label mutations can finish in another order.

This also prevents same-head reuse after an implementer fans out again. Distinct
gates in one flow use distinct producer triggers/pass labels and opening outcomes;
one join cannot consume the other pair's passes. Reusing a label or role for
simultaneous independent gates is ambiguous and should be rejected or diagnosed.
Sequential reuse needs a fresh opening, not a reset of label appearance alone.

If the head moves after visual passes, consuming visual's trigger has already
removed its automatic rerun path. **Fail closed and require a fresh implementer
fan-out on the new head** in the first milestone; expose exactly which evidence
is stale. Automatic head watching and rearming are optional later work. `retry`
must not manufacture join inputs by adding its first trigger, as its current
single-label repair can do. Route operators to rerun the producer/fan-out instead.
The integrator must still validate the head and required GitHub checks at merge.

## 4. Sibling stop and durable transitions

**Quiet completion is feasible; early stop needs supervisor work.** The prototype
recognizes a sibling's durable removal, leaves its late pass unaccepted, and
releases as `cancelled` with unchanged attempt effect. Production should make
cancelled a validated neutral terminal result throughout records, attempts,
reporting, recovery, notices and display. It must not reset failure history,
charge a retry, add a stop label or post an action-needed notice. A human removing
an unexplained trigger must retain the existing paused/inspection behavior.

A removed trigger is a cancellation request, not proof that the process stopped.
The sibling launcher retains its lease, ends its owned process group using the
existing TERM/KILL and termination checks, runs cleanup, then releases. Poll for
trusted invalidation alongside renewal/planning, using a per-run cancellation
signal. Do not use the launcher's global interrupt as a new retry failure. Polls
must be bounded and rate-limit aware; unreadable GitHub cannot establish a pass
or a confirmed cancellation, and the last confirmed lease deadline still applies.
No other launcher kills a remote process or edits its ownership record.

The subtle race is [`apply_transition`](../../src/ub_agents/loop.py): once
`started` is recorded, recovery intentionally replays additions without requiring
the original trigger. A sibling removing that trigger cannot safely undo this
intent. There is also a window between checking the trigger and adding a label.
Suppressing the current exception alone would allow a late/recovered pass to
resurrect a cancelled gate.

Serialize short label-finalization operations with a separate cooperative
per-item transition claim while reader execution leases remain live. Persist an
ordered invalidation/opening intent before removals and additions, and make replay
check whether a newer intent supersedes it. Joins validate these intents even
when a crash leaves stale labels visible. Keep pending handoffs and failed writes
fail-closed and recoverable; handoffs acquire source and destination mutation
resources in a stable order to avoid deadlock. This is an additional protocol resource, not a gate
group name, and needs claim-race, lost-ownership and partial-write recovery tests.
It improves cooperative ordering without claiming atomic GitHub transactions.
The prototype does not implement or prove this finalization protocol.

## 5. Doctor without group names

Build a graph of roles, normalized trigger predicates and declared outcome
additions/removals. Diagnose wiring in offline doctor as well as config parsing;
online doctor can retain its existing label provisioning checks.

| Check | Suggested severity |
| --- | --- |
| An outcome enables two distinct ordinary PR roles, but either lacks `parallel: true` | Error for the proposed fan-out; name the outcome and both consumers. |
| A parallel role is not PR-only, lacks a private workspace, or an `all` join is parallel | Error for the bounded first implementation. |
| Any `all` input has no declared parallel pass producer | Error; require a producer for **each** input, not just one somewhere in the join. |
| Join inputs lack a common fan-out capable of opening their producers | Error for this milestone's common-opening contract. |
| Pass labels have ambiguous producers, are also ordinary start/stop labels, or two concurrent gates reuse the same role/labels | Error where ambiguous; otherwise show the possible paths for review. |
| Fan-out does not clear old destination passes, or changes-requested paths leave sibling triggers/passes untouched | Warning with the missing removals; verify opening invalidation regardless of labels. |
| Join has no reachable producer path, a cycle has no entry, or fan-out has no reachable join | Warning; incomplete graph reachability is not proof of deadlock. |

An outcome adding two inputs of one `all` join is not by itself a fan-out to two
roles. Detect which complete predicates are enabled, preserve PR/issue target
distinctions, and account for consumed triggers. General label graphs can have
external entry points and mutually exclusive paths; doctor should explain its
inference rather than promise complete workflow verification. No group field is
needed for these local checks.

## 6. Terminal view and status

`status_rows` in [`cli.py`](../../src/ub_agents/cli.py) currently attaches
`active[0]` to every plan on the item. With two runs, security can show visual's
owner, expiry and process even while its outcome comes from security. Select the
plan's own lease by item, agent and run; show **all** incompatible owners for an
exclusive waiter. Extend `Plan.owner` or add an owners collection without silently
changing the JSON meaning. Include waiting-for-current-head evidence and neutral
cancellation, with tests for JSON and plain output.

[`Observations`](../../src/ub_agents/observations.py) has one local `assignment`.
That remains correct with one assignment per launcher. Existing history reduction
retains runs by identity, and plan keys already include item and agent. The
[Work view](../../src/ub_agents/view_data.py) can retain separate role rows, but
owner selection must be fixed; eligible plans grouped under one item must retain
both roles. Process logs and cleanup controls must continue to target a run, not
just an item number. Item-wide action-needed clearing/minimization needs review so
a sibling's claim or cancellation cannot hide another role's unresolved request.

Each terminal still shows its own launcher and may show the sibling from the last
GitHub observation/history; it does not acquire the sibling's local live logs.
That distinction and freshness must remain visible. No combined cross-launcher
terminal is required. Running multiple assignments in one launcher would require
a snapshot version change from one assignment to a collection and corresponding
selection, log and process-lifecycle changes.

## Changes to the proposed design

- Define `parallel: true` as cooperative, private-workspace PR reading with
  different-role sharing on the same assigned head; writers and joins stay exclusive.
- Add destination removal, provisionally `target-remove`, to outcomes. Existing
  `remove` applies to the assignment, including issue-to-PR handoffs, so changing
  its meaning would silently affect other flows. Clear destination passes before
  publishing new gate triggers:

  ```yaml
  handed-off:
    add: [needs-visual-review, needs-security-review]
    target-remove: [visual-ok, security-ok]
  ```

- Make `all` consume trusted current-head, current-opening pass evidence, with
  a common opening for its producers. Pin that identity in claims and recovery;
  normalize handoff copies and invalidate old proofs even on an unchanged head.
- Add neutral cancellation and optional early supervisor termination; trigger
  disappearance is only quiet when a trusted superseding gate intent explains it.
- Order and recover fan-out, pass and cancellation mutations through a short
  transition claim, including cancellation of already-started stale transitions.
- Provide stale-join diagnostics and producer rerun guidance. Do not silently
  restore old proofs or promise automatic reruns after a push in this milestone.
- Version and validate the new coordination semantics. Old launchers would still
  elect one owner and cannot understand cancellation/ordering; all launchers for
  an affected project must stop, upgrade together and restart before enabling it.
  Missing fields are exclusive, not an opt-in inferred from current config.

## Proposed implementation split, smallest first

These are proposed scopes, **not filed issues**. Sizes include focused tests and
affected docs/site pages; estimates assume the existing cooperative trust model.
The table orders by size, not by dependency or shipping order.

| Proposed issue | One-line scope | Estimate |
| --- | --- | --- |
| Trigger configuration and wiring diagnostics | Normalize OR/`all`, validate parallel role restrictions and destination removal, and add graph-based doctor checks without enabling execution. | 1–2 days |
| Multiple-owner status and terminal display | Preserve per-role lease/process identity, explain join evidence and cancellation, and keep sibling actions visible in status and each launcher view. | 1–2 days |
| Neutral sibling cancellation | Record supersession without retry cost, poll bounded cancellation requests, stop and confirm only owned processes, and recover cleanup safely. | 2–3 days |
| Compatible PR leases and rollout | Share only compatible reader claims across separate serial launchers, preserve branch exclusion and recovery, and version the ownership protocol with upgrade instructions. | 3–5 days |
| Durable fan-out and current-head joins | Add opening provenance and target removal, ordered transition claims/replay, stale-pass invalidation and end-to-end crash/race tests for all four scenarios. | 4–6 days |

Land passive parsing/display foundations first. Cancellation depends on the new
lease and invalidation contracts; finish those contracts before enabling any
parallel role in project configuration. Ship the ownership, join and cancellation
pieces as one usable capability after coordinated launcher upgrade. Automatic
rearming, a multi-assignment launcher, cross-launcher log aggregation and stronger
external write fencing remain outside this first milestone.
