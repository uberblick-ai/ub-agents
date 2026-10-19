# Checkout setup

Reinstall the control checkout's dependencies when the launcher pulls a lockfile or tool configuration change, and install dependencies in new private worktrees.

```yaml
checkout-setup:
  command: [sh, -c, 'mise trust ./mise.toml && exec mise run install']
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

This optional project hook runs in the Python launcher, before the agent starts.
Without `checkout-setup`, no setup command runs. ub-agents does not require mise:
choose the command for your project, such as `[npm, ci]` for an npm project.

The mise example trusts only this checkout's `mise.toml` before installing.
Every new worktree has a new path, so trusting the control checkout alone is not
enough. Agent runtime command permissions do not apply to this hook. The explicit
`sh -c` runs the two steps in order; if trust fails, install does not run.

The command is a nonempty argv list, executed directly in the checkout being set up; name a shell explicitly when needed. `when-changed` is a nonempty list of literal repository-relative file paths, including files added or deleted by a pull. Absolute paths, directories and `..` components are rejected. The timeout defaults to 600 seconds and must be positive and at most 3600.

In the control checkout, before every new run, after refresh and configuration reload, the launcher compares watched files with the last commit where setup succeeded. Until setup first succeeds, it compares with the HEAD before the fast-forward. Any difference runs the command once before claiming a role. A commit can add the setting and lockfile change together. Unrelated changes and refreshes with nothing to pull skip setup, unless a previous setup failed. Pulls made by hand are outside this mechanism.

The first baseline is saved before the fast-forward. If launch stops or configuration reload fails before setup runs, the next launch still detects the watched-file change.

The terminal view and plain output name the triggering file. Command output goes only to the named log; records and logs stay outside the checkout in the user's state directory. Keep tracked files unchanged and install dependencies into ignored paths.

A nonzero exit, start failure, timeout or interruption in control-checkout setup stops launch without claiming a role, spending an attempt or marking the item blocked or retrying. Read the named log, fix the install in the control checkout, and launch again. Failed setup retries even with nothing to pull, until it succeeds.

For an agent with `worktree: true`, the launcher also runs the command once in each private worktree immediately after creating it, before the agent starts. `when-changed` applies only to the control checkout; a new worktree always needs its own dependencies. Shared-checkout agents run no additional setup command. Private setup uses the same environment and timeout as control-checkout setup.

The terminal view and plain output say setup is running in the worktree. Its output goes to `checkout-setup/process.log` in the run's log directory, separately from the agent's `process.log`.

For a PR reviewer, setup installs the PR head's lockfile, so its install scripts run before the agent does; the agent would run them anyway to test the PR.

A private-worktree setup failure counts toward the item's retry limit. Fix the hook or environment before using `ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"` to reset exhausted attempts.

A failure ends the run before the agent starts, cleans up the private worktree, and reports the cause and log as a workspace-preparation failure. Interruption terminates setup like an interrupted agent. Timeout and interruption terminate and confirm the whole process group; crash recovery requires confirmed termination before releasing the item or removing the worktree. The item's stop labels, open state, trigger labels and candidate head are checked again after setup finishes.
