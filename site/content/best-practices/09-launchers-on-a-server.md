# Run launchers on a server

A launcher on an always-on machine keeps the loop going overnight. Its agents run pull-request code as the launcher's user, with its environment, so this page is about what that user can reach and how you notice a stopped loop. Each practice says what to achieve; your service manager, cloud and monitoring decide how.

## Isolate the box

- A machine or VM that does nothing else, rebuilt from code rather than repaired. A run can change anything the agent user owns, such as its services, crontab, SSH keys and tool installs, so replace a box you doubt.
- An agent user without sudo and outside the `docker` group, with a rootless container daemon of its own if checks need one. Root runs nothing from a directory that user can write to.
- No inbound traffic and no route into your team's environments. On a VPN, people may reach the box and the box reaches nothing; a canary that alerts when it can reach staging catches a policy that drifted.
- The cloud metadata service blocked for the agent user, and no long-lived secret in the machine's setup data.

## Only the runtimes' credentials

Agents inherit the launcher's environment, so it holds what the CLIs need and nothing else. See [Give launchers their own account](/docs/best-practices/launcher-account.html).

- **GitHub:** a fine-grained token for the loop's repositories. A classic `repo` token opens every repository the account can write to. Machines on one account share its [rate limit](/docs/best-practices/account-capacity.html).
- **Claude Code:** a token from `claude setup-token`.
- **Logins that refresh themselves,** such as Codex on a ChatGPT seat or the Uberblick hub: log in on the box. A copied login goes stale.
- **Encrypted repository secrets:** no person's key. Give the box a key of its own and share only what an agent may hold.

## Run it as a service

Run `ub-agents launch --no-ui` in the launcher's checkout under your service manager. Your agent can write the service file. {{discuss launcher-service}} It should:

- **restart on every exit,** with a growing delay and no start limit
- **stop with SIGINT,** which releases the claim at once and charges no attempt. SIGTERM waits for the run, and a stop timeout that kills it charges an attempt and holds the item for 30 minutes.

| | Restart on every exit | Stop with SIGINT | Growing delay |
|---|---|---|---|
| systemd | `Restart=always`, `StartLimitIntervalSec=0` | `KillSignal=SIGINT`, `KillMode=mixed` | `RestartSteps=6`, `RestartMaxDelaySec=30min` |
| launchd | `KeepAlive` | not configurable | fixed `ThrottleInterval` |
| Docker Compose | `restart: unless-stopped` | `stop_signal: SIGINT` | built in |

The launcher writes its own log, `.ub-agents/launch.log`. For the live view, have the service start it in tmux, with one tmux server per launcher. `q` in the view drains it, and the service starts it again.

## Keep it current

With ub-agents installed from the Homebrew tap, turn on [launcher updates](/docs/configuration/runtime-updates.html#updating-the-launcher). The launcher upgrades between runs and restarts in place, so the service sees no exit. Releases that need every launcher upgraded together wait for you; see [Upgrading](/docs/upgrading.html). Don't add an updater of your own: it can't tell those releases apart.

```yaml
runtime-updates:
  ub-agents: auto
  claude: auto
  gh: auto
```

Install the agent CLIs the way [runtime updates](/docs/configuration/runtime-updates.html) recognise, or give it your own command.

## Size for the peak run

A box with 8 shared vCPUs and 16 GB ran three launchers for two weeks: 454 runs, at most two at once, memory peaking at 11 GB. A run with a database, a cache and a search engine in containers peaks at 4–5 GB; one TypeScript repository's builds reached 8.5 GB plus 7.5 GB of swap. Cap memory per launcher and for its containers, add swap, and cap test workers per run.

## Watch what the service doesn't show

- restarts in a window: a crashing launcher still looks active
- an enabled launcher that hasn't run for 15 minutes
- a running version behind the installed one for over a day: a coordinated release is waiting for you
- a failing [`ub-agents doctor --json`](/docs/commands/doctor.html), run every few hours: a launcher stays up while every run fails on an expired login

Collect these as root or from outside the box, so a run can't make a broken loop look healthy.
