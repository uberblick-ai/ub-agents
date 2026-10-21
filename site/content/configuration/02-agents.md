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

Run in a private checkout: the exact commit for a same-repository pull request,
the base branch tip from `origin` for a fork pull request, or a fresh branch for
an issue. Without it, the agent runs in the launcher's own checkout.
Fork heads are reviewed only as diffs using a run-owned ref; setup is skipped and
repository CI on the exact head SHA supplies check evidence. See
[outside pull requests](/docs/configuration/approvals.html#outside-pull-requests).

```yaml
worktree: true
```

## Health check

Wait for a project dependency before claiming new work for this agent. Give a nonempty argv list; ub-agents runs it without a shell in the control checkout. Relative executables resolve like an agent `command`. The project decides what the command checks.

```yaml
health-check: [./scripts/check-corpus]
```

The check runs only for otherwise-ready work, before the claim or any new assignment write. Its timeout is fixed at 60 seconds. A pass is reused for up to five minutes within the launcher; failures are checked again on the next poll with ready work for that agent.

A nonzero exit, start failure or timeout makes ready items `waiting`, with the command and the last nonempty stderr line, falling back to stdout or the start or timeout error. One notice appears when the check starts failing or its error line changes, and one when it passes again; launch output also goes to `.ub-agents/launch.log`. Waiting items remain in Eligible, outside Needs attention, with no claims, attempts, label changes or item comments for new work. Other agents keep claiming, and `launch --once` exits normally.

`ub-agents status` runs the check itself, so it works without a launcher on the same machine. Claiming resumes automatically when the check passes, without `retry`. Checks do not interrupt active runs, stop recovery of recorded outcomes or reset previously parked items. `doctor --offline` validates the argv list without running it, and `doctor` does not run it. Project checks are separate from [daily runtime maintenance](/docs/configuration/runtime-updates.html).

## On conflict

Send a pull request GitHub reports as conflicting straight to the agent that repairs it. Name one of this `pr` agent's outcomes that adds no stop label. After the claim, the launcher records that outcome with a summary naming the conflict instead of starting the agent, so the integrator does not spend a run only to send the conflict back. An unknown merge state starts the agent as usual.

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
