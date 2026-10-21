# Limits

Claim timing, timeouts and retries. Set them under `limits`; timeout and retry keys can also be set on one agent.

```yaml
limits:
  lease-minutes: 30
  lease-renew-minutes: 10
  agent-timeout-minutes: 180
  max-attempts: 5
  retry-backoff-seconds: 60
  max-backoff-seconds: 3600
```

## Timeout

How long one run may take, in minutes. Defaults to 180.

```yaml
agent-timeout-minutes: 60
```

## Attempts

How many failures in a row before an item stops and waits for `ub-agents retry`. Defaults to 5. A success resets the count.

```yaml
max-attempts: 5
```

## Backoff

The wait before the first retry, in seconds. It doubles after each failure, up to the maximum. Defaults to 60 and 3600.

```yaml
retry-backoff-seconds: 60
max-backoff-seconds: 3600
```

## Claims

How long a claim lasts and how often a live run renews it, in minutes. Defaults to 30 and 10, top level only.

```yaml
limits:
  lease-minutes: 5
  lease-renew-minutes: 2
```

The renewal interval must be more than 21 seconds shorter than the lease. If a launcher dies, another one picks the work up once the claim expires, so a shorter lease means faster takeover. Each claim records its own expiry, so launchers with different settings can share a repository. A run that hangs but stays alive keeps renewing; `agent-timeout-minutes` stops it.
