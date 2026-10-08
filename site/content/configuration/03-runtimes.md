# Runtimes

A runtime is the agent CLI that does the work. Claude Code and Codex are built in. Anything else can run as a [command](/docs/configuration/command-agents.html).

```yaml
runtime: "claude:claude-opus-5-5:high"
```

## CLI, model and effort

Written as `cli:model:effort`. The CLI is `claude` or `codex`. `opencode`, for local models through Ollama, is planned and not supported yet.

```yaml
runtime: "claude:claude-opus-5-5:high"
runtime: "codex:gpt-6.1-sol:high"
runtime: "opencode:ollama/qwen3.8:high"   # planned, not supported yet
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

`runtime-args` must not change the model or effort or resume a session. Claude arguments must not set `-c` (continue a session) or `--output-format`; Codex arguments must not set `--ephemeral`. `ub-agents check` rejects them.

Declare [per-CLI arguments](/docs/configuration/permissions.html#arguments-for-alternatives) once with a top-level `runtime-args` mapping. Agents inherit it unless they provide their own list or mapping, which replaces all defaults. Each mapping entry follows its CLI's rules and the same placeholder rules as an agent-level list. Codex's `-c` for settings such as MCP servers is accepted in the `codex` entry even when an agent also lists Claude. A shared agent-level list must satisfy every listed CLI's rules.
