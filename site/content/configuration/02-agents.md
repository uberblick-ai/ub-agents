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

Make a pull request agent run on a different CLI and model from the agent that wrote the current commit. A different effort alone does not count.

```yaml
runtime: ["claude:claude-opus-5-5:high", "codex:gpt-6.1-sol:high"]
different-runtime-from: implementer
```

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
