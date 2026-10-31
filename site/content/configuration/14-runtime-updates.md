# Runtime updates

Keeps Claude Code, Codex, the GitHub CLI (`gh`), Uberblick (`ub`) and the launcher itself up to date, once a day, between runs. Off unless you turn it on; leaving out a CLI runs no updater for it.

```yaml
runtime-updates:
  claude: auto
  codex: auto
  gh: auto
  ub: auto
  ub-agents: auto
  timeout-seconds: 300
```

## Auto

Detects how the CLI was installed and uses its own updater: native installer, npm or Homebrew for the agent CLIs, and Homebrew only for `gh` and `ub`. For `ub`, only the `uberblick` formula qualifies: its owning Homebrew runs `brew upgrade --formula uberblick`. Other formulas, casks, system packages (apt, dnf, apk), shims and unknown installs are skipped with a note; give those [your own command](#your-own-command). Nothing uses `sudo`, and missing CLIs are never installed.

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
ub:
  command: [ub, update]
```

Commands run without a shell, in the launcher's environment, and must need no elevated privileges.
`ub-agents` accepts only `auto` or `off`, without a custom command.

## Updating the launcher

`ub-agents: auto` applies only to a continuous `launch`, with or without `--agent`.
`launch NUMBER` and `--once` keep showing the banner. Only the Homebrew formula
`uberblick-ai/tap/ub-agents` qualifies; `doctor` reports what it found or why it will
skip. Upgrade older launchers before adding this key: they reject it.

The launcher waits until no run, recovery, checkout refresh or setup is active,
then checks and upgrades its owning Homebrew installation. It claims nothing until
maintenance finishes. `timeout-seconds` limits the whole operation. Failures go to
`launch.log` and wait for the next daily update check before retrying.

It restarts only if the installed version is newer than its running version,
including when another launcher or a person installed it. A successful command
that leaves the version unchanged causes no restart. Every release between the
running and intended version must have a readable marker allowing independent
launcher upgrades. A coordinated release or an unreadable marker keeps the banner
with a reason; follow [Upgrading](/docs/upgrading.html) to upgrade manually.

Restart replaces the process, keeping its PID and arguments in the terminal view
and `--no-ui`; a service manager keeps supervising the same process. `q` or
`SIGTERM` during an upgrade lets it finish and then exits 0 without restarting.
The launcher and helpers keep their startup code and dependencies if the old
ub-agents keg is removed, even with updates off. The separate Python installation
must remain available.

## Off

The default for any CLI you leave out.

```yaml
claude: off
```

## Shared

For Claude Code, Codex, `gh` and `ub`, launchers on one machine share one daily check per installation, across projects, so turning updates off in one project doesn't stop another from updating the same CLI. Launcher upgrades are serialized per installation; each launcher's daily release check can request one. Claude Code and Codex are checked only when an agent uses them; `gh` whenever it's configured. `ub` is checked when it would find `.uberblick.json` in the project root or an ancestor, regardless of the agent runtime. Without a workspace file, a configured `ub` policy reports an unused skip.

Updates wait until no run or queue check is using the CLI. Every run in an Uberblick project reserves an installed `ub` through execution and cleanup, even with updates off, so the next run starts a fresh MCP server after an update. A missing `ub` does not hold runs unless an earlier update left it unusable. If an update leaves `ub` unusable, new runs in Uberblick projects wait until it works again; an unusable `gh` holds every project's new runs.

`ub-agents init` includes `ub: auto` when you enable updates and select the Uberblick extra.
