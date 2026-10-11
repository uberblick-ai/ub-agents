# ub-agents status

Run `ub-agents status` to see matching work, claims, failures and reported outcomes. Give a number to see why one item is waiting. It writes no GitHub changes.

`status N` uses the same gates as `launch N`: queue priority and milestone
selection do not apply to the explicit item. Every other gate
still applies, including matching trigger labels. Later-milestone and unmilestoned
targets show `ready` when they can run. Unnumbered status keeps the queue policy.

The three milestone modes are `gate`, `order` and `ignore`. In `gate`, ready new
issues follow the earliest milestone with eligible work for this launcher,
with a strictly higher-priority exception for unmilestoned issues. Status explains
passed-over milestones, such as `Milestone #12 has no eligible issues for this launcher`;
JSON includes `milestone_skips` when nonempty.

Status runs configured [agent health checks](/docs/configuration/agents.html#health-check) for otherwise-ready work and shows failing checks as `waiting`, even without a running launcher.

Targeted status displays the item’s own priority and milestone without inheritance.
`ready` means launch can start an assignment; `recover` means it can finish a pending
completion instead of starting another agent run.

## Ownership and output

Status exits 0 when it can report, including waiting or untriggered work; unreadable
work exits 1 and invalid input exits 2. It starts no runtime maintenance and never
claims, recovers or releases work. Only a live owner's process group on this host
is inspected. Inspection failure is reported without failing status.

Ownership identifies actor, host, claim time, runtime and lease expiry. Times use
UTC, with a date for other days. Confirmed local processes show their log path;
other states distinguish starting, exited, another host, recovery and unknown.
JSON keeps ownership `state` and adds `process` and `process_reason` (null without
a live lease). Reported outcomes belong to that agent's live run, or latest run
when idle; recovery shows the original report. Missing reports are omitted in text
and null in JSON. The item's live lease may belong to another agent.

```text
{{help status}}
```
