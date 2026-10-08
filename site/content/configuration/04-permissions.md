# Permissions

ub-agents adds no permission flags. The CLIs start in their default headless modes and usually cannot edit files or push. Grant each role what it needs with `runtime-args`.

```yaml
implementer:
  runtime: "codex:gpt-6.1-sol:high"
  runtime-args: [--sandbox, danger-full-access]
```

## Starter permissions

In a terminal, `ub-agents init` asks once whether to turn on the examples below for all four starter agents. Otherwise they stay commented out in `ub-agents.yaml`.

## Codex

Full access, including commits in private worktrees. Codex's `workspace-write` sandbox cannot commit there, because their Git metadata lives in the main checkout.

```yaml
runtime-args: [--sandbox, danger-full-access]
```

## Claude Code

File edits plus the commands the role needs, with no prompts. Commands not listed in `--allowedTools` are denied, so add your project's check commands.

```yaml
runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
               --allowedTools, "Bash(git *)", "Bash(gh *)",
               "Bash(npm test)",
               "Bash({report_command} report *)", "Bash({report_command} read *)",
               --add-dir, "{scratch}"]
```

## Report command

`{report_command}` becomes an absolute command running the launcher's startup code copy with its interpreter. A rule like `Bash(ub-agents *)` would not match it, because Claude matches the command text including its path. Agents with a [retrospectives](/docs/best-practices/retrospectives.html) board also need the `retrospective` rule.

```yaml
"Bash({report_command} report *)"
"Bash({report_command} read *)"
"Bash({report_command} retrospective *)"
```

## Scratch

`{scratch}` becomes the run's private scratch directory, outside the worktree. Quote it, or YAML reads it as a mapping.

```yaml
runtime-args: [--add-dir, "{scratch}"]
```

## Arguments for alternatives

A `runtime-args` list applies to every runtime alternative. Use a mapping keyed by CLI to give Claude and Codex their own arguments:

```yaml
runtime: ["codex:gpt-6.1-sol:xhigh", "claude:claude-opus-5-5:xhigh"]
runtime-args:
  codex: [--sandbox, danger-full-access, -c, 'mcp_servers.example.enabled=true']
  claude: [--permission-mode, acceptEdits, --permission-prompts, none,
           --allowedTools, "Bash(git *)", "Bash(gh *)",
           "Bash({report_command} report *)", "Bash({report_command} read *)",
           --add-dir, "{scratch}"]
```

Only the selected CLI's arguments are appended, including when a later run switches alternatives. A CLI without a key gets no extra arguments. Keys must be `codex` or `claude` and appear in the agent's runtime list; values must be lists of strings. YAML aliases such as `codex: *codex-scoped` can reuse argument lists.

Upgrade every launcher to a build supporting mappings before adopting this form. Older builds reject it.

## Check

`ub-agents doctor` warns about runtime agents without `runtime-args`, including mappings with missing or empty arguments for any listed CLI. It does not test whether the arguments grant enough.

```sh
ub-agents doctor
```
