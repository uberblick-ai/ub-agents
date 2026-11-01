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

## Start sub-issues

An agent that takes issues can start the native sub-issues its run creates:

```yaml
split: {sub-issues: {add: [needs-preparation]}}
```

Agents file sub-issues without trigger labels, then report `split`. The launcher adds the declared labels, which must be issue-agent triggers, and removes the parent's triggers. With [approvals on](/docs/configuration/approvals.html), sub-issues inherit the parent's start. See the [outcome contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#explicit-outcomes).

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

## Key reference

`outcomes` is a required, nonempty mapping from names to transitions.

| Key | Type and default | Rules |
|---|---|---|
| `add` | List, `[]` | Must not include the agent's own triggers. May add a stop label. |
| `remove` | List, `[]` | Must not include a stop label, and no trigger of the agent may be one. |
| `max-rounds` | Positive integer | Needs a configured stop label, and the outcome must not add one. |
| `sub-issues` | Mapping with only `add` | Nonempty list of `issue` or `either` agent triggers, no stop labels. Agents that take issues only. |

See [explicit outcomes](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#explicit-outcomes) for validation,
transition order, stop checks and recovery, and [assignments and attempts](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#assignments-and-attempts)
for round counting.
