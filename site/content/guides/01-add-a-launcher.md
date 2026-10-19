# Add more launchers

A launcher runs wherever you start it: your laptop, a teammate's, or any server you like. A laptop works while it is awake. For a loop that keeps going overnight, use an always-on machine, such as a small cloud server. All launchers for a project share one queue through GitHub, and a claim keeps two of them off the same item.

## On the new machine

Install, clone and log in with an account that has write access.

```sh
brew install uberblick-ai/tap/ub-agents
gh auth login
git clone https://github.com/your-org/your-project
cd your-project
```

## Check and launch

```sh
ub-agents doctor
ub-agents launch
```

## Which accounts count

By default every account with write access or higher can run a launcher. To allow only some, list them, and use the same list on every machine. See [Launcher accounts](/docs/configuration/launchers.html).

```yaml
launchers: [ub-bot, alice]
```

## Several launchers on one machine

One machine can run several loops in parallel: one launcher per project, or several for one project, each in its own checkout. Each run gets its own worktree and scratch directory.

Runs can still collide on resources outside the worktree, such as a Docker database, a fixed port or a shared cache. Give each run its own, for example by naming them after the worktree, and stop them in the [cleanup hook](/docs/configuration/cleanup.html).

## Keep versions together

When a release note says launchers must be upgraded together, stop every launcher for the project before upgrading any. See [Upgrading](/docs/upgrading.html).

## On a server

On a machine you log in to over SSH, run the launcher in tmux so it keeps running after you disconnect. Detach with Ctrl-b d.

```sh
tmux new -A -s ub-agents      # attach, or create on first use
ub-agents launch
```

For the terminal view, add to `~/.tmux.conf`:

```text
set -g mouse on                       # clicks and scrolling reach the view
set -s set-clipboard on               # mouse-drag copy reaches your clipboard (OSC 52)
set -s escape-time 10                 # Escape reaches the view at once
set -as terminal-features ",*:RGB"    # full theme colors
```

Hold Shift (Option in iTerm2) while dragging to use the terminal's own selection.

## Run it as a service

To start the launcher on its own and restart it when it exits, run `ub-agents launch --no-ui` in the launcher checkout as a user service: launchd on macOS, systemd on Linux. Your agent can set it up for you. {{discuss launcher-service}}
