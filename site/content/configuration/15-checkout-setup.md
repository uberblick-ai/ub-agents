# Checkout setup

Reinstall the control checkout's dependencies when the launcher pulls a lockfile or tool configuration change.

```yaml
checkout-setup:
  command: [mise, run, install]
  when-changed: [pnpm-lock.yaml, mise.toml]
  timeout-seconds: 600
```

The command is a nonempty argv list, run without a shell in the control checkout. `when-changed` is a nonempty list of literal repository-relative file paths, including files added or deleted by a pull. Absolute paths, directories and `..` components are rejected. The timeout defaults to 600 seconds and must be positive and at most 3600.

Before every new run, after refresh and configuration reload, the launcher compares watched files with the last commit where setup succeeded. Until setup first succeeds, it compares with the HEAD before the fast-forward. Any difference runs the command once before claiming a role. A commit can add the setting and lockfile change together. Unrelated changes and refreshes with nothing to pull skip setup, unless a previous setup failed. Pulls made by hand are outside this mechanism.

The first baseline is saved before the fast-forward. If launch stops or configuration reload fails before setup runs, the next launch still detects the watched-file change.

The terminal view and plain output name the triggering file. Command output goes only to the named log; records and logs stay outside the checkout in the user's state directory. Keep tracked files unchanged and install dependencies into ignored paths.

A nonzero exit, start failure, timeout or interruption stops launch without claiming a role, spending an attempt or marking the item blocked or retrying. Read the named log, fix the install in the control checkout, and launch again. Failed setup retries even with nothing to pull, until it succeeds.

**Upgrading:** Upgrade every launcher before adding `checkout-setup` to the project's configuration. Older versions reject the unknown key.
