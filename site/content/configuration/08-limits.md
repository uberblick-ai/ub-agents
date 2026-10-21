# Limits

Claim timing, timeouts and retries for every agent. Set them under `limits`.
Timeout and retry keys can also be overridden on one agent; claim timing keys are top level only.

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

`lease-minutes` sets how long a claim lasts; `lease-renew-minutes` sets the renewal
interval. They default to 30 and 10 minutes. Both accept positive, finite numbers
and can only be set under the top-level `limits` mapping, never on an individual agent.

```yaml
limits:
  lease-minutes: 5
  lease-renew-minutes: 2
```

The renewal interval plus the renewal request bound (20 seconds and a one-second
margin) must be strictly shorter than the lease. Invalid settings fail configuration
loading and `ub-agents doctor --offline`.

Renewal continues throughout setup, execution, completion and cleanup, including
recovery and rate-limit waits. Each renewal extends the claim by the configured
lease without shortening its recorded expiry. Launchers with different settings
can share a repository because each claim records its own expiry. Discovery looks
back by the larger of 30 minutes and the configured lease, plus the recovery window.

If a launcher dies, another one picks the work up after the claim expires. A live
but silent run keeps renewing; use `agent-timeout-minutes` to limit its execution time.
