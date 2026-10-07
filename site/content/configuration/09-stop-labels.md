# Stop labels

A stop label pauses all work on an issue or pull request until a person removes it. You list them in `ub-agents.yaml`; agents never set labels themselves. Defaults to `needs-human`.

```yaml
stop-labels: [needs-human]
```

## How an agent stops

Give the role an [outcome](/docs/configuration/outcomes.html) that adds the stop label, and say in its instructions when to use it.

```yaml
implementer:
  outcomes:
    handed-off: {add: [needs-review]}
    needs-human: {add: [needs-human]}
```

The agent reports that outcome with at least one ask. The launcher adds the label and posts an **Action needed** comment that lists each ask.

```sh
ub-agents report --outcome needs-human --summary "Storage policy unclear" \
  --action "Owner: choose local or cloud storage; recommend local."
```

## Say when a person decides

The agent stops only where its instructions tell it to. Name the decisions that belong to a person, and who answers them, in the [shared policy](/docs/configuration/instructions.html#shared-policy):

```markdown
## Human decisions

@acme/maintainers answers scope and policy questions. Stop and ask before you:
- change a public API, a database schema or a dependency
- delete data or change permissions
- choose between designs the issue leaves open
```

Then say in each role file what that means for the role:

```markdown
If the issue does not name the storage backend, or the change needs a
migration, report needs-human with one --action per decision.
```

## Blocked runs

`--status blocked` also needs an ask but adds no label. The item waits for `ub-agents retry`, or for a new commit on a pull request.

## Approvals

With [approvals](/docs/configuration/approvals.html) on, ub-agents also adds the stop label when an item needs a maintainer, with a comment saying what to do.

## Resuming

Remove the stop label, then set the workflow label you want, or run `ub-agents retry`.

```sh
ub-agents retry 214 --reason "Chose local storage"
```

## Rules

An outcome may add a stop label but never remove one. `ub-agents check` rejects it.
