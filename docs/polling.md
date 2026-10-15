# Polling contract

Agent reference for changing discovery without weakening authority checks or
exhausting a shared GitHub account. Configuration exposes only
[`poll-seconds`](configuration.md#top-level), default 30 seconds.

## Flow and authority

- Claiming discovery walks queue rank and stops at a successful claim. After running
  or recovering work, it starts again immediately, subject to active rate-limit
  waits, failed-poll retry backoff, runtime pauses and stopping. Reporting, transitions and cleanup do not wait
  for a poll interval. The next interval starts at that new claiming pass, not the
  latest observation.
- Continuous launch observes the whole queue during assignments. Observation never
  claims, recovers or approval-parks work, and never chooses the next claim. Only a
  completed observation replaces displayed rows; failure retains old rows without
  interrupting execution or completion. The first observation interval counts from
  the claiming pass’s start.
- `launch --once` and `launch N` have no observation worker or post-pass wait.
  `status` evaluates its requested scope read-only. Claiming alone announces or
  approval-parks the candidates it reaches.
- Cached discovery input never authorizes writes. Claim and approval-parking checks
  reread authority; account permissions are fresh each pass and again at claim time.

## Keep discovery cheap

Reuse unchanged item history, approval inputs, PR details and dependency links.
Invalidate on issue-list changes (including `updated_at`), comment IDs/timestamps,
new claim comments or fresh approval denial. Restart discards these memory caches.
Observation starts from independent copies of discovery inputs, ETags, repository
comment cursor, per-item store and reconciliation state; later changes stay local.

Serve conversation comments from the per-item store only when the item is covered
by the first successful scan's window or reconciled by a per-item read, and its row
count matches the open list's valid comment count. Otherwise reread and replace
its rows. Missing/invalid counts and items absent from the open list require this
fallback; PR reviews and inline comments remain per-item reads. Retain stored rows
while the item is open or referenced by the repository scan, beyond recovery
lookback. Deleted comments cause count mismatch and repair.

Share account permission reads within a pass. Use conditional REST reads: confirmed
HTTP 304 responses consume no quota. The moving `since` comment scan is not ETag
cached. With priorities configured, cold discovery and status use paginated dependency
graph reads instead of one REST request per issue. Pagination, changed/older items
and long histories still add reads; do not assume a fixed per-pass cost.

## Shared-account budget

Each launcher has one discovery balance shared by claiming and observation:

| Rule | Value / effect |
| --- | --- |
| Initial balance / maximum credit | **125 requests** |
| Refill | **250 REST requests/hour**; negative balances retain debt |
| Idle/observation admission | `poll-seconds` elapsed since the preceding start and balance at least 1; admit the whole pass without predicting cost |
| Maximum admission gap | One hour, even with debt; time inside passes and rate-limit waits counts |
| Bypass admission | Claiming immediately after work and accepted poll-now passes; both still debit |
| Debit | Quota-counted REST responses, including pagination, failed passes, retries and approval parking |
| Exclude | HTTP 304, GraphQL, transport failures without a response, and successful claim checks through release |

The refill reserves half a common 5,000-request/hour account quota for discovery
across ten launchers. It is an average target, not a rolling-hour ceiling: bursts,
exempt passes and sustained passes above 250 requests at the one-hour cap can exceed
it. Sum all launchers across repositories and separately budget agent calls,
execution writes/renewals and GraphQL points. See [account capacity](../site/content/best-practices/08-account-capacity.md)
for measured sizing rather than assuming ten busy launchers are always affordable.

## Quota and failure handling

Use headers from real requests via `gh api --include`; never probe `GET /rate_limit`.
Track remaining/limit/reset independently for `core` and `graphql`.

| Condition | Required behavior |
| --- | --- |
| Any resource below 20% remaining | Double the idle/observation gap until the earliest applicable reset; never shorten the ordinary gap or exceed its one-hour cap. Ignore expired/incomplete headers. |
| Rate-limited 403/429 | Recognize zero remaining, `Retry-After`, or GitHub's primary/secondary rate-limit message. Primary: reset plus **5 seconds**. Secondary: `Retry-After`. Missing wait metadata: one minute. Cap each wait at one hour, then retry. |
| Retryable continuous-launch failure | A failed gh call without an HTTP status (regardless of its text), request timeouts, truncated/empty JSON reported by gh, and HTTP 5xx: back off from **5 seconds** to **60 seconds**, doubling, without a retry limit. A completed poll resets the delay. |
| Other errors | gh authentication required (exit 4), a gh binary that cannot run, non-rate-limited HTTP 4xx, configuration errors and other malformed responses stop immediately. `launch --once` and `status` fail on their first error. |

Rate-limit waits spend no poll failures or item attempts and make no waiting-path
writes. They count toward the discovery gap. Observation failures never stop the
launcher. Failed discovery reads make no GitHub writes; skipped polls report the
error and next delay, never an empty queue. These unlimited retries are independent
of bounded agent execution retries.

A retryable claim POST failure uses the same unlimited poll backoff. Before replanning,
reconcile whether the claim was created; withdraw it if present without charging
an attempt or item backoff. A restart leaves normal lease expiry to recover it.
Other retryable failures after a claim write, during election, execution or
finalization, end that pass and continue polling. A failed ownership check caused
by a retryable GitHub failure also follows this policy. Stop the owned process
and renewal, make no further writes for that lease, and leave it to expiry and
durable-outcome recovery. No second run starts while its lease is live.
Completed-run finalization preserves the durable outcome; expiry recovery
finishes completion without rerunning the agent. Rate-limited owned reads retry
while renewal continues; expiry triggers lost ownership, not further writes.
Other rate-limited writes are not automatically replayed. See
[recovery](coordination.md#recovery) and [signal handling](operations.md#stopping-and-restarting).

## Accounting and diagnostics

Pass records distinguish claiming, empty and observation; report wall time, attempted
`gh` calls, quota REST responses, HTTP 304, GraphQL invocations (not points), and
distinct candidates reached. Count pages, failures and unconditional refetches.
Successful claim checks through release belong to the run; unsuccessful claims
remain discovery. Observation measures its own client.

The run's `released` event records `github_requests` with `gh_calls`, `quota_requests`,
`not_modified_responses` and `graphql_calls`: launcher claim checks, renewals and
finalization, excluding observation, agent calls and post-release display refresh.
Consecutive pass records with the same kind, counts and candidate count print once,
even if duration differs. Operator messages and logs belong in
[Running launchers](operations.md#launch-output).
