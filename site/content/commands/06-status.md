# ub-agents status

Run `ub-agents status` to see matching work, claims, failures and reported outcomes. Give a number to see why one item is waiting. It writes no GitHub changes.

Status runs configured [agent health checks](/docs/configuration/agents.html#health-check) for otherwise-ready work and shows failing checks as `waiting`, even without a running launcher.

```text
{{help status}}
```
