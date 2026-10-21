# How it works with Uberblick

[Uberblick](https://next.uberblick.ai) is a shared workspace of documents for people and agents: what the product should do, plans and the decisions behind them. Each works without the other. Together, every issue starts from the pages that describe the product, and every pull request keeps them true. This is how Uberblick itself is built.

## What changes in the loop

Your agents use Uberblick through its MCP server because their instructions say so; ub-agents itself doesn't know about it.

- **Prepare.** The issue-preparer points the issue to the pages and decisions it touches, instead of restating them.
- **Implement.** The implementer reads those pages first. When the change makes a page wrong or adds something new, it drafts the new text in the pull request.
- **Review.** The reviewer checks the code against the pages and reviews the drafted page changes with it.
- **Merge.** After the merge, the integrator applies the page changes, so the workspace describes what shipped.

## Set it up

Install Uberblick:

```sh
brew install uberblick-ai/tap/uberblick
```

Then add its MCP server to `.mcp.json` in your repository and commit it, so every launcher picks it up:

```json
{
  "mcpServers": {
    "uberblick": { "command": "ub", "args": ["mcp", "serve"] }
  }
}
```

`ub mcp install claude` writes this entry for you. [Agents over MCP](https://next.uberblick.ai/docs/agents) on the Uberblick site has the details.

## Let the agents use it

Loop runs can't answer permission prompts, so allow the Uberblick tools for each role that reads the workspace, next to its [other permissions](/docs/configuration/permissions.html):

- **Claude Code:** add `"mcp__uberblick__*"` to `--allowedTools`.
- **Codex:** add `-c, 'mcp_servers.uberblick.default_tools_approval_mode="approve"'` to `runtime-args`.

## Tell the roles what to do

Put a short rule in your [shared policy](/docs/configuration/instructions.html#shared-policy), so every role follows it:

```text
## Product pages

What the product should do lives in our Uberblick workspace. Before you change
behavior, search it for the pages the change touches and read them. Point to
pages by title in issues instead of restating them. When a pull request makes a
page wrong, put the new text under "Page updates" in its description. The
integrator applies it after the merge.
```

## More than one launcher

A local workspace lives on one computer. When the loop runs on [more launchers](/docs/guides/add-a-launcher.html), share the workspace through a [hub](https://next.uberblick.ai/docs/hub), so every agent reads the same pages. Commit `.uberblick.json`, and on each launcher machine install `ub` and sign in with `ub auth login`.

If the workspace can't be reached, agents work without the pages. To make the launcher wait instead, add a [health check](/docs/configuration/agents.html#health-check) that checks the workspace answers.
