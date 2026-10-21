# Upgrading

A running launcher picks up configuration and instruction changes by itself, but keeps the ub-agents version it started with. To upgrade, stop it, upgrade and start it again. Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases) first: if a release says launchers must be upgraded together, stop every launcher before upgrading any.

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

If a [service](/docs/guides/add-a-launcher.html#run-it-as-a-service) restarts the launcher, upgrade first, then send `SIGTERM`. The launcher finishes its run, and the service starts the new version.
