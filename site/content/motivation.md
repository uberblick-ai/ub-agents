# Motivation

ub-agents started because we spent more time managing coding agents than letting them work: starting sessions, waiting for an agent to ask us something, passing work between implementers and reviewers, restarting failed runs. We spent more time waiting than deciding.

We wanted agents to take an issue through implementation, review and integration while we worked on something else, without running another platform to coordinate them. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## Why not just write a script?

You can. Polling GitHub and starting an agent is easy. The hard parts are coordinating several launchers, recovering interrupted runs, limiting review cycles and keeping track of what happened. ub-agents packages those, plus a nifty [terminal UI](/docs/commands/launch.html). If a short script covers your workflow, use the script.

## No platform to run

A loop with its own server, database, queue and webhooks is one more system to host, secure and debug, and it keeps part of your work away from where your team looks. Your issues and pull requests already live on GitHub, so the loop keeps its queue and history there too, as labels and comments. A launcher needs little more than a checkout and an agent CLI.

## No lock-in

Agents and models change every few months. Switching shouldn't mean starting over, and one provider's outage or usage limit shouldn't stop your work. So the agent is a setting per role, and a role can list alternatives. Your roles, instructions and policy stay in your repository, and ub-agents is MIT licensed.

Agents use the logins their CLIs already have, so you can use the seats your team pays for instead of API keys. When a seat hits its usage limit, only that CLI pauses until it resets.

## Use the machines you have

Developer laptops are fast and spend much of the day waiting for their owner. We'd rather run ours overnight on the seats we have than pay a hosted agent to fix a simple bug, and they already have the toolchain: language versions, dependencies, Docker and tests.

The models run at the provider, so a launcher mostly waits on GitHub, the agent and your checks. A small server that can run your builds and tests is enough for several loops around the clock. All launchers share the GitHub queue. See [Add more launchers](/docs/guides/add-a-launcher.html).

## Every repository is different

A web app wants a browser check before merging, a library a changelog line, and a docs repository may not need a reviewer at all. So the workflow is yours: [init](/docs/commands/init.html) gives you a starter set, and you add, drop or change roles as you need.

## Your process is code

Corrections typed into a chat are gone by the next session. Agents start fresh every run, so what they need has to be written down. Role instructions live in your repository, change through pull requests, and [retros](/docs/best-practices/retrospectives.html) show where they fall short.

## People decide

Agents are good at doing the work and less good at judging what matters. Outside contributions need a maintainer's start, an agent that needs a decision stops and asks, and merges reserved for maintainers wait for them. See [Approvals](/docs/configuration/approvals.html).

## Take what fits

Start with the defaults and change what doesn't fit. If the framework gets in your way, [fork it](https://github.com/uberblick-ai/ub-agents).
