# Checkout setup

Reinstall the control checkout's dependencies when the launcher pulls a lockfile or tool configuration change, and install dependencies in new private worktrees.

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

Use an argument list such as `[npm, ci]`, and name a shell explicitly when you need one. Every worktree needs its own `mise trust`, as in the example.

When launch pulls a change to a `when-changed` file, it runs setup in the control checkout before claiming work. Every new private worktree runs it too, except for [fork pull requests](/docs/configuration/approvals.html#outside-pull-requests). The timeout defaults to 600 seconds, up to 3600. Install into ignored paths and leave tracked files unchanged.

If control-checkout setup fails, read the log, fix the install and launch again. A failure in a private worktree counts toward the item's retry limit; its log is `checkout-setup/process.log` in the run's log directory. Fix the cause, then reset the attempts:

```sh
ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"
```

See the [checkout-setup reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#checkout-setup) for the details.
