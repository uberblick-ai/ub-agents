# Stop labels

A stop label pauses all work on an issue or pull request until a person removes it. The default is `needs-human`. Agents never set workflow labels themselves; the launcher applies them from the reported outcome.

```yaml
stop-labels: [needs-human]
```

{{flow stop-labels}}

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
  --option "Owner: keep storage local." --option "Owner: move storage to the cloud."
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

## Resuming

Answer the asks on GitHub, remove the stop label and add the trigger label you want. A blocked run adds no label; once the blocker is fixed, run [`ub-agents retry`](/docs/commands/retry.html) or push a new commit to the pull request.

```sh
ub-agents retry 214 --reason "Chose local storage"
```

With [approvals](/docs/configuration/approvals.html) on, ub-agents also adds the stop label when an item needs a maintainer, with a comment saying what to do.

The [`ub-agents-unblock`](https://skills.uberblick.ai/skills/ub-agents-unblock) skill walks your own agent through every item waiting for a person.

## Rules

An outcome may add a stop label but never remove one. `ub-agents doctor --offline` rejects it.
