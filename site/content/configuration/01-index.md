# Configuration

`ub-agents.yaml` sits at the root of your repository. `ub-agents init` writes a starter file and `ub-agents doctor --offline` validates the effective configuration, including optional machine-local overrides. Unknown keys are errors.

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

The selected base file remains required. Relative paths and the optional
`ub-agents.local.yaml` resolve from the base file's directory after resolving
symlinks. With `--config PATH`, a local file in an unrelated working directory
is never used.

## Machine-local overrides

Put selected machine settings in an untracked `ub-agents.local.yaml` beside the
base file. Local mappings merge recursively by key, including agents and their
settings. Omitted keys and other agents inherit; supplied scalars and lists replace
the entire base value. Lists are never concatenated, and there is no additive or
deletion syntax. Explicit `null` goes through normal schema validation rather than
meaning inherit or delete. A local-only agent must have a complete definition.

If the shared implementer uses `codex:gpt-6.1-sol:high`, repeat that alternative to
keep it when adding a machine-only model:

```yaml
# ub-agents.local.yaml
agents:
  implementer:
    runtime: ["codex:gpt-6.1-sol:high", "codex:machine-model:high"]
```

This replaces the whole runtime value in order, while inheriting the implementer's
other settings. See [Runtimes](/docs/configuration/runtimes.html#alternatives).

Missing, empty and comment-only local files leave behavior unchanged. Both layers
use strict YAML loading with unique keys and preserved `on`/`off` policy spellings;
the effective settings use the existing schema. Invalid YAML, unreadable files,
non-mapping top levels, unknown keys and invalid local values fail with an error
naming the local file.

`doctor`, `init` validation and launch use the same effective settings.
Launch rereads both files before each run: adding, editing or removing the local
file takes effect at the next reload, and removing it restores base settings.
Active runs keep their selected settings. The local file stays in the operator's
control checkout and is not copied into private agent worktrees.

`init` adds `ub-agents.local.yaml` alongside `.ub-agents/` in `.gitignore` when
absent, preserving existing entries. It generates no machine-specific values.
Existing projects should add `ub-agents.local.yaml` to `.gitignore` before creating
the file and commit only that ignore change.
