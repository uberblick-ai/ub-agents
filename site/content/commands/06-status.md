# ub-agents status

Run `ub-agents status` to see the queue: what's running, what's next, what's waiting and what needs a person. It never changes anything on GitHub, so it's safe to run anytime.

```sh
ub-agents status          # the whole queue
ub-agents status 143      # why #143 is or isn't running
ub-agents status --json   # the same results, machine readable
```

Status also shows the launcher running on this control checkout, with pid, host
and start time, or says none is running. Each checkout allows one launcher across
all [`launch`](/docs/commands/launch.html) modes; stop the current launcher before
starting another, or use [`retry`](/docs/commands/retry.html) to hand it an item.

## One item

`status 143` explains one item with the same checks as [`launch 143`](/docs/commands/launch.html#one-agent-or-one-item). For a claimed item it shows who holds it, on which machine and until when.

## States

- `ready`: launch can start it now.
- `owned`: a launcher has claimed it. For a run on this machine, status shows `running` while its agent runs.
- `recover`: an expired run left a report, which the next launcher checks without running the agent again.
- `waiting`: something holds it for now, such as a blocker, a milestone or a failing [health check](/docs/configuration/agents.html#health-check). The line below says what.
- `backoff`: the last run failed, and it waits a while before the next attempt.
- `parked`, `blocked` or `failed`: it needs a person. The Action needed link says what to decide.

```text
#1574 issue-reviewer: parked · priority high · milestone #24 · attempts 0
  Stop label needs-human is present
  Action needed: https://github.com/OWNER/REPO/issues/1574#issuecomment-6166713718 · waiting 16h
```

## JSON output

`--json` prints `{"assignments": [...], "launcher": ...}`. The top-level `launcher`
is `{"pid": 1234, "host": "machine", "started_at": "2026-10-25T11:00:00Z"}`
for the launcher holding this checkout's lock, or `null` when none is running.
Leftover owner records do not count as running launchers.

Assignments have one object per row: `number`, `kind`, `agent`, `state`, `reason`, `attempts`, `priority`, `priority_inherited_from`, `priority_from_issue`, `milestone`, `milestone_inherited_from`, `open_blockers`, `runtime`, `candidate_sha`, `result`, `lease`, `process`, `process_reason`, `outcome`, `notice_url`, `waiting_since`, `stop_labels` and `attention_reason`. Unknown values are `null`. The top level adds `milestone_skips` when `gate` passed over a milestone, and `explanation` when `status NUMBER` has no row for the item.

[Queue](/docs/configuration/queue.html) explains the order and milestones, and [unstick an item](/docs/guides/stuck-items.html) what to do next.

```text
{{help status}}
```

Coordination records use format v4. Outcome run identity, runtime, launcher host and original PR head are resolved from the source lease; local process details come from the run directory on the lease's host.
