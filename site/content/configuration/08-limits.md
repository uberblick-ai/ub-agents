# Limits

Timeouts and retries for every agent. Set them under `limits`, or on one agent to override.

```yaml
limits:
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

Not configurable. A claim lasts 30 minutes and renews every 10 minutes while the run is alive. If a launcher dies, another one picks the work up after the claim expires.
