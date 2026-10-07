# ub-agents cleanup

Run `ub-agents cleanup` to preview worktrees and local branches left by stopped runs on this machine, and `--apply` to remove the ones that are safe to remove. After a normal run the launcher already removes its worktree, running the [cleanup hook](/docs/configuration/cleanup.html) first, so this command is for leftovers.

```text
{{help cleanup}}
```
