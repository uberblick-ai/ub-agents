# Agents

An agent is one role in your workflow, such as implementer or reviewer. It starts when its trigger label appears, does its job and reports an outcome. Each agent has either a `runtime` or a `command`.

```yaml
agents:
  reviewer:
    runtime: "claude:claude-opus-5-5:high"
    kind: pr
    trigger: needs-review
    instructions: .agents/reviewer.md
    worktree: true
    outcomes:
      approved: {add: [ready-to-merge]}
      changes-requested: {add: [needs-changes]}
```

## Trigger

The label that starts this agent. Give a list to start it on any of several labels.

```yaml
trigger: [ready, needs-changes]
```

## Kind

Whether the agent works on issues, pull requests or both. Defaults to `either`.

```yaml
kind: pr    # issue, pr or either
```

## Runtime

The agent CLI, model and effort. See [Runtimes](/docs/configuration/runtimes.html).

```yaml
runtime: "codex:gpt-6.1-sol:high"
```

## Instructions

The Markdown file that describes this agent's job. Required with `runtime`. See [Instructions](/docs/configuration/instructions.html).

```yaml
instructions: .agents/reviewer.md
```

## Worktree

Run in a private checkout: the exact commit for a pull request, or a fresh branch for an issue. Without it, the agent runs in the launcher's own checkout. Fork pull requests are reviewed as a diff; see [outside pull requests](/docs/configuration/approvals.html#outside-pull-requests).

```yaml
worktree: true
```

## Health check

Wait for a project dependency before claiming new work for this agent. Give an argv list; ub-agents runs it without a shell in the control checkout.

```yaml
health-check: [./scripts/check-corpus]
```

The check runs before claiming ready work, with a 60-second timeout, and a pass is reused for five minutes. While it fails, ready items show as `waiting` with the command's last error line, and nothing is claimed, charged or commented for that agent. Other agents keep working. Claiming resumes by itself once the check passes, and active runs are never interrupted. `ub-agents status` runs the check too.

## On conflict

Send a pull request with a merge conflict straight to the agent that repairs it, instead of spending a run only to send it back. Name one of this `pr` agent's outcomes that adds no stop label; the launcher records it without starting the agent.

```yaml
integrator:
  on-conflict: changes-requested
```

## Outcomes

The results this agent can report, and the labels each one changes. Required. See [Outcomes](/docs/configuration/outcomes.html).

```yaml
outcomes:
  approved: {add: [ready-to-merge]}
```

## Different runtime from

Run on a different CLI and model from another agent's accepted outcome. On issues,
this uses that agent's latest accepted successful outcome on the same issue. On
pull requests, it uses the accepted outcome for the current head. A different
effort alone does not count. A successful source release or outcome recovery must
back the outcome; invalid provenance or a started, unfinished source handoff blocks.

```yaml
runtime: ["claude:claude-opus-5-5:high", "codex:gpt-6.1-sol:high"]
different-runtime-from: implementer
```

For independent issue review, use `different-runtime-from: issue-preparer` and
configure both runtime alternatives on the issue reviewer.

To exclude several sources, use a nonempty list:

```yaml
different-runtime-from: [implementer, reviewer]
```

The selected runtime must differ in both CLI and model from every identified
source. If none of the named agents has an accepted outcome for the issue or
current PR head, only the first configured runtime is used. If every runtime is
excluded, planning blocks. Names must be unique and refer to other runtime agents,
not command agents. Doctor checks each named source. Local configuration replaces
the whole value; `null` clears it.

## Runtime arguments

Extra arguments for the CLI, usually permission flags. See [Permissions](/docs/configuration/permissions.html).

```yaml
runtime-args: [--sandbox, danger-full-access]
```

## Retrospectives

The GitHub discussion where this agent posts its [retrospectives](/docs/best-practices/retrospectives.html).

```yaml
retrospectives: 203
```

Claude agents also need `"Bash({report_command} retrospective *)"` in [`--allowedTools`](/docs/configuration/permissions.html#report-command).

## Limits

Timeout and retry keys from [Limits](/docs/configuration/limits.html) can be set on
one agent. The claim timing keys `lease-minutes` and `lease-renew-minutes` can only
be set under the top-level `limits` mapping.

```yaml
agent-timeout-minutes: 60
```

## Key reference

| Key | Type and default | Rules |
|---|---|---|
| `trigger` | Required label or list | Labels that select the agent, subject to [approvals](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md). |
| `outcomes` | Required mapping | See [Outcomes and transitions](/docs/configuration/outcomes.html). |
| `kind` | `issue`, `pr` or `either` (default) | Which items the agent takes. |
| `runtime` | `cli:model:effort` or list | `cli` is `codex` or `claude`. A list gives alternatives in order. |
| `instructions` | Path | Required with `runtime`. Relative to the configuration file, inside the project and readable. Reread before each run. |
| `command` | Argv list | Runs instead of an LLM session. A relative path containing `/` resolves against the configuration's directory. |
| `worktree` | Boolean, `false` | `true` runs in a private checkout: a same-repository PR's exact head, a fork PR's base branch tip, or a fresh branch for an issue. See [pull requests](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#pull-requests). |
| `runtime-args` | List or mapping | Replaces all top-level `runtime-args`; `[]` or `{}` disables them. |
| `different-runtime-from` | Agent name or nonempty list | Use a different CLI and model from each named runtime agent's accepted outcome: the latest on an issue, or for the current PR head. `null` clears the setting. See [runtime independence](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#runtime-independence-and-candidate-provenance). |
| `on-conflict` | Outcome name | `pr` agents only; an outcome that adds no stop label. Recorded instead of starting the agent when GitHub reports the PR as conflicting. |
| `health-check` | Argv list | See [Project health checks](/docs/configuration/agents.html#health-check). |
| `retrospectives` | Positive integer | See [Retrospectives](/docs/configuration/agents.html#retrospectives). |
| Limit keys | | Override the timeout and retry [`limits`](/docs/configuration/limits.html). The lease keys are top level only. |

`health-check` is a nonempty argv list, resolved like `command`; `doctor` never runs it.
`retrospectives` is a positive discussion number in `repository`; `doctor` checks that it exists.

See the [coordination contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#runtime-independence-and-candidate-provenance)
for runtime independence and candidate provenance, and [health check rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#project-health-checks)
for pre-claim health gating.
