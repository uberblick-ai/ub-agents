# How it works with Uberblick

ub-agents runs delivery: issues, pull requests, reviews and merges, all on GitHub. It doesn't hold what the product should do. Agents still need that, and a run that can't find it guesses or stops to ask. We keep it in [Uberblick](https://next.uberblick.ai), a shared workspace of pages and decisions that people edit in the browser and agents read over MCP. This is how Uberblick itself is built.

The two are separate tools. Each works without the other, and neither depends on the other's code.

## Why not Markdown files in the repository

How your agents work belongs in the repository: roles, instructions and policy change with the code, are reviewed in pull requests, and the checkout that runs is the version that applies. See [Your process is code](/docs/motivation.html#your-process-is-code).

What the product should do is different, and files in the repository fit it badly:

- **Intent changes before the code does.** A decision made today should reach every agent today, including the ones working on a branch cut last week. A file reaches a branch only when someone merges or rebases it in. Uberblick has one current copy, and every run reads it.
- **The people who decide don't all open pull requests.** A product person or a designer should be able to change a rule and discuss a sentence where it stands, without a branch and a review. The discussion of a docs pull request is out of sight once it merges.
- **A decision is more than a paragraph.** It needs the question, the answer, who decided and when, and the issue it came from, or the next session reopens it. In a file that's a convention people forget. In Uberblick a decision record has those fields, and an agent finds the records linked to an issue in one call.
- **Agents shouldn't read the whole folder.** As a docs folder grows, an agent either loads too much or searches and misses the page that matters. Uberblick names a few starting pages when an agent connects, and search finds the rest.

If your product fits in a README, keep it there. The case for Uberblick grows with the number of people, agents and branches that need the same answer.

## An example

A team member labels an issue: "Let users retry a failed export". Here is what the loop does with the workspace:

```text
issue-preparer  Finds "Exports: rules" and the decision "Retry limit on exports"
                (two retries, decided 14 May). Links both in the issue
                instead of restating them.

implementer     Reads both pages, adds the retry button and hides it after
                two retries. The rules page doesn't say what users see then,
                so the pull request proposes the text under "Page updates":
                "After two failed retries, the export shows Contact support."

reviewer        Checks the code against the rule and reviews the page text
                with it.

integrator      Merges, then applies the page update.
```

Nobody reopened the retry limit, and the page now describes what shipped.

## Why they're separate

ub-agents knows nothing about Uberblick. The connection is two things in your repository: an MCP server entry and a short rule in your [shared policy](/docs/configuration/instructions.html#shared-policy). Your agents use the workspace because their instructions say so.

- **The loop stays small.** No server, account or database: the queue and history stay in GitHub labels and comments, with or without Uberblick.
- **Use either alone.** You can run the loop with product intent in a docs folder, in another tool with an MCP server, or nowhere. Uberblick is just as useful in an interactive session with no loop at all.
- **Replace either.** The rule in your policy names the server, so a different source of product intent is a config change, not a migration of your loop.
- **A failure stays on one side.** If the workspace can't be reached, agents work without the pages and the loop keeps running. If the loop stops, the pages are still there.
- **Separate releases.** Upgrading one never requires upgrading the other.

## What each gets from the other

- **The loop gets a spec.** Issues stay short because they point to pages. The implementer builds against written rules instead of guessing, the reviewer has something to check the code against, and a question that was settled doesn't become another stop for a person.
- **The pages stay true.** Docs go stale because updating them is nobody's step. In the loop it is a step: a pull request that changes behavior carries its page updates, and the integrator applies them after the merge.
- **Retros improve both.** A [retro](/docs/best-practices/retrospectives.html) can propose a change to the loop's instructions or to a page, and a person decides each one.

## Set it up

Install Uberblick on each launcher machine:

```sh
brew install uberblick-ai/tap/uberblick
```

Agent worktrees start at new paths, so the server has to be configured in files the loop's agents read, not only in your interactive setup.

For **Claude Code**, run `ub mcp install claude` to add the server to `.mcp.json`, and commit it with a `.claude/settings.json` that approves it for headless runs:

```json
{
  "enabledMcpjsonServers": ["uberblick"]
}
```

For **Codex**, define the server in `runtime-args`, since a new worktree doesn't read a project `.codex/config.toml`. Merge these entries into the arguments your roles already use:

```yaml
runtime-args:
  codex:
    - -c
    - 'mcp_servers.uberblick.command="ub"'
    - -c
    - 'mcp_servers.uberblick.args=["mcp", "serve"]'
    - -c
    - 'mcp_servers.uberblick.default_tools_approval_mode="approve"'
```

Loop runs can't answer permission prompts. For Claude Code, add `"mcp__uberblick__*"` to `--allowedTools` for each role that reads the workspace, next to its [other permissions](/docs/configuration/permissions.html). The Codex entries above already approve the tools.

Then add the rule to your shared policy:

```text
## Product pages

What the product should do lives in our Uberblick workspace. Before you change
behavior, search it for the pages the change touches and read them. Point to
pages by title in issues instead of restating them. When a pull request makes a
page wrong, put the new text under "Page updates" in its description. The
integrator applies it after the merge.
```

Run `ub-agents doctor` on each launcher. It checks that every configured runtime loads the server and lists its tools. [Agents over MCP](https://next.uberblick.ai/docs/agents) on the Uberblick site has the rest of the configuration.

## More than one launcher

A local workspace lives on one computer. When the loop runs on [more launchers](/docs/guides/add-a-launcher.html), share the workspace through a [hub](https://next.uberblick.ai/docs/hub), commit `.uberblick.json`, and sign in on each launcher machine with `ub auth login`. To make a launcher wait while the workspace is down instead of working without it, add a [health check](/docs/configuration/agents.html#health-check).
