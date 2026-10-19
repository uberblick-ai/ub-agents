# Checkout setup

Reinstall the control checkout's dependencies when the launcher pulls a lockfile or tool configuration change, and install dependencies in new private worktrees.

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

Choose an argument list for your project, such as `[npm, ci]`. Name a shell explicitly when needed. The mise example trusts each checkout's `mise.toml` before installing; every private worktree needs its own trust.

`when-changed` lists repository-relative files to watch in the control checkout. When launch pulls a change to one, it runs setup before claiming work. Each new private worktree runs setup regardless of watched files, except fork PR worktrees, where setup never runs. The timeout defaults to 600 seconds, up to 3600.

Fork PR worktrees contain the base branch's files and guidance. Agents review the
head as a diff and use repository CI on the exact head SHA for check evidence;
they must never check out or execute the head. See
[outside pull requests](/docs/configuration/approvals.html#outside-pull-requests).

Keep tracked files unchanged and install dependencies into ignored paths. The terminal view and plain output name the triggering file and setup log. Private-worktree output goes to `checkout-setup/process.log` in the run's log directory.

If control-checkout setup fails, read the log, fix the install and launch again. A private-worktree failure counts toward the item's retry limit; fix the cause before resetting exhausted attempts:

```sh
ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"
```

See the [checkout-setup reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#checkout-setup) for file comparison, path validation and process recovery, and [operations](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#when-things-go-wrong) for troubleshooting.
