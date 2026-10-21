# Configuration

`ub-agents.yaml` sits at the root of your repository. `ub-agents init` writes a starter file, and `ub-agents doctor --offline` validates it. Unknown keys are errors.

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

HTTPS and SSH remotes both work, and so do SSH host aliases such as `git@github-work:OWNER/REPO.git`, as long as `ssh -G` resolves them to `github.com`.

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

The minimum gap between queue checks, in seconds; defaults to 30. See [polling rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md) for timing and [account capacity](/docs/best-practices/account-capacity.html) for sizing a shared account.

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
| `trusted-pr-authors` | [Trusted PR authors](/docs/configuration/approvals.html#trusted-pr-authors) |
| `launchers` | [Launcher accounts](/docs/configuration/launchers.html) |
| `cleanup` | [Cleanup hook](/docs/configuration/cleanup.html) |
| `checkout-setup` | [Checkout setup](/docs/configuration/checkout-setup.html) |
| `runtime-updates` | [Runtime updates](/docs/configuration/runtime-updates.html) |
| `runtime-args` | [Per-CLI argument defaults](/docs/configuration/permissions.html#arguments-for-alternatives) |

## Using a different configuration file

Every command reads `ub-agents.yaml` by default. Pass `--config` to use another file.

```sh
ub-agents --config staging.yaml launch
```

Relative paths and `ub-agents.local.yaml` resolve from that file's directory.

## Machine-local overrides

To change settings on one machine only, put them in `ub-agents.local.yaml` next to `ub-agents.yaml`, which `init` adds to `.gitignore`. Give only the keys you change. A runtime list replaces the shared one, so repeat the shared runtime to keep it:

```yaml
# ub-agents.local.yaml
agents:
  implementer:
    runtime: ["codex:gpt-6.1-sol:high", "claude:claude-opus-5-5:high"]
```

An agent that only exists in the local file needs a full definition. `launch` picks up changes before its next run.
