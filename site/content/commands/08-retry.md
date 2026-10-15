# ub-agents retry

Run `ub-agents retry` after fixing why an item stopped. It resets the failure count with your reason. Labels stay as they are.

It also resets the selected agent's `max-rounds` outcome counts on that item to
zero. For the starter PR reviewer, use
`ub-agents retry N --agent reviewer --reason "Findings resolved"` for a fresh set
of three rounds, then remove `needs-human` and reapply `needs-review`.

To allow just one more review without resetting the count, remove `needs-human`
and reapply `needs-review` directly. Another `changes-requested` returns the PR
to `needs-human`; an approval can still advance it. Counts come from GitHub and
survive restarts, recovery and different launchers. A new PR starts at zero.

Specify `--agent NAME` to reset a particular agent. Without it, retry selects the
first configured agent whose kind matches the item (or `either`), independently
of trigger labels and eligibility; no match fails without a record. The result
links the reset and names the next step. Use status for remaining pickup gates.

```text
{{help retry}}
```
