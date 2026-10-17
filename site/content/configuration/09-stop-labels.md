# Stop labels

A stop label pauses all work on an issue or pull request until a person removes it. You list them in `ub-agents.yaml`; the default is `needs-human`. Agents may set other labels, but never workflow labels (triggers, outcome labels and stop labels): the launcher applies those from the reported outcome.

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

## Resuming

Answer the asks on GitHub, remove the stop label and re-add the trigger label you
want. This manual label swap allows one more round without resetting the count;
another limited outcome parks the item again. A blocked run adds no label; run
`ub-agents retry` once the blocker is fixed, or push a new commit to the pull request.

```sh
ub-agents retry 214 --reason "Chose local storage"
```

Retry records the reset before removing all configured stop labels on an open
item. It adds the selected agent's first trigger only if none of that agent's
triggers is present, then reports the changes and pickup on a running launcher's
next poll. Closed items keep their labels. If a label change fails, retry exits
nonzero and names the remaining manual changes; the reset record stays.

With [approvals](/docs/configuration/approvals.html) on, ub-agents also adds the stop label when an item needs a maintainer, with a comment saying what to do.

The [`ub-agents-unblock`](https://skills.uberblick.ai/skills/ub-agents-unblock) skill walks your own agent through every item waiting for a person, one at a time.

## Rules

An outcome may add a stop label but never remove one. `ub-agents doctor --offline` rejects it.
