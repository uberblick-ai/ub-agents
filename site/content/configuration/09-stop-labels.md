# Stop labels

A stop label pauses all work on an issue or pull request until a person removes it. Defaults to `needs-human`.

```yaml
stop-labels: [needs-human]
```

## Asking a person

Agents park an item by reporting an outcome that adds a stop label. The comment lists each decision separately.

```yaml
outcomes:
  needs-human: {add: [needs-human]}
```

## Approvals

With [approvals](/docs/configuration/approvals.html) on, ub-agents also adds the stop label when an item needs a maintainer, with a comment saying what to do.

## Resuming

Remove the stop label, then set the workflow label you want, or run `ub-agents retry`.

```sh
ub-agents retry 214 --reason "Chose local storage"
```

## Rules

An outcome may add a stop label but never remove one. `ub-agents check` rejects it.
