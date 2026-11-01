# Upgrading

A running launcher picks up configuration and instruction changes by itself, but keeps the ub-agents version it started with until it restarts. Most releases let you upgrade and restart launchers one at a time. Check the [release notes](https://github.com/uberblick-ai/ub-agents/releases) before upgrading: they say when every launcher must be stopped and upgraded together because of a coordination record-format change or a new config key that older launchers reject.

For automatic upgrades, opt in after upgrading existing launchers to a version
that accepts this setting; older launchers reject the new key:

```yaml
runtime-updates:
  ub-agents: auto
```

Only continuous queues installed with `brew install uberblick-ai/tap/ub-agents`
qualify, including queues scoped with `--agent`. `--once`, numbered launches, pip
and source installs keep the banner. The daily check upgrades only while idle,
outside runs, recovery, checkout refresh and setup, using `runtime-updates`
`timeout-seconds` (300 by default). It restarts only on a newer installed version,
including one installed manually or by another launcher. Failed upgrades are
logged and retried at the next daily check.
Automatic launcher upgrades and restarts wait until any configuration pause clears.

Every release between the running and target versions must explicitly allow
independent upgrades. A coordinated release, missing marker or unreadable release
range blocks automatic upgrading and restarting, with a reason in the banner.
Stop every launcher for the project and upgrade manually in that case. The
launcher and its helpers keep their startup code and dependencies if the old
ub-agents keg is removed, even when automatic updates are off; keep the separate
Python installation available.

To upgrade manually, stop the launcher, upgrade and start it again:

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

Automatic updates replace the process with the same PID and arguments, including
`--no-ui`, so the service needs no special restart exit status. Use the service's
normal restart policy for actual exits. `q` or `SIGTERM` during an upgrade lets it
finish and exits 0 without restarting; stop the service too if its policy restarts
successful exits. A failed process replacement exits 1.

Coordination format v4 requires a coordinated upgrade: stop every launcher with no runs in flight, upgrade every machine, then restart. v3 records stop being read, resetting attempt counts, max-rounds counts, blocked state and earlier-run branches. Before restarting, park items that must stay stopped with a stop label and close or finish agent draft PRs from earlier runs.
