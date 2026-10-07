# Review with a different model

A second model catches what the first one missed. Make the reviewer run on a [different CLI and model](/docs/configuration/agents.html#different-runtime-from) from the agent that wrote the commit.

```yaml
reviewer:
  runtime: ["claude:claude-opus-5-5:high", "codex:gpt-6.1-sol:high"]
  different-runtime-from: implementer
```

## How it picks

The reviewer uses the first runtime in its [list](/docs/configuration/runtimes.html#alternatives) that differs from the implementer's recorded one. A different effort alone does not count.

## Keep both installed

If no different runtime is available, the reviewer does not run, rather than review its own model's work.
