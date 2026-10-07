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

Detects how the CLI was installed and uses its own updater: native installer, npm or Homebrew for the agent CLIs. For `gh`, only a Homebrew formula under `Cellar/gh` on macOS or Linux is updated, with `brew upgrade --formula gh` using its owning Homebrew. The installation must be writable; dependent upgrades, cleanup, application quitting, sudo and interactive confirmation are disabled.

System packages (apt, dnf or apk) are skipped as needing elevated privileges and must be updated manually. Shims and unknown `gh` installs are skipped with an instruction to configure `runtime-updates.gh.command`. Missing CLIs are reported and never installed. No updater uses `sudo`.

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

Commands run as argv without a shell, use the launcher's environment, and must need no elevated privileges. Claude's `DISABLE_UPDATES` policy does not apply to `gh`.

## Off

The default for any CLI you leave out.

```yaml
claude: off
```

## Shared

Launchers on one machine share a daily cooldown per installation, across projects. Turning updates off in one project does not stop another project from updating the same CLI.

Claude Code and Codex are checked only when configured agent runtimes use them. `gh` is checked whenever its policy is `auto` or a command, including projects with only command agents, because every launcher and agent uses it.

Checks run at unclaimed launcher boundaries. Every run reserves `gh` through execution and cleanup, so an active run defers its updater without starting a cooldown. Completed checks print a maintenance line and start a shared 24-hour cooldown; automatic skips stay local to the launcher. A check that leaves `gh` unusable blocks every new run until a later boundary finds it working again, including launchers with updates off.

**Upgrading:** upgrade every launcher of a project before setting `runtime-updates.gh`; earlier launchers reject that key.
