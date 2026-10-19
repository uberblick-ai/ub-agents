# Outcomes

When an agent finishes, it reports a named outcome. ub-agents checks the result on GitHub, removes the agent's trigger labels and applies the labels you declared.

```yaml
outcomes:
  approved: {add: [ready-to-merge]}
  changes-requested: {add: [needs-changes], max-rounds: 3}
```

## Limit rounds

`max-rounds` caps how often an agent can report an outcome on the same item. With the example above, the first three `changes-requested` reports add `needs-changes`. The fourth adds `needs-human` instead, with an **Action needed** comment. Approval still adds `ready-to-merge` at any round.

{{flow limit-rounds}}

To allow one more round without resetting the count, remove `needs-human` and
re-add `needs-review` by hand. To start over, run
`ub-agents retry N --agent reviewer --reason TEXT`: it records the reset, removes
configured stop labels and restores `needs-review` if no reviewer trigger is
present on the open item. The count lives on GitHub, so restarts and other
launchers don't reset it, and each new pull request starts at zero.

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
For a `max-rounds` stop, the launcher supplies a review request if no ask is given,
plus both resume paths. Keep every open P1 finding in the report's summary.

```sh
ub-agents report --outcome needs-human --summary "Storage policy unclear" \
  --action "Owner: choose local or cloud storage; recommend local."
```

## Retry and blocked

`--status retry` and `--status blocked` change no labels. Retry runs again after a backoff. Blocked parks the item and also needs `--action`.

```sh
ub-agents report --status retry --summary "GitHub returned 502"
```
