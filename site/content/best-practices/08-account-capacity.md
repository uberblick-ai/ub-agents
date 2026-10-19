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

The budget allows bursts. See the [polling reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md#shared-account-budget) for pacing rules.

## Finished runs

The other **2,500 requests/hour** pays for work. Budget **about 35 quota-counted requests per run**, including claiming: roughly **70 finished runs/hour** across the account before agents' own calls.

For short runs, assume **about 6 finished runs/hour per launcher**. Allowing for agents' calls gives roughly **42 runs/hour**, or **seven busy launchers**. Other account activity reduces this allowance.

Use your own run lengths and request costs when sizing. The [0.2.0 measurements in #421](https://github.com/uberblick-ai/ub-agents/issues/421) contain the tables, measurement method and calculations. The [launch-output reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output) explains the recorded counts.

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
