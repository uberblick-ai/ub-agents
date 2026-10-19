# Motivation

ub-agents comes from a small team that builds its own software with coding agents. We wanted agents to take an issue to a merged pull request while we work on something else, without running a platform for it. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## No platform to run

Agent loops usually bring their own setup: a server, a database, a queue, webhooks and secrets in yet another place. All of it needs hosting and upkeep, and it holds part of your work.

ub-agents needs a checkout, the GitHub CLI and an agent CLI. GitHub is the only state: labels are the queue, and claims and results are comments on the issue or pull request. Restart a launcher, or start one on another machine, and it picks up from there.

## No lock-in

Agents and models change every few months; your workflow shouldn't have to. Each role names its agent and model, today Claude Code or Codex, or runs any program as a [command agent](/docs/configuration/command-agents.html). Give a role a list and the first available one runs, so a usage limit on one CLI doesn't stop the loop.

Roles, instructions and policy are files in your repository, and ub-agents is MIT licensed. If you stop using it, you keep all of it.

## Developer machines sit idle

A developer laptop is a fast machine with your whole toolchain, and most of the day it waits for its owner to type. A launcher puts it to work in its own checkout, and each run works in its own worktree. Agents check their work with your real language versions, Docker and test suite, not a stripped-down container.

Teammates can do the same. Every launcher takes work from one queue on GitHub, and a claim keeps two of them off the same item. See [Add more launchers](/docs/guides/add-a-launcher.html).

## One small server runs it around the clock

The heavy lifting happens at the model provider. A launcher only watches GitHub, starts agents and runs your checks, so a basic Hetzner box that can run your builds and tests keeps several loops going day and night. See [How many launchers one account supports](/docs/best-practices/account-capacity.html).

## Every repository is different

A web app wants a browser check before merging, a library a changelog line, and a docs repository may not need a reviewer at all.

A role is a few lines of YAML. Choose your roles, labels, instructions and outcomes, or run your own script as a role. Each repository can set its own [dependency install](/docs/configuration/checkout-setup.html) and [cleanup](/docs/configuration/cleanup.html). [init](/docs/commands/init.html) gives you a starter workflow; after that, it's yours.

## Your process is code

You change role instructions in a pull request and review them like code. When a run loses time, the agent posts a short [retro](/docs/best-practices/retrospectives.html) on what slowed it down, so you know which line to fix.

## People decide

When a role reaches a decision your instructions leave to a person, it stops and asks on the issue instead of guessing. Merges your policy reserves for a maintainer wait for one. Comments from outside your team never reach the agents, and on a public repository an outside issue or pull request waits until a maintainer starts it. See [Approvals](/docs/configuration/approvals.html).

## Take what fits

Start with the defaults, change what doesn't fit, and tell us on [GitHub](https://github.com/uberblick-ai/ub-agents). Or fork the whole damn thing and make it yours.
