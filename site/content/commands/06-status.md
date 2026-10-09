# ub-agents status

Run `ub-agents status` to see matching work, claims, failures and reported outcomes. Give a number to see why one item is waiting. It writes no GitHub changes.

`status N` uses the same gates as `launch N`: queue priority and milestone policy,
including the `gate` hold, do not apply to the explicit item. Every other gate
still applies, including matching trigger labels. Later-milestone and unmilestoned
targets show `ready` when they can run. Unnumbered status keeps the queue policy.

Status runs configured [agent health checks](/docs/configuration/agents.html#health-check) for otherwise-ready work and shows failing checks as `waiting`, even without a running launcher.

```text
{{help status}}
```
