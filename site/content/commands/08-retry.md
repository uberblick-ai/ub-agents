# ub-agents retry

Run `ub-agents retry` after fixing why an item stopped. It resets the failure count
with your reason. After recording the reset on an open item, it removes every
configured stop label present and adds the selected agent's first trigger label
if none of that agent's triggers is already present. Other labels stay as they are.

It also resets the selected agent's `max-rounds` outcome counts on that item to
zero. For the starter PR reviewer, use
`ub-agents retry N --agent reviewer --reason "Findings resolved"` for a fresh set
of three rounds, with `needs-human` removed and `needs-review` restored automatically.

To allow just one more review without resetting the count, remove `needs-human`
and reapply `needs-review` directly. Another `changes-requested` returns the PR
to `needs-human`; an approval can still advance it. Counts come from GitHub and
survive restarts, recovery and different launchers. A new PR starts at zero.

Specify `--agent NAME` to reset a particular agent. Without it, retry selects the
first configured agent whose kind matches the item (or `either`), independently
of trigger labels and eligibility; no match fails without a record. The result
links the reset, names the labels removed and added (or says they were already
in place), and says a running launcher picks it up on its next poll. Use status
for remaining pickup gates.

Closed items get a reset record without label changes; retry says the item is
closed. An owned assignment or unknown agent is refused without a record or
label changes. If a label change fails, the reset stays recorded, retry exits
nonzero and names the labels still to remove or add by hand.

```text
{{help retry}}
```
