# Loop events

Run a project command when work starts or a run finishes, for example to notify Slack.

```yaml
events:
  command: [mise, run, notify-slack]
  on: [started, outcome]
  timeout-seconds: 10
```

## Command and subscriptions

`command` is a required argument list, run without a shell in the control checkout.
`on` is a required non-empty list containing `started`, `outcome`, or both. The timeout
defaults to 10 seconds and must be positive, finite, and at most 60 seconds.

Each invocation receives one JSON object on stdin. The command inherits the launcher's
environment with `UB_AGENTS_*` variables removed. Read credentials from your project's
secret store; keep webhook URLs and tokens out of the configuration.

## Events

`started` is sent after winning the claim and recording the run as started, before
worktree setup. This includes automatic `on-conflict` routing. Election losers,
withdrawn claims, approval parking, failed outside-input label writes, and recovery
claims send no start event.

`outcome` is sent when the launcher releases a run, after applying any transition.
It covers agent reports, launcher verdicts such as timeouts and rejected reports,
and recovery runs. Each launcher emits only for runs it owns.

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

Hooks run asynchronously, one process at a time per launcher, in event order. A slow,
missing or failing command only prints an `Events hook:` notice. It cannot change the
run's verdict, labels, claim, lease or cleanup, or hold up further claims. Timeouts
stop the hook's process group, and successful commands cannot leave background children.

On exit, the launcher drains hooks within one timeout budget, at most 60 seconds,
then stops remaining processes and discards queued events. No hook process outlives
shutdown. A crash can lose or repeat an event, so tolerate both. Your command can use
`(repository, run, event)` to recognize duplicates.
