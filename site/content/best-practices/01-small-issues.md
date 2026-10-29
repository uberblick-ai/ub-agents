# Keep issues small

One issue, one change, one pull request a person can review in a few minutes. Small issues fail less, review faster and are cheap to redo.

## Split before you start

If an issue needs more than one pull request, split it into several issues. Use GitHub's "blocked by" links for the order; with [`dependencies: wait`](/docs/configuration/queue.html#dependencies), later issues wait for earlier ones.

```yaml
queue:
  dependencies: wait
```

## Let the preparer ask

The starter issue-preparer turns a rough idea into clear requirements, [splits](/docs/how-the-loop-works.html#complex-issues) work that needs several PRs into sub-issues, and asks with `needs-human` when a decision is open. Customize its [instructions](/docs/configuration/instructions.html#role-files) for your project.

## Ship in milestones

Group related issues in a milestone and use [milestone `gate`](/docs/configuration/queue.html#milestones), so each milestone finishes before the next one starts.

```yaml
queue:
  milestones: gate
```

Use `order` instead if higher priority should always win.
When each milestone ends with a release, use `strict-gate` to wait until a person publishes the release and closes the milestone.
