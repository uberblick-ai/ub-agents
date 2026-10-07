# Public repositories

Anyone can comment on a public repository. Keep [approvals](/docs/configuration/approvals.html#on) on, so outside text reaches an agent only after a maintainer reads it.

```yaml
approvals: on
```

## Start work on purpose

A maintainer adds the trigger label. Outside edits to the issue [park it](/docs/configuration/stop-labels.html#approvals) until a maintainer starts it again or approves the new text.

## Approve what changed

[`approve`](/docs/commands/approve.html) prints the current input and records your approval of exactly that.

```sh
ub-agents approve 214
```

## Fork pull requests

Agents can review them but never push to them ([outside pull requests](/docs/configuration/approvals.html#outside-pull-requests)). Every new fork commit needs a maintainer's approval.
