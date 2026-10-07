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

An argument list, run without a shell. A relative path starts from the directory of `ub-agents.yaml`.

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
| `UB_AGENTS_REPORT` | Report command using the launcher's startup code copy and interpreter |
| `UB_AGENTS_REPOSITORY` | `owner/name` |
| `UB_AGENTS_ASSIGNMENT` | Issue or pull request number |
| `UB_AGENTS_RUN` | Run id |
| `UB_AGENTS_SCRATCH` | Private scratch directory, also `TMPDIR` |
| `UB_AGENTS_CANDIDATE_SHA` | The pull request's head commit |
| `UB_AGENTS_BRANCH` | The branch to work on, when known |

## Scratch

Write temporary files to `UB_AGENTS_SCRATCH`. It is removed when the run ends; run logs stay.
