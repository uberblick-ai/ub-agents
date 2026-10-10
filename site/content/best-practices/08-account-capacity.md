# How many launchers one account supports

One GitHub account supports **up to ten launchers** under ub-agents' discovery
budget. Count launchers across every machine and repository using that account.
For many short runs, put launchers beyond **about seven on a second account** so
claiming and completion traffic have room alongside discovery and agents' calls.

## Idle discovery

On a quiet repository, a warm idle pass costs about one quota-counted request,
or **120 requests/hour** at the default 30-second poll interval. On a busy
repository, other launchers and agents keep changing items, so a warm pass costs
a few requests. The **250/hour discovery budget**, rather than `poll-seconds`,
then paces discovery. This budget leaves half of the account's usual 5,000/hour
for work when ten launchers share it.

The [0.2.0 measurements](https://github.com/uberblick-ai/ub-agents/issues/421)
found warm empty passes costing p50 1 on ub-agents, but 1–7 on uberblick-2
(p50 2, mean about 2.6 across 46 passes). At 30 seconds, that busy-repository
cost would use about 240–310 requests/hour before budget pacing.

Each launcher starts with a 125-request balance, shared by claiming and in-run
observation passes, and refills at 250/hour. Every pass debits its reported REST
quota cost. Idle and observation passes wait for the poll interval and a balance
of at least 1, then admit the whole pass without predicting its cost. A cold
123-request pass can be followed by one-request passes every 30 seconds. A pass
above 125 can leave debt that subsequent waits repay.

Claiming immediately after work and accepted poll-now passes bypass admission and
still debit. Their spend can exceed the average until later idle waits repay it.
This is not a strict rolling-hour ceiling: over any interval `T`, the bound is
`125 + 250/hour × T`, plus the largest admitted pass and exempt spend since the
last admitted pass. The one-hour gap cap is the exception: passes that keep
costing more than 250 requests can resume with debt still outstanding.

## Finished runs

The other **2,500 requests/hour** pays for work. Across all launchers on an account,
the rough capacity is:

```text
finished runs/hour ≈ 2,500 ÷ per-run request cost
```

Budget **about 35 quota-counted requests per run**, including the claiming pass.
The source is the `released` events of the **0.2.0 launchers on `uberblick`**,
from 2026-10-09T19:44Z to 2026-10-10T13:45Z, recorded in
[#421](https://github.com/uberblick-ai/ub-agents/issues/421):

| `github_requests` per run | Runs | p50 | p90 | Max |
|---|---|---|---|---|
| uberblick-2 `quota_requests` | 86 | 31 | 36 | 38 |
| ub-agents `quota_requests` | 11 | 29 | 31 | 36 |
| uberblick-2 `graphql_calls` | 86 | 4 | 5 | 9 |

The separately logged claiming pass cost p50 5 quota-counted requests on
uberblick-2 (87 passes) and 4 on ub-agents (12 passes). Adding it to the run's
`github_requests.quota_requests` gives the rounded **35 requests per run**.
Runs lasted up to 47 minutes and even the longest stayed under 40 quota-counted
requests in its `released` event, including renewals.

```text
finished runs/hour ≈ 2,500 ÷ 35 ≈ 70
```

This is a planning allowance across the account's launchers, not a measured
throughput limit. Measure your own per-run cost from the `released` events in
runs' `events.jsonl`: use `github_requests.quota_requests`, added by
[#400](https://github.com/uberblick-ai/ub-agents/issues/400), plus the claiming
pass. The event counts cover the launcher's fresh claim checks, claim, renewals
and completion. They exclude discovery, the observation worker and the agent's
own `gh` calls. GraphQL calls use a separate point budget. The
[launch-output reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output)
describes the recorded counts.

For short runs, assume **about 6 finished runs/hour per launcher**: uberblick-2's
p50 run of 9.4 minutes plus about 1 minute of handoff, from
[#403](https://github.com/uberblick-ai/ub-agents/issues/403), gives
`60 ÷ (9.4 + 1) ≈ 6`. On launcher traffic alone, `70 ÷ 6 ≈ 12` busy launchers
fit, more than the ten-launcher discovery limit.

Agents' own `gh` calls share the account's quota and need a separate allowance;
the launcher does not count them per run. #403 measured p50 6 and p90 24 agent
calls per run on uberblick-2, part of them GraphQL. Counting all 24 p90 calls as
REST requests gives a conservative allowance:

```text
finished runs/hour ≈ 2,500 ÷ (35 + 24) ≈ 42
busy launchers ≈ 42 ÷ 6 ≈ 7
```

For many short runs, put launchers beyond **about seven on a second account**.
Other account activity reduces this allowance; use your own measured run lengths
and launcher and agent request costs to size it.

Beyond the account's available hourly quota, launchers pause until GitHub's
hourly reset and then continue. Adding launchers to the same account does not
increase that allowance.

## Why the account matters

GitHub's usual **5,000 REST requests/hour per user** are shared by every token
of that user, including agents' own `gh` calls. Separate tokens for the same
account do not provide separate quotas. See
[GitHub's primary rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-authenticated-users).

Authenticated conditional requests answered with **HTTP 304** do not consume
primary quota. This is why unchanged discovery is cheap even when a pass sends
several requests. GitHub also recommends serial requests to avoid secondary
limits. See
[GitHub's REST API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).

The REST secondary limit includes **900 points/minute**; most reads cost one
point and most writes cost five. Ten launchers cold-starting together can briefly
reach it even with hourly quota remaining. They wait out the secondary limit
and continue according to GitHub's retry headers. See
[GitHub's secondary rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#about-secondary-rate-limits).
