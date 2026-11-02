# Command agents

An agent can be any program instead of an agent CLI: a script, a linter or another tool.

```yaml
agents:
  investigate:
    command: [./scripts/investigate-issue]
    kind: issue
    trigger: investigate
    outcomes:
      investigated: {}
    agent-timeout-minutes: 20
```

## Command

A nonempty argv list, used instead of `runtime`, run without a shell. A relative path containing `/` resolves against the directory of `ub-agents.yaml`.

```yaml
command: [./scripts/investigate-issue]
```

## Reporting

Report like any agent, through the command in `UB_AGENTS_REPORT`.

```python
subprocess.run(shlex.split(os.environ["UB_AGENTS_REPORT"]) +
               ["report", "--outcome", "investigated", "--summary", "Done"],
               check=True)
```

## Environment

Commands and agent CLIs get the same variables.

| Variable | Value |
|---|---|
| `UB_AGENTS_CONTEXT` | Path to a JSON file describing the assignment |
| `UB_AGENTS_REPORT` | Absolute command for the launcher's startup code copy and interpreter, followed by `report`, `read` or `retrospective`; also `report_command` in context |
| `UB_AGENTS_RUN_CONFIG` | Absolute path to `.ub-agents/runs/<run>/run.json` |
| `UB_AGENTS_REPOSITORY` | `owner/name` |
| `UB_AGENTS_ASSIGNMENT` | Issue or PR number |
| `UB_AGENTS_RUN` | Run id |
| `UB_AGENTS_SCRATCH` | Absolute path to the run's private scratch directory |
| `UB_AGENTS_LEASE_ID` | The claim's comment id |
| `UB_AGENTS_CANDIDATE_SHA` | The PR's head commit; empty for issues |
| `UB_AGENTS_BRANCH` | The branch to work on, when known |

`TMPDIR` is inherited unchanged.

## Scratch

Write temporary files to `UB_AGENTS_SCRATCH`. It is removed when the run's processes have ended; run logs stay for 7 days.

`UB_AGENTS_REPORT` is shell-quoted; split it with `shlex.split` to run without a shell.
See [execution boundaries](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#execution-boundaries) for scratch ownership
and [explicit outcomes](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#explicit-outcomes) for pinned run identity.
