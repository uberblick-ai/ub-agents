# Motivation

ub-agents comes from a small team that builds its own software with coding agents. We wanted agents to take an issue all the way to a merged pull request while we work on something else, without building or renting a platform for it. This page explains the choices that came out of that. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## No platform to run

Running agents in a loop usually comes with a setup of its own: a server, a database, a queue, webhooks, and secrets kept in yet another place. Each one is something more to host, upgrade and debug, and one more place your work lives.

ub-agents needs a checkout, the GitHub CLI and an agent CLI. GitHub is the only state. Labels are the queue, and claims and results are comments on the issue or pull request. Stop a launcher, restart it or start it on another machine, and it picks up from GitHub. There is nothing else to back up.

## No lock-in

Agents and models change every few months, and your workflow shouldn't have to change with them. Each role names its agent and model, today Claude Code or Codex, and any other program can run as a [command agent](/docs/configuration/command-agents.html). Give a role a list and the first available one runs. When one hits its usage limit, the others keep going. The reviewer can run on a different model than the implementer.

Agents run with the logins you already have. Roles, instructions and policy are files in your repository, and ub-agents is MIT licensed. If you stop using it, you keep all of that, and the history stays on your issues and pull requests.

## Developer machines sit idle

A developer laptop is a fast machine with the whole toolchain installed, and for most of the day it waits for its owner to type. A launcher puts it to work. It runs in its own checkout next to yours, and every run gets its own worktree, so the agents stay out of your way.

That machine already runs your project: the right language versions, Docker, the database and the full test suite. Agents check their work the way you would, not in a stripped-down container you have to keep in sync.

Teammates can do the same. Every launcher takes work from the same queue on GitHub, and a claim keeps two of them off the same item. Another laptop is more capacity, with nothing to coordinate. See [Add more launchers](/docs/guides/add-a-launcher.html).

## One small server runs it around the clock

The heavy lifting happens at the model provider. A launcher watches GitHub, starts the agent and runs your checks. So a basic Hetzner box is enough to keep several loops going day and night, and laptops add capacity while they are awake. [How many launchers one account supports](/docs/best-practices/account-capacity.html) shows how far one GitHub account goes.

## Every repository is different

A web app wants a browser check before merging. A library wants a changelog line. A docs repository might not need a reviewer at all. One fixed pipeline fits none of them well.

In ub-agents a role is a few lines of YAML: the label that starts it, the agent that runs, its instructions and what each outcome does next. Add a role, drop one, rename the labels or run your own script as a step. Each repository [installs its own dependencies](/docs/configuration/checkout-setup.html) and [cleans up after its runs](/docs/configuration/cleanup.html). [init](/docs/commands/init.html) sets up a starter workflow to begin with. After that, it is yours to change.

## Your process is code

The instructions each role follows live in your repository. You change them in a pull request and review them like any other code. After a run, agents post a short [retro](/docs/best-practices/retrospectives.html) on what slowed them down, so you know which line to add or fix. The loop gets better as your instructions do.

## People decide

Agents are good at the work, not at deciding what matters. When a role reaches a decision that belongs to a person, it stops and asks on the issue instead of guessing. Merges your policy reserves for a maintainer wait for one. Only your team's input reaches the agents, and on a public repository an outside issue or pull request waits until a maintainer starts it. See [Keep people on the decisions](/docs/best-practices/people-decide.html) and [Approvals](/docs/configuration/approvals.html).

## Take what fits

This is what held up for us, not the one right way to run agents. Start with the defaults, change what doesn't fit your team, and tell us what you change on [GitHub](https://github.com/uberblick-ai/ub-agents).
