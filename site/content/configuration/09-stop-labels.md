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
