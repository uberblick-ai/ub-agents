# Upgrading

The launcher never reloads its own code. Stop it, upgrade, start it again.

## Stop

| Key or signal | What happens |
|---|---|
| `q` or `kill -TERM <pid>` | Claims nothing new, finishes the current run, then exits. |
| Ctrl-C | Stops the running agent now. The item keeps its attempt count and runs again on the next launch. |

## Upgrade

Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases) first. An **Upgrading** note says when every launcher must stop before any upgrades.

```sh
brew update && brew upgrade ub-agents
ub-agents launch
```

## Under tmux or systemd

When something restarts the launcher for you, upgrade first and then send `SIGTERM`.

## The update banner

The terminal view shows a banner when a newer release exists, with the command to run.

```text
⬆ ub-agents 0.1.17 is available · you run 0.1.16 · brew update && brew upgrade ub-agents, then restart the launcher
```
