# Keep issues small

One issue, one change, one pull request a person can review in a few minutes. Small issues fail less, review faster and are cheap to redo.

## Split before you start

If an issue needs more than one pull request, split it into several issues. Use GitHub's "blocked by" links for the order; with [`dependencies: wait`](/docs/configuration/queue.html#dependencies), later issues wait for earlier ones.

```yaml
queue:
  dependencies: wait
```

## Let the preparer ask

The issue preparer turns a rough idea into clear requirements. Tell it, in its [instructions](/docs/configuration/instructions.html#role-files), to stop and ask when an issue is too big or unclear rather than guess.

## Ship in milestones

Group related issues in a milestone and [use milestone `gate`](/docs/configuration/queue.html#milestones). Earlier runnable milestone work goes first, with a strict-priority exception for unmilestoned issues. Independent later work can start when earlier work is unavailable to this launcher.

```yaml
queue:
  milestones: gate
```

The three milestone modes are `gate`, `order` and `ignore`. Use `order` if higher priority should always win, including in later milestones, or `ignore` to skip milestone selection. Projects using `gate` now start later work while earlier issues are ineligible; to keep work in milestone order, give every open issue in the active milestone a trigger label, or move it out.
