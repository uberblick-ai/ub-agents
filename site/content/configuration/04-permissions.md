# Permissions

ub-agents adds no permission flags. The CLIs start in their default headless modes and usually cannot edit files or push. Grant each role what it needs with `runtime-args`.

```yaml
implementer:
  runtime: "codex:gpt-6.1-sol:high"
  runtime-args: [--sandbox, danger-full-access]
```

## Starter permissions

`ub-agents init` writes one commented top-level `runtime-args` mapping entry for the selected CLI. In a terminal it asks once whether to enable that entry for all four starter agents. Otherwise it stays commented out in `ub-agents.yaml`. The list examples below belong inside individual agent definitions.

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

Declare a top-level `runtime-args` mapping to give Claude and Codex their own arguments once, independent of the agents:

```yaml
runtime-args:
  codex: [--sandbox, danger-full-access, -c, 'mcp_servers.example.enabled=true']
  claude: [--permission-mode, acceptEdits, --permission-prompts, none,
           --allowedTools, "Bash(git *)", "Bash(gh *)",
           "Bash({report_command} report *)", "Bash({report_command} read *)",
           --add-dir, "{scratch}"]
agents:
  reviewer:
    runtime: ["codex:gpt-6.1-sol:xhigh", "claude:claude-opus-5-5:xhigh"]
    instructions: .agents/reviewer.md
    trigger: needs-review
    outcomes: {approved: {add: [ready-to-merge]}}
```

Runtime agents inherit the top-level mapping when they omit their own `runtime-args`; command agents do not inherit it. Only the selected CLI's arguments are appended, including when a later run switches alternatives. A missing or empty CLI entry adds no arguments. Top-level keys must be `codex` or `claude`; values must be lists of strings. Every entry is validated, including CLIs no agent currently uses.

An agent's own `runtime-args` replaces the entire top-level mapping. It can be a list applying to every alternative or a mapping whose keys also appear in the agent's runtime list. An omitted CLI key in an agent mapping receives no arguments from the top level. An explicit `[]` or `{}` disables the defaults. YAML aliases can selectively reuse argument lists.

Upgrade every launcher before adopting top-level `runtime-args` or agent mappings. Older builds reject these forms.

## Check

`ub-agents doctor` warns when a runtime agent's effective arguments, after inheritance or replacement, leave any listed CLI with missing or empty arguments. Configure the top-level mapping or agent overrides to cover those CLIs. It does not test whether the arguments grant enough.

```sh
ub-agents doctor
```
