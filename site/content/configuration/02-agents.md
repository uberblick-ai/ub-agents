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

Run in a private checkout: the pull request's exact commit, or a fresh branch for an issue. Without it, the agent runs in the launcher's own checkout.

```yaml
worktree: true
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

A GitHub Discussion number in this repository. The agent posts there when a run lost time and it can name the fix. See [Learn from runs](/docs/best-practices/retrospectives.html).

```yaml
retrospectives: 203
```

## Limits

Any key from [Limits](/docs/configuration/limits.html) can be set on one agent.

```yaml
agent-timeout-minutes: 60
```
