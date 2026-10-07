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

Group related issues in a milestone and [order the queue by it](/docs/configuration/queue.html#milestones). Earlier milestones run first, and nothing waits on a milestone that cannot move.

```yaml
queue:
  milestones: order
```
