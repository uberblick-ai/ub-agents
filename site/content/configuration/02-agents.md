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

## Health check

Wait for a project dependency before claiming new work for this agent. Give a nonempty argv list; ub-agents runs it without a shell in the control checkout. Relative executables resolve like an agent `command`. The project decides what the command checks.

```yaml
health-check: [./scripts/check-corpus]
```

The check runs only for otherwise-ready work, before the claim or any new assignment write. Its timeout is fixed at 60 seconds. A pass is reused for up to five minutes within the launcher; failures are checked again on the next poll with ready work for that agent.

A nonzero exit, start failure or timeout makes ready items `waiting`, with the command and the last nonempty stderr line, falling back to stdout or the start or timeout error. One notice appears when the check starts failing or its error line changes, and one when it passes again; launch output also goes to `.ub-agents/launch.log`. Waiting items remain in Eligible, outside Needs attention, with no claims, attempts, label changes or item comments for new work. Other agents keep claiming, and `launch --once` exits normally.

`ub-agents status` runs the check itself, so it works without a launcher on the same machine. Claiming resumes automatically when the check passes, without `retry`. Checks do not interrupt active runs, stop recovery of recorded outcomes or reset previously parked items. `check` validates the argv list without running it, and `doctor` does not run it. Project checks are separate from [daily runtime maintenance](/docs/configuration/runtime-updates.html).

**Upgrading:** Upgrade every launcher of a project before adding `health-check`; older versions reject the unknown agent key.

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

A GitHub Discussion number in this repository. Only agents with this key receive retrospective guidance. A loss paragraph needs a real loss or something the run needed but missed, and a change that would have prevented it.

```yaml
retrospectives: 203
```

A post may consist of, or end with, at most two optional topic lines, even when the run lost nothing:

```text
Would have helped: topic A, topic B
Suggest creating: topic C, topic D
```

The first names existing guidance, records or pointers the run lacked or had to reconstruct. The second names lasting choices no record covers for later human review. Omit empty lines, use short comma-separated topic names, and reuse a name from an existing record or earlier post when it fits. Include no per-topic rationale, scores, identifiers or other structure; link the item for evidence. Posting is optional, at most once per item, without repeating an earlier run's post. Use a scratch body file and keep public posts free of credentials, environment values, local paths, hostnames and log excerpts; a failed post blocks nothing.

A person groups topic lines over several weeks and reads the linked items for rationale before deciding which records to create. Repetition is evidence, not a creation threshold: one consequential compatibility or security choice can merit a record. Records are never created automatically; this route uses no scoring, analytics service or scheduler.

A project's own rules decide when a run creates a record. They can defer optional new records to topic lines, so a missing proposed record alone does not block completion. Binding decisions, behavior guarantees and human-approval gates stay in force; this framework guidance does not override project rules. See [Learn from runs](/docs/best-practices/retrospectives.html).

## Limits

Any key from [Limits](/docs/configuration/limits.html) can be set on one agent.

```yaml
agent-timeout-minutes: 60
```
