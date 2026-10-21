# Runtime updates

Keeps Claude Code, Codex and the GitHub CLI (`gh`) up to date, once a day, between runs. Off unless you turn it on; leaving out `gh` runs no `gh` updater.

```yaml
runtime-updates:
  claude: auto
  codex: auto
  gh: auto
  timeout-seconds: 300
```

## Auto

Detects how the CLI was installed and uses its own updater: native installer, npm or Homebrew for the agent CLIs, and Homebrew only for `gh`. System packages (apt, dnf, apk), shims and unknown installs are skipped with a note; give those [your own command](#your-own-command). Nothing uses `sudo`, and missing CLIs are never installed.

```yaml
claude: auto
```

## Your own command

For other installs, give the command that updates only that CLI.

```yaml
codex:
  command: [mise, upgrade, codex]
gh:
  command: [mise, upgrade, gh]
```

Commands run without a shell, in the launcher's environment, and must need no elevated privileges.

## Off

The default for any CLI you leave out.

```yaml
claude: off
```

## Shared

Launchers on one machine share one daily check per installation, across projects, so turning updates off in one project doesn't stop another from updating the same CLI. Claude Code and Codex are checked only when an agent uses them; `gh` whenever it's configured.

Updates wait until no run or queue check is using the CLI. If an update leaves `gh` unusable, every launcher on the machine holds new runs until it works again.
