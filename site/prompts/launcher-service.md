Set up the ub-agents launcher in this checkout as a user service that starts on its own and restarts when it exits: a launchd agent on macOS or a systemd user unit on Linux.
It runs `ub-agents launch --no-ui` from this directory, with a PATH that finds ub-agents, git, gh and my agent CLI. The launcher already logs to .ub-agents/launch.log, so the service needs no log of its own.
Stopping sends SIGTERM, and the launcher then finishes its current run before it exits, which can take an hour or more. Set the stop timeout high enough that the service manager never kills it sooner; see https://agents.uberblick.ai/docs/upgrading.
Show me the service file and the commands to start, stop and check it, and ask before installing anything.
