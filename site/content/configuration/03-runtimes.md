# Runtimes

A runtime is the agent CLI that does the work. Claude Code, Codex and opencode are built in. Anything else can run as a [command](/docs/configuration/command-agents.html).

```yaml
runtime: "claude:claude-opus-5-5:high"
```

## CLI, model and effort

Claude Code and Codex use `cli:model:effort`. Opencode uses `opencode:provider/model`, with no effort part.

```yaml
runtime: "claude:claude-opus-5-5:high"
runtime: "codex:gpt-6.1-sol:high"
runtime: "opencode:ollama/qwen3.8:27b-mlx"
```

For opencode, everything after the first `:` is the model ID, passed as-is. Both provider and model must be nonempty; Ollama tags may contain colons. Configure the provider in opencode first, then use an ID from `opencode models`. See [opencode models](https://opencode.ai/en/docs/models/).

**Upgrading:** Upgrade every launcher of a project before any machine configures an opencode runtime. Older launchers cannot parse its runtime in coordination records.

## Alternatives

Give a list. The first one that is installed and allowed runs. To change the list on one machine, use a [machine-local override](/docs/configuration/index.html#machine-local-overrides).

```yaml
runtime: ["codex:gpt-6.1-sol:high", "opencode:ollama/qwen3.8:27b-mlx"]
```

Runtime independence still requires a different CLI and model. The opencode provider prefix is ignored for model comparisons: `opencode:openai/gpt-6.1-sol` is not independent of `codex:gpt-6.1-sol:high`. Two opencode runtimes are never independent of each other.

## What runs

The prompt, made of the assignment context and the agent's instructions, arrives on stdin. Every run starts a fresh session.

```sh
claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT --thinking-display summarized
codex exec --json --model MODEL --config model_reasoning_effort="EFFORT" --config model_reasoning_summary="detailed"
opencode run --format json --model PROVIDER/MODEL
```

Ub-agents adds no permission flags. The Log pane formats opencode messages, tool calls and token usage when reported. `ub-agents doctor` checks the executable and whether `opencode models` lists the configured model; it does not run inference.

## Usage limits

Nothing to configure. When a run reports a usage limit, the launcher stops starting runs on that CLI until the limit resets, plus one minute. Other CLIs keep working, and the item spends no attempt. Restarting the launcher clears the pause. The terminal footer shows each pause, such as `claude paused for 1d 4h · codex paused for 35m`.

```text
claude usage limit reached; pausing claude runs until 2026-10-04T12:01:00Z
```

Opencode pauses only when an error explicitly marks a usage or rate limit, including an API status of 429. Other errors fail normally. Local Ollama runs never pause and remain available during a hosted opencode pause. Without a reported reset, the pause lasts 15 minutes.

## Not allowed

`runtime-args` must not change the model or effort or resume a session. Claude arguments must not set `-c` (continue a session) or `--output-format`; Codex arguments must not set `--ephemeral`. `ub-agents doctor --offline` rejects them.

Codex's `-c` for settings such as MCP servers is fine in a `codex` entry of the [per-CLI mapping](/docs/configuration/permissions.html#arguments-for-alternatives). A single list shared by several CLIs must follow every listed CLI's rules.

Opencode arguments must not set `--model`/`-m`, `--variant`, `--format`, or continue, resume or fork a session (`--continue`/`-c`, `--session`/`-s`, `--resume`, `--fork`). Use `runtime` for the full provider/model ID; there is no variant or effort setting in ub-agents.
