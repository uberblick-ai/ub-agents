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

## Start sub-issues

An agent that takes issues can start the native sub-issues its run creates:

```yaml
split: {sub-issues: {add: [needs-preparation]}}
```

The launcher applies the labels to open sub-issues in the same repository,
authored by the account holding the run's lease and created after its claim.
Other sub-issues receive no label and are named in the outcome summary. The parent
loses its triggers. Targets are stored before the transition, so recovery finishes
the same set. Agents file issues without trigger labels and report `split` normally.

`sub-issues` accepts only `add`, with a nonempty list of issue-agent trigger labels.
Stop labels and PR-only agents are rejected by `doctor --offline`. Adding the
preparer's own trigger to its sub-issues is allowed.

With [approvals on](/docs/configuration/approvals.html), the accepted parent outcome
lets these labels inherit the parent's start. Later outside edits and comments
follow the usual approval rules. Approvals off still allow any trigger to start work.

**Upgrading:** Before using this key, stop and upgrade all project launchers, then
restart them on the new version. Older launchers reject the key and would park
inherited starts.

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
