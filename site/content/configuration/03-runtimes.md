# Runtimes

A runtime is the agent CLI that does the work. Claude Code and Codex are built in. Anything else can run as a [command](/docs/configuration/command-agents.html).

```yaml
runtime: "claude:claude-opus-5-5:high"
```

## CLI, model and effort

Written as `cli:model:effort`. The CLI is `claude` or `codex`.

```yaml
runtime: "claude:claude-opus-5-5:high"
runtime: "codex:gpt-6.2-sol:high"
```

## Alternatives

Give a list. The first one that is installed and allowed runs. To change the list on one machine, use a [machine-local override](/docs/configuration/index.html#machine-local-overrides).

```yaml
runtime: ["claude:claude-opus-5-5:high", "codex:gpt-6.2-sol:high"]
```

## What runs

The prompt, made of the assignment context and the agent's instructions, arrives on stdin. Every run starts a fresh session.

```sh
claude --print --output-format stream-json --verbose --model MODEL --effort EFFORT --thinking-display summarized
codex exec --json --model MODEL --config model_reasoning_effort="EFFORT" --config model_reasoning_summary="detailed"
```

`process.log` records CLI JSON output as is; only the
[report command](/docs/commands/loop-internal.html#report) sets an outcome.

## Usage limits

Nothing to configure. When a run reports a usage limit, the launcher stops starting runs on that CLI until the limit resets, plus one minute. Other CLIs keep working, and the item spends no attempt. Restarting the launcher clears the pause. The terminal footer shows each pause, such as `claude paused for 1d 4h · codex paused for 35m`.

```text
claude usage limit reached; pausing claude runs until 2026-10-04T12:01:00Z
```

If the reset is missing, past, or more than seven days away, the pause is 15 minutes.
Pauses are held in memory per launcher; alternatives skip paused CLIs, and an item
with every runtime paused stays `waiting`. See the [usage pause contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#runtime-usage-pauses)
for detection and reset rules.

## Rejected CLI settings

If Claude Code or Codex rejects an option or value at startup, before producing
any JSON output, the launcher releases the run as `retry` without charging an
attempt. It blocks that role's runtime alternative. Other alternatives and roles
keep running. An item with every eligible alternative blocked stays `waiting`;
this failure adds no stop label or action-needed notice.

`status`, the terminal view and `doctor` show this checkout's live launcher blocks,
including the role, alternative, rejected setting and how to clear it. Doctor
reports them as warnings. Blocks stay in that launcher's memory and have no timer;
they do not change the shared installation health.

To clear a block, change the affected alternative's `runtime-args`, model or effort,
install a different CLI version, update launcher flags, or restart the launcher.
The launcher rechecks commands on configuration reload and versions at runtime
maintenance boundaries. A successful `--version` with the same version leaves the
block in place. Per-run scratch and report-command substitutions do not clear it.
See the [rejection contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#runtime-command-rejections)
for the recognised diagnostics. Other startup failures spend an attempt as usual.

## Not allowed

`runtime-args` must not change the model or effort or resume a session. Claude arguments must not set `-c` (continue a session) or `--output-format`; Codex arguments must not set `--ephemeral`. `ub-agents doctor --offline` rejects them.

Codex's `-c` for settings such as MCP servers is fine in a `codex` entry of the [per-CLI mapping](/docs/configuration/permissions.html#arguments-for-alternatives). A single list shared by several CLIs must follow every listed CLI's rules.
