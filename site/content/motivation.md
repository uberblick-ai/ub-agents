# Motivation

ub-agents started because we were spending too much time managing coding agents instead of letting them work: starting sessions, waiting for an agent to reach a certain point or ask us something, passing work between implementers and reviewers, checking results, restarting failed runs. We spent more time waiting than deciding.

We wanted agents to take an issue through implementation, review and integration while we worked on something else, without running another platform to coordinate them.

The first version was a small script. Most of the work since then has been dealing with what happens when things go wrong. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## Why not just write a script?

You can. Polling GitHub and starting an agent is straightforward. The harder parts are coordinating several launchers, recovering interrupted runs, limiting review cycles and keeping track of what happened. ub-agents packages those parts, so each project doesn't have to solve them again. Plus it has a nifty [terminal UI](/docs/commands/launch.html). If your workflow is simple enough for a short script, use the script.

## No platform to run

A loop that needs its own server, database, queue and webhooks is one more system to host, secure and debug. It also keeps part of your work somewhere other than where your team already looks.

Your issues and pull requests already live on GitHub. So the loop keeps its queue and its history there too, as labels and comments, and a launcher needs little more than a checkout and an agent CLI.

## No lock-in

Agents and models change every few months. Moving your workflow to a new one shouldn't mean starting over, and one provider's outage or usage limit shouldn't stop your work.

So the agent is a setting per role, and a role can list alternatives. Your roles, instructions and policy stay in your repository, and ub-agents is MIT licensed, so they stay yours.

## Developer machines sit idle

Most developer laptops are fast machines, and much of the day they wait for their owner. We'd rather leave ours running overnight than pay a hosted agent another few dollars to fix a simple bug.

Your development machine already has the toolchain: language versions, dependencies, Docker and tests. A launcher can use that environment instead of requiring another one.

Each agent works in its own worktree. Multiple machines can share the GitHub queue, with claims coordinating which launcher handles each item. See [Add more launchers](/docs/guides/add-a-launcher.html).

## One small server runs it around the clock

Laptops sleep, and loops are most useful when they keep going overnight. The models run at the provider, so a launcher mostly waits on GitHub, the agent and your checks. For most projects a small server, such as a basic Hetzner box that can run your builds and tests, is enough for several loops. See [How many launchers one account supports](/docs/best-practices/account-capacity.html).

## Every repository is different

A web app wants a browser check before merging, a library a changelog line, and a docs repository may not need a reviewer at all. A fixed pipeline makes you work around it.

So the workflow is yours to define. [init](/docs/commands/init.html) gives you a starter set, and you add, drop or change roles and steps as your repositories need.

## Your process is code

Corrections typed into a chat are lost by the next session. Agents start fresh every run, so what they need has to be written down. Role instructions live in your repository, change through pull requests, and [retros](/docs/best-practices/retrospectives.html) show you where they fall short.

## People decide

Agents are good at doing the work and less good at judging what matters. Outside contributions require authorization before entering the workflow. When an agent needs a decision, it stops and asks. Merges reserved for maintainers wait for approval. See [Approvals](/docs/configuration/approvals.html).

## Take what fits

Start with the defaults. Change what doesn't fit. If the framework gets in your way, it's MIT licensed—[fork it](https://github.com/uberblick-ai/ub-agents).
