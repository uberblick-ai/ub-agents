# Polling and GitHub requests

Detailed discovery timing, request accounting and failure handling. Configure only
[`poll-seconds`](configuration.md#top-level); the budgets and retry limits below
are fixed. For day-to-day diagnostics, see [Running launchers](operations.md).

## Poll interval

`poll-seconds` measures the minimum time between the starts of successful
continuous discovery passes, including observation passes during a run.
A run's report, transitions and cleanup finish immediately. After a pass that ran
or recovered work, the next claiming pass starts immediately, whether the run
ended as success, retry or blocked. The interval then counts from that new pass's
start; the latest in-run observation does not delay it. Rate-limit waits, runtime
usage pauses, failed-poll retry backoff and graceful stops still apply. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
waits. Failed-poll retry delays below are independent of this interval, and
`launch --once` and `launch N` never wait after their pass.

When no open issue or PR has a configured trigger label, `launch` and
`launch --once` list the trigger labels and say to add one to start. Continuous
launch's idle message rounds its next poll delay to whole seconds below a minute
or whole minutes otherwise, such as `next poll in 56s` or `next poll in 2 min`.
Work already carrying a trigger retains its eligibility diagnostics.

## Discovery budget

Each launcher has one rolling discovery balance shared by its claiming loop and
observation worker. It starts full at **125 requests**, saves at most 125, and
refills at **250 REST requests/hour** (`5000 × 0.5 / 10`), or one request every
14.4 seconds. This fixed rule reserves half the common **5,000 requests/hour**
account quota for discovery by ten launchers, leaving the other half for work and
agents' own `gh` calls; it is not a configuration key.

Every discovery pass debits its reported quota-counted REST responses, including
claiming and poll-now passes, extra pages, failed passes, rate-limit retries and
approval-parking writes. HTTP 304 confirmations, GraphQL and transport failures
without an HTTP response do not debit it. The balance can go negative, and later
refills repay all debt. Successful claims' fresh checks, claim writes, renewals and
completion belong to the run and do not debit discovery.

Only idle and observation passes wait for admission: `poll-seconds` must have
passed since the previous pass started, and the balance must be at least **1**.
The launcher admits a whole pass without predicting its cost. The pass-start gap
remains capped at **one hour**, even with unpaid debt. Time already spent in the
pass, including rate-limit waits, counts toward the gap and refills the balance.
Claiming discovery immediately after work and accepted poll-now passes bypass this
admission wait and still debit the balance; actual rate-limit waits still apply.

For example, with the default 30 seconds and negligible pass duration:

- A cold 123-request pass leaves 2 requests. Thirty seconds later, refill adds
  about 2.08; the next one-request pass can start on time. Further one-request
  passes keep the 30-second gap, rather than inheriting a 29.5-minute wait.
- A 230-request pass leaves a balance of −105. Reaching 1 requires 106 refilled
  requests: 1,526.4 seconds, about 25 minutes 26 seconds after the debit.
- Three consecutive claiming passes spending 100 requests each, ignoring time in
  their runs, leave −175. They start immediately after each run; a later empty
  pass waits for 176 requests to refill, about 42 minutes 14 seconds. Poll-now
  passes add debt in the same way.

## Observation and request accounting

Observation passes during a continuous run share this balance, low-quota doubling,
one-hour cap and rate-limit waits. Their first admission counts from the claiming
pass's start, using the remaining balance rather than the claiming pass's cost.
They evaluate the whole ranked
queue without claiming, recovering, approval-parking or printing plan or idle lines.
Each pass reports its kind (`claiming`, `empty` or `observation`), wall time,
attempted `gh` calls, quota-counted REST responses, HTTP 304 responses, GraphQL
calls and candidates reached in launch output. Candidates count distinct items
reached for evaluation, including items that fail evaluation, rather than agent rows.
Every attempted invocation counts once, including extra pages, failed requests
and an unconditional refetch after a 304 without a cached response. GraphQL calls
count invocations, not GraphQL quota points. Consecutive passes with the same kind,
counts and candidate count print once even when their wall times differ.
For a successful claim, discovery counts stop before its fresh claim checks
begin; those checks, the claim and subsequent launcher traffic
through release belong to the run. Unsuccessful claim attempts remain part of an
empty pass, or of discovery before a later successful claim. Observation passes
measure their own client. The run's `events.jsonl` `released` event records
`github_requests` with `gh_calls`, `quota_requests`, `not_modified_responses` and
`graphql_calls` for the launcher's client, including renewals and finalization.
It excludes observation-worker and agent requests, and the display refresh after
release. Use these measured counts when budgeting busy launchers sharing an account;
agents' own requests still need a separate allowance.
Only completed observation passes replace the snapshot's queue rows; a failed or
rate-limited pass retains the previous rows and cannot interrupt execution,
heartbeats, reporting, transitions or cleanup, or stop the launcher.
After the run, fresh claiming discovery starts immediately, retaining any active
observation rate-limit wait. An empty claiming pass resumes the budget pacing above.

## Low-quota pacing

The launcher retains `X-RateLimit-Remaining`, `X-RateLimit-Limit` and
`X-RateLimit-Reset` from the latest response for each `X-RateLimit-Resource`
(`core`, `graphql`), read through `gh api --include`. It makes no `GET /rate_limit`
probe. If any resource has **less than 20%** remaining, the empty-pass gap doubles.
The extra wait stops at that resource's reset (the earliest reset if several are
low), never shortens the ordinary gap, and remains capped at **one hour**.
Resources whose reset has passed, and incomplete or unreadable quota headers,
do not add a wait.

When the launcher becomes idle, and again when the set of low-quota resources
changes, it prints `No eligible work; next poll in <n> min (<k> requests last poll)`.
The request count measures REST quota usage, excluding HTTP 304 confirmations.
It does not repeat the message on every empty pass. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
this idle wait.

## Discovery inputs and caching

Claiming discovery evaluates candidates in rank order and stops once it claims
work. During that run, separate read-only observation passes evaluate all rows;
their results never choose the next claim. After the run, fresh claiming discovery
again walks the rank order and rechecks authority before any write. Lower-ranked
rows are announced and approval-parked only by a claiming pass that reaches them.
The observation worker starts with independent copies of the launcher's discovery
inputs, REST ETags, repository comment cache and comment cursor, plus the per-item
comment store, reconciliation state and first successful scan's window start.
Its first scan continues from that cursor; new claim comments invalidate the claimed item's
inputs. Later cache changes stay local to each client, and the worker keeps its
own request counters and rate-limit state.
`launch --once` and `launch N` do not start observation passes; `status` still
evaluates every row and remains read-only.
Within each pass, an item's comments supply history and approval input, and
`status` renders the same history. Fresh repository permissions are read once per
account across all reached items. These shared reads end with the pass; each
claim-time approval check reads permissions anew.
Discovery serves issue and PR conversation comments from a separate per-item
store when the item's creation falls within the first successful scan's window,
or a per-item read has reconciled it, and its stored row count matches the open
issue list's valid comment count. Otherwise it reads the item and replaces its
stored rows exactly. Missing or invalid counts and items absent from the open list
always require that fallback. PR reviews and inline review comments remain
per-item reads.
Each launcher retains per-item discovery inputs in memory: history, approval
inputs and permissions, PR details, and dependency links. Record authors' roles and
the launcher's own role are checked freshly each pass, including on unchanged items.
Changes in the issue list (including `updated_at`) or an item's comment IDs and
update times invalidate that item's reads. Stored comment rows do not age out
with the repository history's recovery lookback; they leave the store when the
item is no longer open or referenced by the repository scan. A deleted row causes
a count mismatch and a per-item repair. A fresh claim-approval denial also drops
the item's cached inputs so the next reached pass can plan its gate. Claims and approval parking
always revalidate with fresh reads;
cached input never authorizes a claim or a write. Restarting a launcher drops its
cache. With configured priorities, cold discovery lists the dependency graph in
pages to preserve inheritance without one REST request per queued issue.

## Capacity planning

For a rough request budget, an unchanged warm pass sends one request per page of
open issues/PRs, plus the incremental repository comment scan (usually one page),
an optional milestone list, and one permission read per distinct listed record
author (all marked-record authors when `launchers` is omitted). Checking a ready
launcher's own account can add one permission read if it was not already read.
List pages hold up to 100 rows; a full REST page
also needs a request to check for a following page. A cold pass or changed item adds
roughly 2–6 reads for each candidate actually reached when the comment store is
complete, plus one permission read per distinct account across those candidates.
An older item without reconciliation, a comment-count mismatch, a missing or
invalid count, or an item absent from the open list adds a per-item conversation
comment read, with extra pages for long histories. A successful fallback repairs
the store so later passes with matching valid counts can reuse it.
Unchanged REST reads with an ETag consume no quota when GitHub confirms freshness
with HTTP 304; the incremental comment scan's moving `since` cursor skips ETag
caching. Configured priorities add a paginated dependency-graph list on cold
discovery and each `status` invocation; very large dependency lists may
need extra pages. Fresh claim/recovery reads, approval-parking writes, execution
heartbeats and completion add their own requests. `status` pays for every row.

Discovery pacing targets an **average of at most 250 requests/hour** per launcher,
not a ceiling in each rolling hour. Apart from the one-hour cap exception, over any
interval `T`, discovery spend is at most `125 + 250/hour × T`, plus the largest
admitted pass and exempt spend since the last admitted pass. Later idle waits repay
exempt claiming and poll-now spend. On a quiet repository, a warm pass costs about
one quota-counted request, or 120 requests/hour at the default 30-second interval.
On a busy repository, a warm pass costs a few requests; the 250/hour budget,
rather than `poll-seconds`, then paces discovery.
The initial 125-request balance absorbs a cold burst, while a pass above 125 can
leave debt. Sustained 300-request passes eventually need more than an hour to repay
each pass; they start at the one-hour cap with debt still outstanding. Passes that
keep costing more than 250 are therefore the exception to the average guarantee.
Sum all launchers using the account, including other repositories, and add
execution/write costs and agents' calls. For many short runs, put launchers beyond
about seven on a second account. See
[How many launchers one account supports](../site/content/best-practices/08-account-capacity.md)
for the measured per-run cost and the agent-call allowance behind this threshold.
GraphQL has a separate point budget; graph-list query cost depends on its
connections. Long runs reduce discovery frequency.

## Transient failures

Continuous `ub-agents launch` retries failed discovery polls, claim POSTs and
completed-run finalization requests for
request timeouts, connection failures, empty or truncated responses reported by
`gh` as `unexpected end of JSON input`, and HTTP 5xx responses. The fixed backoff
starts at **5 seconds**, doubles after each consecutive failure and caps at
**60 seconds**. The launcher stops
on the **sixth consecutive failed poll**; a completed poll resets the count, including
one that finds no work. These values are not configuration keys and are independent
of agent execution retries under `limits`.

## Rate-limit waits

A **403 or 429** is a rate limit when the response has `X-RateLimit-Remaining: 0`,
`Retry-After`, or GitHub's "API rate limit exceeded" or "secondary rate limit"
message. Headers on real requests are authoritative; the launcher does not use
`GET /rate_limit`. Primary limits wait until `X-RateLimit-Reset` plus **5 seconds**
of margin. Secondary limits wait for `Retry-After` seconds. Missing or unreadable
wait metadata falls back to **one minute**. Each wait is capped at **one hour**;
afterward the read is retried, and another rate limit starts another wait.

Time spent waiting for a rate limit counts toward the `poll-seconds` or empty-pass
budget gap between discovery-pass starts. An empty pass waits only for any gap
still left. A pass that ran or recovered work starts the next pass immediately
after any active rate-limit wait.

Rate-limited reads do not count toward the poll failure limit or an item's attempts.
Each wait prints `GitHub rate limit reached; waiting until <reset UTC> (<n> min)`
and makes no GitHub writes while waiting. Authentication, permission, missing
repository, other malformed responses and unclassified failures still stop immediately.
The error names the request and tells the operator to fix the cause and restart
`ub-agents launch`.

Each skipped poll for another transient error prints its error and next delay
and does not report an empty queue. Failed discovery reads make no GitHub writes.
Discovery includes initial authentication and fresh reads immediately before a
claim, including the default-branch read for instruction refresh. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
discovery waits. `launch --once` and `status` still fail on their first error.

## Failed writes and owned runs

A retryable claim POST failure ends the pass with the same skipped-poll message
and counts toward the same failure limit. Before planning on the next pass, the
launcher checks whether that claim was created. If present, it withdraws the claim
with `state: withdrawn` and summary `Claim response was lost; withdrawn`.
This spends no attempt and adds no item backoff, so the item can be claimed again
on that pass. If the launcher restarts first, the claim expires normally.
Failures during active execution retain their existing handling. After execution
and cleanup finish, failed GitHub requests preserve the recorded outcome and use
this same poll backoff; lease expiry permits outcome-only recovery without
rerunning the agent. See [recovery](coordination.md#recovery).

From claim election through release, including completion recovery, rate-limited
reads wait and retry under the active lease, even when the reset is beyond its
current expiry. Renewal continues during the wait. If the last confirmed expiry
actually passes, the launcher takes the lost-ownership path and leaves expiry recovery
to finish durable completion. See
[Stopping and restarting](operations.md#stopping-and-restarting) for signals during
owned-run waits. Rate-limited writes other than claim POSTs retain their existing
handling and are not replayed by this retry mechanism.

