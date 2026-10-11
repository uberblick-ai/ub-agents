# ub-agents retry

Run `ub-agents retry` after fixing why an item stopped. It resets the failure count with your reason. Labels stay as they are.

Specify `--agent NAME` to reset a particular agent. Without it, retry selects the
first configured agent whose kind matches the item (or `either`), independently
of trigger labels and eligibility; no match fails without a record. The result
links the reset and names the next step. Use status for remaining pickup gates.

```text
{{help retry}}
```
