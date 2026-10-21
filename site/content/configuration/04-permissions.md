# Permissions

ub-agents adds no permission flags. The CLIs start in their default headless modes and usually cannot edit files or push. Grant each role what it needs with `runtime-args`.

```yaml
implementer:
  runtime: "codex:gpt-6.1-sol:high"
  runtime-args: [--sandbox, danger-full-access]
```

## Starter permissions

`ub-agents init` writes a commented top-level `runtime-args` entry for the selected CLI. When [init](/docs/commands/init.html) asks whether agents may work without asking, Yes (the default) uncomments it for all starter agents; `--use-defaults` and runs without a terminal leave it commented. Review it before you launch. The retrospectives extra also adds Claude's `retrospective` rule.

## Codex

Full access, including commits in private worktrees. Codex's `workspace-write` sandbox cannot commit there, because their Git metadata lives in the main checkout.

```yaml
runtime-args: [--sandbox, danger-full-access]
```

## Claude Code

File edits plus the commands the role needs, with no prompts. Commands not listed in `--allowedTools` are denied, so add your project's check commands. Read-only commands such as `cat`, `grep`, `ls` and `sed -n` need no rule inside the worktree and the run's scratch directory, and Claude Code refuses paths outside those even with one.

```yaml
runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
               --allowedTools, "Bash(git *)", "Bash(gh *)",
               "Bash(npm test)",
               "Bash({report_command} report *)", "Bash({report_command} read *)",
               --add-dir, "{scratch}"]
```

## Report command

`{report_command}` becomes the absolute path of the launcher's own ub-agents. Claude matches the command text including that path, so `Bash(ub-agents *)` would not match. Agents with a [retrospectives](/docs/best-practices/retrospectives.html) board also need the `retrospective` rule.

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

A top-level `runtime-args` mapping gives Claude and Codex their own arguments once, for every agent:

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

Runtime agents without their own `runtime-args` inherit it; command agents don't. Each run gets the arguments of the CLI it actually uses. Keys are `codex` or `claude`, values lists of strings.

An agent's own `runtime-args` replaces the whole mapping: a list for every alternative, or a mapping by CLI. A CLI it leaves out gets no arguments, and `[]` or `{}` turns the defaults off.

## Check

`ub-agents doctor` warns when any CLI an agent lists ends up with no arguments. It doesn't test whether the arguments grant enough.

```sh
ub-agents doctor
```
