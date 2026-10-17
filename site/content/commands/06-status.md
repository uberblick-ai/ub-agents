# ub-agents status

Run `ub-agents status` to see the queue: what's running, what's next, what's waiting and what needs a person. It never changes anything on GitHub, so it's safe to run anytime.

```sh
ub-agents status          # the whole queue
ub-agents status 143      # why #143 is or isn't running
ub-agents status --json   # the same results, machine readable
```

## One item

`status 143` explains a single issue or pull request with the same checks as [`launch 143`](/docs/commands/launch.html#one-agent-or-one-item): it skips the queue order, but labels, blockers and approvals still apply. For a claimed item it shows who holds it, on which machine and until when, plus its log when it runs on this machine.

## States

- `ready`: launch can start it now.
- `owned`: a launcher is working on it.
- `waiting`: something holds it for now, such as a blocker, a milestone or a failing [health check](/docs/configuration/agents.html#health-check). The line below says what.
- `backoff`: the last run failed, and it waits a while before the next attempt.
- `parked`, `blocked` or `failed`: it needs a person. The Action needed link says what to decide.

```text
#1574 issue-reviewer: parked · priority high · milestone #24 · attempts 0
  Stop label needs-human is present
  Action needed: https://github.com/OWNER/REPO/issues/1574#issuecomment-6166713718 · waiting 16h
```

[Queue](/docs/configuration/queue.html) explains the order and milestones, and [unstick an item](/docs/guides/stuck-items.html) what to do next.

```text
{{help status}}
```
