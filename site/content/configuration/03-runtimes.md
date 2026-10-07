# Runtimes

A runtime is the agent CLI that does the work. Claude Code and Codex are built in. Anything else can run as a [command](/docs/configuration/command-agents.html).

```yaml
runtime: "claude:claude-opus-5-5:high"
```

## CLI, model and effort

Written as `cli:model:effort`. The CLI is `claude` or `codex`.

```yaml
runtime: "claude:claude-opus-5-5:high"
runtime: "codex:gpt-6.1-sol:high"
```

## Alternatives

Give a list. The first one that is installed and allowed runs.

```yaml
runtime: ["claude:claude-opus-5-5:high", "codex:gpt-6.1-sol:high"]
```

## What runs

The prompt, made of the assignment context and the agent's instructions, arrives on stdin. Every run starts a fresh session.

```sh
claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT
codex exec --json --model MODEL --config model_reasoning_effort="EFFORT"
```

## Usage limits

Nothing to configure. When a run reports a usage limit, the launcher stops starting runs on that CLI until the limit resets, plus one minute. Other CLIs keep working, and the item spends no attempt. Restarting the launcher clears the pause.

```text
claude usage limit reached; pausing claude runs until 2026-10-04T12:01:00Z
```

## Not allowed

`runtime-args` must not change the model or effort, resume a session, set Claude's `--output-format` or Codex's `--ephemeral`. `ub-agents check` rejects them.
