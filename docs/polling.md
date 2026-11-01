# Polling contract

Reference for changing discovery without weakening authority checks or exhausting a
shared GitHub account. The [poll interval](https://agents.uberblick.ai/docs/configuration/index.html#poll-interval)
page defines the setting, default and validation.

## Flow and authority

- Claiming discovery walks the queue in rank order and stops at the first successful
  claim. After running or recovering work, it starts again at once, subject to
  rate-limit waits, failed-poll backoff, runtime pauses and stopping. The next
  interval counts from that new pass.
- During an assignment, continuous launch observes the whole queue. Observation
  never claims, recovers or approval-parks. Only a completed observation replaces the
  displayed rows; a failed one keeps the old rows without interrupting the run.
- `launch --once` and `launch N` have no observation and no wait after the pass.
  `status` is read-only.
- Cached discovery input never authorizes writes. Claim and approval-parking checks
  reread authority, and account permissions are read fresh each pass and at claim
  time.

## Keep discovery cheap

Reuse unchanged item history, approval inputs, PR details and dependency links.
Invalidate them on issue-list changes, including `updated_at`, on comment IDs or
timestamps, new claim comments or a fresh approval denial. These caches live in
memory, and a restart discards them. Observation works on its own copies, so its
changes stay local.

Discovery scans repository comments with a moving `since` cursor. It serves an
item's conversation comments from memory only when they are known complete and their
count matches the open issue list; otherwise it rereads them. PR reviews and inline
review comments are always per-item reads.

Share account permission reads within a pass. REST GETs revalidate with the stored
ETag. A 304 response uses no quota and counts as a fresh read, including the
ownership read before a durable write. Writes, GraphQL and the moving `since` scan
are never conditional. With priorities configured, cold discovery and `status` read
dependency links through paginated GraphQL instead of one REST request per issue.
Pagination, changed or older items and long histories still add reads, so do not
assume a fixed cost per pass.

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

The refill reserves half of a common 5,000-request hourly quota for discovery across
ten launchers. It is an average, not a rolling-hour ceiling. Size a shared account
with [account capacity](https://agents.uberblick.ai/docs/best-practices/account-capacity.html).

## Quota and failure handling

Use headers from real requests via `gh api --include`; never probe
`GET /rate_limit`. Track remaining, limit and reset separately for `core` and
`graphql`.

| Condition | Required behavior |
| --- | --- |
| Any resource below 20% remaining | Double the idle/observation gap until the earliest applicable reset; never shorten the ordinary gap or exceed its one-hour cap. Ignore expired/incomplete headers. |
| Rate-limited 403/429 | Recognize zero remaining, `Retry-After`, or GitHub's primary/secondary rate-limit message. Primary: reset plus **5 seconds**. Secondary: `Retry-After`. Missing wait metadata: one minute. Cap each wait at one hour, then retry. |
| Retryable continuous-launch failure | A failed gh call without an HTTP status (regardless of its text), request timeouts, truncated/empty JSON reported by gh, and HTTP 5xx: back off from **5 seconds** to **60 seconds**, doubling, without a retry limit. A completed poll resets the delay. |
| Configuration or instruction errors after refresh | Continuous launch pauses all claims, including recoveries, and refreshes and validates on every poll or `r`; valid files resume claims. See [configuration pauses](https://agents.uberblick.ai/docs/guides/running-launchers.html#when-things-go-wrong). Startup validation, `launch --once` and `launch N` still fail. |
| Other errors | gh authentication required (exit 4), a gh binary that cannot run, non-rate-limited HTTP 4xx and other malformed responses stop immediately. `launch --once` and `status` fail on their first request error. |

Only reads wait and retry. A rate-limited write is not replayed automatically.
Rate-limit waits charge no poll failures or item attempts and make no writes. They
count toward the discovery gap. Observation failures never stop the launcher. A
failed discovery pass makes no GitHub writes and reports the error and the next
delay, never an empty queue.

A retryable failure of the claim POST uses the same poll backoff. Before replanning,
the launcher checks whether the claim was created and withdraws it, charging no
attempt or item backoff. Failures after a claim follow
[recovery](coordination.md#recovery); see also
[signal handling](https://agents.uberblick.ai/docs/guides/running-launchers.html#stopping-and-restarting).

## Accounting and diagnostics

Pass records distinguish claiming, empty and observation passes. They report wall
time, attempted `gh` calls, quota-counted REST responses, HTTP 304 responses,
GraphQL invocations (not points) and distinct candidates reached, counting pages,
failures and refetches. Successful claim checks through release belong to the run;
unsuccessful claims remain discovery. Consecutive records with the same kind, counts
and candidates print once.

The run's `released` event records `github_requests` with `gh_calls`,
`quota_requests`, `not_modified_responses` and `graphql_calls`. These cover the
launcher's claim checks, renewals and finalization, not observation, agent calls or
display refresh after release. See [Running launchers](https://agents.uberblick.ai/docs/guides/running-launchers.html#launch-output)
for operator messages and logs.
