# Checkout setup

Set up dependencies in the control checkout before each new run and in new private worktrees.

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  timeout-seconds: 600
```

Use an argument list such as `[npm, ci]`, and name a shell explicitly when you need one. Every worktree needs its own `mise trust`, as in the example.

`when-changed` is optional. Without it, the command runs before every new run, after the control-checkout refresh, even if HEAD did not move. It must be idempotent and fast when nothing changed.

For slower installs, list the files that should trigger setup:

```yaml
checkout-setup:
  command: [npm, ci]
  when-changed: [package-lock.json, package.json]
```

With `when-changed`, setup runs in the control checkout before claiming work when a listed file differs from the last successful setup. The list must be non-empty and contain repository-relative file paths; an empty list or `null` is a configuration error.

Every new private worktree runs setup regardless of `when-changed`, except for [fork pull requests](/docs/configuration/approvals.html#outside-pull-requests). The timeout defaults to 600 seconds, up to 3600. Install into ignored paths and leave tracked files unchanged.

If control-checkout setup fails, launch stops before a claim and charges no attempt. Read the log, fix the install and launch again. A failure in a private worktree counts toward the item's retry limit; its log is `checkout-setup/process.log` in the run's log directory. Fix the cause, then reset the attempts:

```sh
ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"
```

See the [checkout setup contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#checkout-setup) for comparison baselines, pending setup and worktree failures.

## Key reference

`checkout-setup` is an optional mapping; without it, no setup command runs.

| Key | Type and default | Rules |
|---|---|---|
| `command` | Required argv list | Runs without a shell in the checkout being set up, before the agent starts. Runtime permissions do not apply. |
| `when-changed` | Optional non-empty list | Literal repository-relative file paths. Absolute paths, directories, `..`, an empty list and `null` are rejected. Omit it to run after every refresh. |
| `timeout-seconds` | Number, `600` | Positive, at most 3600. |
