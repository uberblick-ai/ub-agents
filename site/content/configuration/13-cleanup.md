# Cleanup hook

After every run, the launcher removes the run's private worktree by itself; local branches stay. The cleanup hook is an optional program that runs just before that removal. Use it to stop things a run started, such as test services.

```yaml
cleanup:
  command: [./scripts/cleanup-agent-worktree]
  timeout-seconds: 60
```

## Command

An argument list, run without a shell from the launcher's checkout.

```yaml
command: [./scripts/cleanup-agent-worktree]
```

## Timeout

Seconds before the hook is stopped. Defaults to 60, up to 3600.

```yaml
timeout-seconds: 60
```

## What it gets

The worktree and branch, plus a JSON file with the run's details.

```text
UB_AGENTS_WORKTREE
UB_AGENTS_BRANCH
UB_AGENTS_CLEANUP_CONTEXT
```

## Result

Exit zero to let the worktree be removed. Any other exit keeps the worktree and branch for a later retry; the run's outcome stands. Make the hook safe to run twice.

## Leftovers

Every [`ub-agents launch`](/docs/commands/launch.html) invocation sweeps leftovers
once after startup checks pass and before the first poll or claim, including
`--agent`, `NUMBER` and `--once`. It removes eligible worktrees and retained local
branches whose trusted run lease records this machine. Live leases, dirty or
locked trees, other-host leases and uncertain artifacts are kept. Each artifact
is rechecked; the configured cleanup hook runs before each worktree removal.

Each `removed` or `kept` line includes its reason in `.ub-agents/launch.log` and the
terminal view's Startup cleanup fold, or plain output with `--no-ui`. A sweep
failure is logged without stopping launch, leaving artifacts not yet reached in
place.

A crashed run's lease stays live until expiry, so a quick restart leaves its
worktree until a later launch start. There is no periodic sweep.
