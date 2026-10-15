# Cleanup hook

After every run, the launcher removes the run's private worktree by itself. The cleanup hook is an optional program that runs just before that removal. Use it to stop things a run started, such as test services.

After removing the worktree, the launcher fetches origin branches and deletes
the run's local branch if its tip is reachable from one of them. Runs with no
commits and runs that pushed their commits qualify. Deletion compares the tip,
so a branch that moved is kept. Unpublished tips and fetch, reachability or
deletion failures keep the branch and log its name, reason and full tip SHA;
the outcome, cleanup status and lease stay unchanged. A kept worktree also keeps
its branch. PR worktrees are detached and have no local branch to delete.

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
`--agent`, `NUMBER` and `--once`.
The terminal view opens before those setup checks, showing `checking setup` or
`waiting` during retryable GitHub failures. No sweep starts until setup passes.
For every role, the sweep covers registered worktrees and unregistered entries
under `.ub-agents/worktrees/`, plus local
`ub-agents/<agent>/<number>/<run>` and older `ub-agent/` branches. Symlinks are
removed as links without following them.

It keeps artifacts of live leases on this host, branches heading open PRs or
checked out in remaining worktrees, and artifacts whose processes may still be
running. That includes unconfirmed cleanup in leases or local diagnostics,
unfinished hook or setup groups not confirmed stopped, and expired local leases
whose recorded groups are present or cannot be checked. Missing, unreadable,
older-version or other-host leases, ended runs without a PR, dirty trees and
locked trees do not prevent removal. An ended run's worktree is removed even when
its branch heads an open PR. GitHub coordination scan or PR lookup
failures keep affected artifacts and log the reason. Each artifact is rechecked;
the cleanup hook runs before each worktree removal with unknown context fields
set to `null`. A failed hook preserves its worktree and branch for retry.

Each `removed` or `kept` line includes its reason in `.ub-agents/launch.log` and the
terminal view's Startup cleanup fold, or plain output with `--no-ui`. A sweep
failure is logged without stopping launch. Removed branches include `unpushed tip SHA`
if their tips are not reachable from fetched origin branches or merged PR
heads, or reachability cannot be checked. Restore one with `git branch <name> <sha>`.

A crashed run's local lease stays live until expiry, so a quick restart leaves its
worktree until a later launch start. There is no periodic sweep.

## Diagnostic retention

The same startup sweep prunes `.ub-agents/runs/<run>/` when its newest file is
more than 7 days old. Live local leases, unconfirmed cleanup, unfinished setup
or hook processes, expired process groups not confirmed gone, unreadable or
redirected diagnostics, and any worktree or branch the sweep kept protect the
run's logs. Failed coordination or safety checks preserve affected logs.
Symlinked entries are removed only as links, without following their targets.

Ended or stale session snapshots older than 7 days are pruned with their cleanup
logs. Orphan `.tmp` and `.cleanup.log` files older than 7 days are pruned when
no live snapshot protects them. Live sessions and the current launcher's files
survive; the existing cap of 20 inactive sessions still applies. Retention is
fixed, with no configuration key.

When anything is pruned, one line in launch output, `launch.log` and the Startup
cleanup fold reports counts and total file bytes freed, including with `--no-ui`:

```text
pruned 63 run directories and 4 session files older than 7 days (41 MB)
```

Session counts include snapshots, companion logs and temporary files. Nothing
is printed for pruning when nothing was removed. A removal failure is logged
once with its reason, and the sweep and launch continue.
