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

To allow one more round, swap `needs-human` for `needs-review` by hand. To start the count over, run `ub-agents retry N --agent reviewer --reason TEXT`. The count lives on GitHub, so restarts and other launchers don't reset it; each new pull request starts at zero.

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

## Ask the launcher to merge

A `pr` agent can report its merge decision and exit. The launcher waits for checks and performs the merge:

```yaml
integrator:
  kind: pr
  # Other required agent settings omitted.
  on-conflict: changes-requested
  outcomes:
    merged: {merge: squash}
    changes-requested: {add: [needs-changes]}
```

`merge` accepts `squash`, `merge` or `rebase`. The outcome cannot add a stop label, declare `sub-issues`, or be the agent's `on-conflict` outcome. The agent keeps policy, review, changelog and base-merge decisions, then reports instead of running `gh pr merge`.

The launcher checks the reported head against the assigned candidate or its base merge, rereads stop labels, and waits for every check run and commit status on that exact head. Skipped and neutral checks pass. Pending or absent checks wait without a model until the run's agent-timeout deadline, with the lease renewing. After checking mergeability, it merges with a head match and accepts only a confirmed merge, recording both the head and merge commit. Failed checks and conflicts use `on-conflict`; other refusals block. Closing issues happens afterward; a failed close does not undo acceptance.

Recovery only confirms an existing merge; it never merges. Outcomes without `merge` keep their current behavior. See the [merge gates and recovery](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#launcher-merges).

**Upgrading:** every launcher of a project must run a release supporting `merge` before its `ub-agents.yaml` uses the key.

## Start sub-issues

An agent that takes issues can start the native sub-issues its run creates:

```yaml
split: {sub-issues: {add: [needs-preparation]}}
```

Agents file sub-issues without trigger labels, then report `split`. The launcher adds the declared labels, which must be issue-agent triggers, and removes the parent's triggers. With [approvals on](/docs/configuration/approvals.html), sub-issues inherit the parent's start. See the [outcomes reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#outcomes-and-transitions).

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

A report that parks the item needs at least one ask: `--action` for each thing a person must do, or `--option` per alternative for a choice, recommendation first. Each ask is one sentence that names who acts. A `max-rounds` stop gets a review request from the launcher if no ask is given.

```sh
ub-agents report --outcome needs-human --summary "Storage policy unclear" \
  --option "Owner: keep storage local." --option "Owner: move storage to the cloud."
```

## Retry and blocked

`--status retry` and `--status blocked` change no labels. Retry runs again after a backoff. Blocked parks the item and also needs `--action` or `--option`.

```sh
ub-agents report --status retry --summary "GitHub returned 502"
```
