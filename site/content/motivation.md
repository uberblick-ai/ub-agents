# Motivation

ub-agents started because we were spending too much time managing coding agents instead of letting them work: starting sessions, waiting for an agent to reach a certain point or ask us something, passing work between implementers and reviewers, checking results, restarting failed runs.

We wanted agents to take an issue through implementation, review and integration while we worked on something else, without running another platform to coordinate them.

The first version was a small script. Most of the work since then has been dealing with what happens when things go wrong. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## No platform to run

A loop that needs its own server, database, queue and webhooks is one more system to host, secure and debug. It also keeps part of your work somewhere other than where your team already looks.

Your issues and pull requests already live on GitHub. So the loop keeps its queue and its history there too, as labels and comments, and a launcher needs little more than a checkout and an agent CLI.

## No lock-in

Agents and models change every few months. Moving your workflow to a new one shouldn't mean starting over, and one provider's outage or usage limit shouldn't stop your work.

So the agent is a setting per role, and a role can list alternatives. Your roles, instructions and policy stay in your repository, and ub-agents is MIT licensed, so they stay yours.

## Developer machines sit idle

Most developer laptops are fast machines with the whole toolchain set up, and much of the day they wait for their owner. That setup is also where your project builds and its tests run.

So a launcher runs on the machine you already have, next to your own checkout. Agents check their work with the same tools you use. A teammate who starts a launcher adds capacity to the same queue. See [Add more launchers](/docs/guides/add-a-launcher.html).

## One small server runs it around the clock

Laptops sleep, and loops are most useful when they keep going overnight. The models run at the provider, so a launcher mostly waits on GitHub, the agent and your checks. For most projects a small server, such as a basic Hetzner box that can run your builds and tests, is enough for several loops. See [How many launchers one account supports](/docs/best-practices/account-capacity.html).

## Every repository is different

A web app wants a browser check before merging, a library a changelog line, and a docs repository may not need a reviewer at all. A fixed pipeline makes you work around it.

So the workflow is yours to define. [init](/docs/commands/init.html) gives you a starter set, and you add, drop or change roles and steps as your repositories need.

## Your process is code

Corrections typed into a chat are lost by the next session. Agents start fresh every run, so what they need has to be written down. Role instructions live in your repository, change through pull requests, and [retros](/docs/best-practices/retrospectives.html) show you where they fall short.

## People decide

Agents are good at doing the work and less good at judging what matters. So the loop is built to stop and ask on the issue when a decision belongs to a person, and to leave the merges you choose to a maintainer. On a public repository, outside input waits until a maintainer starts the work. See [Approvals](/docs/configuration/approvals.html).

## Take what fits

Start with the defaults, change what doesn't fit, and tell us on [GitHub](https://github.com/uberblick-ai/ub-agents). Or fork the whole damn thing and make it yours.
