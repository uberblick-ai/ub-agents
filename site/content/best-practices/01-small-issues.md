# Keep issues small

One issue, one change, one pull request a person can review in a few minutes. Small issues fail less, review faster and are cheap to redo.

## Split before you start

If an issue needs more than one pull request, split it into several issues. Use GitHub's "blocked by" links for the order; with [`dependencies: wait`](/docs/configuration/queue.html#dependencies), later issues wait for earlier ones.

```yaml
queue:
  dependencies: wait
```

## Let the preparer ask

The starter issue-preparer turns a rough idea into clear requirements. It splits work that needs multiple PRs into prepared native sub-issues, leaves the parent open and blocked by each, and reports `split`. The sub-issues have no workflow labels until a maintainer labels each one to start it. For unresolved decisions it reports `needs-human`. Customize its [instructions](/docs/configuration/instructions.html#role-files) for your project.

## Ship in milestones

Group related issues in a milestone and [use milestone `gate`](/docs/configuration/queue.html#milestones). Earlier runnable milestone work goes first, with a strict-priority exception for unmilestoned issues. Independent later work can start when earlier work is unavailable to this launcher.

```yaml
queue:
  milestones: gate
```

The three milestone modes are `gate`, `order` and `ignore`. Use `order` if higher priority should always win, including in later milestones, or `ignore` to skip milestone selection. See the [queue reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#queue) for milestone rules.
