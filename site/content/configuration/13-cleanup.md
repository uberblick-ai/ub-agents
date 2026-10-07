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

Normal runs need nothing more. [`ub-agents cleanup`](/docs/commands/cleanup.html) previews and removes what crashed runs left on this machine, plus retained branches.

```sh
ub-agents cleanup
ub-agents cleanup --apply
```
