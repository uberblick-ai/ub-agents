# Runtime updates

Keeps Claude Code, Codex, the GitHub CLI (`gh`) and Uberblick (`ub`) up to date, once a day, between runs. Off unless you turn it on; leaving out a CLI runs no updater for it.

```yaml
runtime-updates:
  claude: auto
  codex: auto
  gh: auto
  ub: auto
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

## Off

The default for any CLI you leave out.

```yaml
claude: off
```

## Shared

Launchers on one machine share one daily check per installation, across projects, so turning updates off in one project doesn't stop another from updating the same CLI. Claude Code and Codex are checked only when an agent uses them; `gh` whenever it's configured. `ub` is checked when it would find `.uberblick.json` in the project root or an ancestor, regardless of the agent runtime. Without a workspace file, a configured `ub` policy reports an unused skip.

Updates wait until no run or queue check is using the CLI. Every run in an Uberblick project reserves an installed `ub` through execution and cleanup, even with updates off, so the next run starts a fresh MCP server after an update. A missing `ub` does not hold runs unless an earlier update left it unusable. If an update leaves `ub` unusable, new runs in Uberblick projects wait until it works again; an unusable `gh` holds every project's new runs.

`ub-agents init` includes `ub: auto` when you enable updates and select the Uberblick extra.
