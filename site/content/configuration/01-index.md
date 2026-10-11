# Configuration

`ub-agents.yaml` sits at the root of your repository. `ub-agents init` writes a starter file and `ub-agents check` validates it. Unknown keys are errors.

```yaml
repository: your-org/your-project
shared-instructions: .agents/ub_agents.md

agents:
  implementer:
    runtime: "codex:gpt-6.1-sol:high"
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md
    worktree: true
    outcomes:
      handed-off: {add: [needs-review]}

poll-seconds: 30
stop-labels: [needs-human]
```

## Repository

The GitHub repository the agents work on, as `owner/name`. It must match your checkout's `origin`.

```yaml
repository: your-org/your-project
```

## Agents

The roles that do the work, by name. See [Agents](/docs/configuration/agents.html).

```yaml
agents:
  implementer:
    ...
```

## Shared instructions

A policy file every agent reads before its own instructions: checks, merge policy and who decides. See [Instructions](/docs/configuration/instructions.html).

```yaml
shared-instructions: .agents/ub_agents.md
```

## Poll interval

The minimum gap between discovery-pass starts, in seconds; defaults to 30.
After running or recovering work, claiming resumes immediately. Idle and
observation passes can wait longer for request budget or low quota; rate-limit
waits and runtime usage pauses still apply. This is not an agent retry setting.
See [polling rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md)
for exact timing and [account capacity](/docs/best-practices/account-capacity.html)
for sizing a shared account.

```yaml
poll-seconds: 30
```

## Stop labels

Labels that park an item until a person acts. Defaults to `needs-human`. See [Stop labels](/docs/configuration/stop-labels.html).

```yaml
stop-labels: [needs-human]
```

## Everything else

All optional:

| Key | Page |
|---|---|
| `limits` | [Limits](/docs/configuration/limits.html) |
| `queue` | [Queue](/docs/configuration/queue.html) |
| `approvals`, `trusted-bots` | [Approvals](/docs/configuration/approvals.html) |
| `launchers` | [Launcher accounts](/docs/configuration/launchers.html) |
| `cleanup` | [Cleanup hook](/docs/configuration/cleanup.html) |
| `checkout-setup` | [Checkout setup](/docs/configuration/checkout-setup.html) |
| `runtime-updates` | [Runtime updates](/docs/configuration/runtime-updates.html) |
| `runtime-args` | [Per-CLI argument defaults](/docs/configuration/permissions.html#arguments-for-alternatives) |

## Another file

Every command reads `ub-agents.yaml` by default. Pass `--config` to use another file.

```sh
ub-agents --config staging.yaml launch
```
