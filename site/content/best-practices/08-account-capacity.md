# How many launchers one account supports

One GitHub account supports **up to ten launchers** under ub-agents' discovery
budget. Count launchers across every machine and repository using that account.
For many short runs, put launchers beyond **about six on a second account** so
claiming and completion traffic have room alongside discovery and agents' calls.

## Idle discovery

A warm idle launcher at the default 30-second poll interval uses about **120
requests/hour**, assuming one quota-counted response per pass. Discovery targets
an average of at most **250/hour** per launcher, leaving half of the account's
usual 5,000/hour for work when ten launchers share it.

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

Using the **estimate of about 70 requests per run**, this is about **35 finished
runs/hour** across all launchers. This is an estimate, not a measured throughput
limit. Long runs need more lease renewals; agents' own GitHub requests and other
account activity reduce the allowance.

Replace the estimate with your measured per-run cost from the `released` events
in runs' `events.jsonl`: use `github_requests.quota_requests`, added by
[#400](https://github.com/uberblick-ai/ub-agents/issues/400). These counts cover the
launcher's fresh claim checks, claim, renewals and completion. They exclude
discovery, the observation worker and the agent's own `gh` calls, so leave room
for agent requests separately. The
[launch-output reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output)
describes the recorded counts.

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
