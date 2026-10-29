# Upgrading

A running launcher picks up configuration and instruction changes by itself, but keeps the ub-agents version it started with. To upgrade, stop it, upgrade and start it again. Most releases let you upgrade and restart launchers one at a time. Check the [release notes](https://github.com/uberblick-ai/ub-agents/releases) before upgrading: they say when every launcher must be stopped and upgraded together because of a coordination record-format change or a new config key that older launchers reject.

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

Coordination format v4 requires a coordinated upgrade: stop every launcher with no runs in flight, upgrade every machine, then restart. v3 records stop being read, resetting attempt counts, max-rounds counts, blocked state and earlier-run branches. Before restarting, park items that must stay stopped with a stop label and close or finish agent draft PRs from earlier runs.
