# How it works with Uberblick

[Uberblick](https://uberblick.ai) is a shared workspace of documents for people and agents: what the product should do, plans and the decisions behind them. ub-agents runs the loop on GitHub. Each works without the other. Together, every issue starts from the pages that describe the product, and every pull request keeps them true. This is how Uberblick itself is built.

## What changes in the loop

ub-agents doesn't know about Uberblick. Your agents use it because their instructions say so, through the Uberblick MCP server.

- **Prepare.** The issue-preparer reads the pages and decisions the issue touches and points to them in the issue, instead of restating them. The page stays the one place to be right.
- **Implement.** The implementer reads those pages before it writes code. When the change makes a page wrong, or adds something no page describes yet, it drafts the new text in the pull request.
- **Review.** The reviewer checks the pull request against the pages, not only against the issue text, and reviews the drafted page changes with the code.
- **Merge.** After the merge, the integrator applies the drafted page changes. The workspace describes the product as shipped, never code that hasn't merged.

## Set it up

Install Uberblick and connect your agent to it, in your project's checkout:

```sh
brew install uberblick-ai/tap/uberblick
ub mcp install claude   # or: ub mcp install codex
```

Commit the file it writes, so every launcher picks it up. [Agents over MCP](https://next.uberblick.ai/docs/agents) on the Uberblick site has the details.

## Let the agents use it

Loop runs can't answer permission prompts. Add `"mcp__uberblick__*"` to `--allowedTools` for each role that reads the workspace, next to its [other permissions](/docs/configuration/permissions.html#claude-code).

## Tell the roles what to do

Put the rule in your [shared policy](/docs/configuration/instructions.html#shared-policy), so every role follows it. Keep it short, for example:

```text
## Product pages

What the product should do lives in our Uberblick workspace. Before you change
behavior, search it for the pages the change touches and read them. Point to
pages by title in issues instead of restating them. When a pull request makes a
page wrong, put the new text under "Page updates" in its description. The
integrator applies it after the merge.
```

Adjust the role files from there. [Treat instructions as code](/docs/best-practices/instructions-as-code.html) has more on what belongs where.

## More than one launcher

A local workspace lives on one computer. When the loop runs on [more launchers](/docs/guides/add-a-launcher.html), share the workspace through a [hub](https://github.com/uberblick-ai/uberblick-2/blob/main/REMOTE.md), so every agent reads the same pages. Commit `.uberblick.json`, and on each launcher machine install `ub` and sign in with `ub auth login`.

If agents start while the workspace can't be reached, they work without the pages. A [health check](/docs/configuration/agents.html#health-check), a command of yours that checks the workspace answers, makes the launcher wait instead, and it picks up work again once the check passes.
