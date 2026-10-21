# How many launchers one account supports

One GitHub account supports **up to ten launchers**, counted across every machine and repository using it. For many short runs, put launchers beyond **about seven on a second account**.

## Why the account matters

GitHub allows about **5,000 REST requests/hour per user**, shared by every token of that user, including agents' own `gh` calls. Separate tokens don't add quota. See [GitHub's rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-authenticated-users).

## Idle discovery

Unchanged discovery is cheap, because GitHub doesn't count `304 Not Modified` answers. A quiet repository costs about **120 requests/hour** at the default 30-second poll. On a busy one, a **250/hour discovery budget** paces polling, which leaves half the account's quota for work when ten launchers share it. See the [polling reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md#shared-account-budget).

## Finished runs

Budget **about 35 requests per run**, including claiming: roughly **70 finished runs/hour** from the other 2,500 before agents' own calls. With those calls and about 6 short runs/hour per launcher, that is **seven busy launchers**. The [0.2.0 measurements in #421](https://github.com/uberblick-ai/ub-agents/issues/421) show the method; use your own run lengths when sizing.

## When the quota runs out

Launchers pause until GitHub's hourly reset, then continue. Ten launchers starting together can also briefly hit GitHub's per-minute [secondary limit](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#about-secondary-rate-limits); they wait it out as GitHub's retry headers say.
