# Public repositories

Anyone can comment on a public repository. Keep [approvals](/docs/configuration/approvals.html#on) on, so outside text reaches an agent only after a maintainer reads it.

```yaml
approvals: on
```

## Start work on purpose

A maintainer adds the trigger label. Outside edits to the issue [park it](/docs/configuration/stop-labels.html#resuming) until a maintainer re-applies a trigger label and removes the stop label.

## Fork pull requests

Agents can review them but never push to them ([outside pull requests](/docs/configuration/approvals.html#outside-pull-requests)). Every new fork commit needs a maintainer's approving review of that exact commit.
