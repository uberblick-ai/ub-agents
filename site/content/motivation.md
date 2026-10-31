# Motivation

Once coding agents could write most of the code, our job changed. We were starting sessions, waiting for an agent to ask something, carrying its work to a reviewer, carrying the review back and restarting runs that died. We spent more time waiting than deciding.

So we split the work. The conversation stays with us: working out with Claude Code or Codex what to build and how. Everything after that decision, implementing, reviewing, revising and merging, goes to a loop that runs in the background on our own machines and stops when it needs one of us. For the bigger picture, see the [Uberblick motivation](https://next.uberblick.ai/docs/motivation/).

## Decide in the session, deliver in the loop

A session is the right place to think: you read the code with your agent, try ideas and settle the scope. It is the wrong place to sit through a review cycle. When the work is clear, your agent files it as an issue with a label, you start the next conversation, and you come back to a pull request or a question. See [How the loop works](/docs/how-the-loop-works.html).

## People decide

Agents are good at doing the work and less good at judging what matters. An agent that needs a decision stops and asks on the issue instead of guessing. Merges your policy reserves for a maintainer wait for one, and outside contributions need a maintainer's start. See [Approvals](/docs/configuration/approvals.html).

## No platform to run

A loop with its own server, database, queue and webhooks is one more system to host, secure and debug, and it keeps part of your work away from where your team looks. Your issues and pull requests already live on GitHub, so the loop keeps its queue and history there too, as labels and comments. A launcher needs a checkout and an agent CLI.

## Your process is code

Corrections typed into a chat are gone by the next session. Agents start fresh every run, so what they need has to be written down. Role instructions live in your repository, change through pull requests, and [retros](/docs/best-practices/retrospectives.html) show where they fall short.

## Every repository is different

A web app wants a browser check before merging, a library a changelog line, and a docs repository may not need a reviewer at all. So the workflow is yours: [init](/docs/commands/init.html) gives you a starter set, and you add, drop or change roles as you need.

## No lock-in

Agents and models change every few months. Switching shouldn't mean starting over, and one provider's outage or usage limit shouldn't stop your work. So the agent is a setting per role, and a role can list alternatives. Your roles, instructions and policy stay in your repository, and ub-agents is MIT licensed.

Agents use the logins their CLIs already have: a seat or an API key set in the launcher's environment. Check your provider's terms for unattended use. When a seat hits its usage limit, only that CLI pauses until it resets.

## Use the machines you have

Developer laptops are fast and spend much of the day waiting for their owner, and they already have the toolchain: language versions, dependencies, Docker and tests. The models run at the provider, so a launcher mostly waits on GitHub, the agent and your checks. A small server that can run your builds and tests is enough for several loops around the clock, and all launchers share one queue. See [Add more launchers](/docs/guides/add-a-launcher.html).

## Why not just write a script?

You can. Polling GitHub and starting an agent is easy. The hard parts come later: several launchers claiming the same issue, interrupted runs, review cycles that never end and knowing what happened. ub-agents handles those, plus a [terminal view](/docs/commands/launch.html) of what the loop is doing. If a short script covers your workflow, use the script.

## Take what fits

Start with the defaults and change what doesn't fit. If the framework gets in your way, [fork it](https://github.com/uberblick-ai/ub-agents).
