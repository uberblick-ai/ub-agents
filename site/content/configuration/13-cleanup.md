# Cleanup hook

After every run, the launcher removes the run's private worktree. The optional cleanup hook runs just before that, to stop things a run started, such as test services.

## Branches on GitHub

- The launcher cleans up only on the machine; it never deletes branches on GitHub.
- In the repository's **Settings**, under **General**, **Pull Requests**, turn on **Automatically delete head branches** so a merged pull request's branch is deleted with the merge. See GitHub's guide to [managing the automatic deletion of branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches).
- Branches of pull requests closed without merging stay; delete them from the pull request page.

## Hook configuration

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

At startup, [`launch`](/docs/commands/launch.html) clears leftover worktrees, running the hook before removing each, and removes run logs older than 7 days. Decisions go to `.ub-agents/sessions/*.cleanup.log`. See the [cleanup rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#stale-artifact-cleanup).
