# Upgrading

A running launcher keeps the version it started with. Instruction files and configuration update automatically while it runs, but a new version needs an explicit upgrade: stop the launcher, upgrade, start it again.

Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases) first. If a release has an **Upgrading** note, stop every launcher before upgrading any of them.

This site's documentation and header version follow release tags. Documentation fixes between releases appear with the next release.

```sh
brew update && brew upgrade ub-agents
ub-agents launch
```

When a newer release exists, the terminal view shows a banner with the command to run.

## Stop

| Key or signal | What happens |
|---|---|
| `q` or `kill -TERM <pid>` | Claims nothing new, finishes the current run, then exits. |
| Ctrl-C | Stops the running agent now. The item keeps its attempt count and runs again on the next launch. |

## With a service

If a [service](/docs/guides/add-a-launcher.html#run-it-as-a-service) restarts the launcher for you, upgrade first, then send `SIGTERM`. The launcher finishes its run and exits, and the service starts the new version.
