# Outcomes

When an agent finishes, it reports a named outcome. ub-agents checks the result on GitHub, removes the agent's trigger labels and applies the labels you declared.

```yaml
outcomes:
  approved: {add: [ready-to-merge]}
  changes-requested: {add: [needs-changes], max-rounds: 3}
```

## Limit rounds

`max-rounds` counts an agent's accepted records of that outcome on the same item
since its latest `ub-agents retry` reset. With the example above, the first three
`changes-requested` reports add `needs-changes`. The fourth removes the triggers
but adds the first configured stop label (`needs-human` by default) instead. An
**Action needed** notice names the limit, carries the agent's summary and explains
both ways to resume. Approval still adds `ready-to-merge` at any round.

The count lives on GitHub and survives restarts, different launchers and recovery,
which counts each transition once. Other agents' outcomes, including integrator
`changes-requested` and conflict routing, do not count. Each new PR starts at zero.
Accepted successes reset consecutive failures, without resetting rounds.

To allow one more review, remove `needs-human` and reapply `needs-review`; the count
stays. Another `changes-requested` parks the PR again. To start a fresh set of
rounds, run `ub-agents retry N --agent reviewer --reason TEXT`, then remove the stop
label and reapply the trigger. Retry never restores labels.

Assignment context includes `outcome_rounds`, keyed by outcome with `used` and
`limit`. The starter limits only the PR reviewer's `changes-requested` to three.
It marks findings P1 (must not merge as is) or P2. On the final review
(`used >= limit`), it approves when only P2 findings remain, listing them in the
review. Open P1 findings require `changes-requested`, named in the summary.

The key works on any agent's outcome. It must be a positive integer, needs at least
one configured stop label, and cannot be used on an outcome that already adds a
stop label. `doctor --offline` rejects invalid values. Omit it for no limit.

**Upgrading:** Older launchers reject `max-rounds`. Upgrade every project launcher
before using it.

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
