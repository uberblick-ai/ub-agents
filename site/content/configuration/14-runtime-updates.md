# Runtime updates

Keeps Claude Code and Codex up to date, once a day, between runs. Off unless you turn it on.

```yaml
runtime-updates:
  claude: auto
  codex: auto
  timeout-seconds: 300
```

## Auto

Detects how the CLI was installed and uses its own updater: native installer, npm or Homebrew. Unknown installs are skipped with a message.

```yaml
claude: auto
```

## Your own command

For other installs, give the command that updates only that CLI.

```yaml
codex:
  command: [mise, upgrade, codex]
```

## Off

The default for any CLI you leave out.

```yaml
claude: off
```

## Shared

Launchers on one machine share a daily cooldown per installation, across projects. Turning updates off in one project does not stop another project from updating the same CLI.
