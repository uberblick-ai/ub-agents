# Upgrading

A running launcher keeps the version it started with. Stop it, upgrade, start it again.

## Stop

| Key or signal | What happens |
|---|---|
| `q` or `kill -TERM <pid>` | Claims nothing new, finishes the current run, then exits. |
| Ctrl-C | Stops the running agent now. The item keeps its attempt count and runs again on the next launch. |

## Upgrade

Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases) first. If a release has an **Upgrading** note, stop every launcher before upgrading any of them.

```sh
brew update && brew upgrade ub-agents
ub-agents launch
```

If a service such as systemd restarts the launcher for you, upgrade first, then send `SIGTERM`. The launcher finishes its run and exits, and the service starts the new version.

When a newer release exists, the terminal view shows a banner with the command to run.
