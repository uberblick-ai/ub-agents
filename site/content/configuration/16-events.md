# Loop events

Run a project command when work starts or a run finishes, for example to notify Slack.

```yaml
events:
  command: [mise, run, notify-slack]
  on: [started, outcome]
  timeout-seconds: 10
```

## Command and subscriptions

`events` is an optional mapping; without it, no hook runs.

`command` is a required argument list, run without a shell in the control checkout.
`on` is a required non-empty list containing `started`, `outcome`, or both. The timeout
defaults to 10 seconds and must be positive, finite, and at most 60 seconds.

Each invocation receives one JSON object on stdin. The command inherits the launcher's
environment with `UB_AGENTS_*` variables removed. Read credentials from your project's
secret store; keep webhook URLs and tokens out of the configuration.

## Events

`started` announces a new run before worktree setup. `outcome` announces the
released result after any transition, including launcher failures and recovery.
It describes the released verdict even when supervision superseded an early report.
Each launcher emits only for runs it owns. See the [event contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#loop-events-hook)
for exact emission and recovery rules.

| Field on every event | Value |
|---|---|
| `event` | `started` or `outcome` |
| `repository` | `owner/name` |
| `run`, `agent` | Run id and agent name |
| `assignment`, `kind`, `url` | Issue or PR number, `issue` or `pr`, and its GitHub URL |
| `attempt` | Run's attempt number |
| `mode` | `recovery` for recovery runs, otherwise `null` |

| Additional outcome field | Value |
|---|---|
| `status` | Released result: `success`, `retry` or `blocked` |
| `outcome` | Declared outcome whose transition was applied, otherwise `null` |
| `summary` | Released summary |
| `actions`, `options` | Report's asks, `[]` when absent |
| `handoff` | Handoff PR number or `null` |
| `labels_added`, `labels_removed` | Applied transition's resolved labels, including trigger removal and a stop label substituted by `max-rounds`; `[]` without a transition |

Recovery uses the recovery run's identity and summary, with the source report's asks,
handoff and transition.

## Filtering and delivery

Filter in your command: post `started` only when `agent` is `issue-preparer`, or
`outcome` only when `labels_added` contains `needs-human`.
[Run a command on loop events](/docs/guides/loop-events.html) walks through a
complete example.

Delivery runs asynchronously. Slow or failing hooks print an `Events hook:`
notice and leave the run's result unchanged. Events may be lost or repeated;
make your command tolerate both and use `(repository, run, event)` to recognize
duplicates. The [delivery contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#loop-events-hook)
defines ordering, timeout and shutdown guarantees.
