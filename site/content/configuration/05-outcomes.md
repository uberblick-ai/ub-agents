# Outcomes

When an agent finishes, it reports a named outcome. ub-agents checks the result on GitHub, removes the agent's trigger labels and applies the labels you declared.

```yaml
outcomes:
  approved: {add: [ready-to-merge]}
  changes-requested: {add: [needs-changes]}
```

## Add labels

Labels to add. When the agent hands off a pull request, they go on that pull request.

```yaml
handed-off: {add: [needs-review]}
```

## Remove labels

Extra labels to take off. The agent's own trigger labels are always removed.

```yaml
handed-off: {add: [needs-review], remove: [in-progress]}
```

## End of the workflow

An empty mapping changes nothing else.

```yaml
merged: {}
```

## Ask a person

Add a stop label to park the item for a human decision.

```yaml
maintainer-merge: {add: [needs-human]}
```

## Reporting

The agent reports with one command. Your instructions say which outcome to use when.

```sh
ub-agents report --outcome approved --summary "Checks pass"
ub-agents report --outcome handed-off --summary "Ready for review" --handoff 219
```

## Stop reports

A report that parks the item needs one `--action` per decision: who acts, the choices and a recommendation, in one sentence.

```sh
ub-agents report --outcome needs-human --summary "Storage policy unclear" \
  --action "Owner: choose local or cloud storage; recommend local."
```

## Retry and blocked

`--status retry` and `--status blocked` change no labels. Retry runs again after a backoff. Blocked parks the item and also needs `--action`.

```sh
ub-agents report --status retry --summary "GitHub returned 502"
```
