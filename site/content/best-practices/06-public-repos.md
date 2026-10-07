# Public repositories

Anyone can comment on a public repository. Keep approvals on, so outside text reaches an agent only after a maintainer reads it.

```yaml
approvals: on
```

## Start work on purpose

A maintainer adds the trigger label. Outside edits to the issue park it until a maintainer starts it again or approves the new text.

## Approve what changed

`approve` prints the current input and records your approval of exactly that.

```sh
ub-agents approve 214
```

## Fork pull requests

Agents can review them but never push to them. Every new fork commit needs a maintainer's approval.
