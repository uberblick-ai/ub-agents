# How it works with Uberblick

ub-agents runs delivery: issues, pull requests, reviews and merges, all on GitHub. It doesn't hold what the product should do. We keep that in [Uberblick](https://uberblick.ai), a shared workspace of pages and decisions that people edit in the browser and agents read over MCP. Both come out of the same work, building our own software with coding agents, and both stayed separate tools. The [Uberblick motivation](https://uberblick.ai/docs/motivation) tells the whole story; this page covers the part where the two meet.

## What hurt

We started where everyone starts: Markdown files in the repository. Once the loop was merging pull requests without us, it got worse fast.

- **The files went stale.** Pull requests changed the code, and nothing in the loop changed the docs with it.
- **Agents read too much or the wrong thing.** As the folder grew, files filled every agent's context window, or the agent searched and missed the page that mattered. We caught ourselves writing index pages, a list of files with a line on each, only so an agent could decide what to open. We were building a wiki by hand, for machines.
- **Settled questions came back.** An agent that doesn't know a question was decided reopens it in code, with a fresh plan and good arguments. The rationale lived in a chat log on somebody's laptop, so we had the same discussion again every few weeks.
- **The people who decide don't work in pull requests.** A product or design change to a rule had to go through a branch and a review, and the discussion was out of sight once it merged.

Markdown in the repository is still right for how agents work: roles, instructions and policy change with the code and are reviewed with it. See [Your process is code](/docs/motivation.html#your-process-is-code). It's what the product should do that didn't fit. If your product fits in a README, keep it there.

## What we do now

The index pages are gone. Every Uberblick page has tags and a short description written for the model. An agent searches the workspace by tag, reads the descriptions and decides by itself what to read and what to skip. Uberblick is local-first: the whole workspace sits on the agent's machine, kept in sync by a [hub](https://uberblick.ai/docs/hub) when there are several. So the MCP server answers from a local copy, almost as fast as reading Markdown files, and an agent that reads a dozen pages before it writes a line doesn't wait on a dozen round trips. Decisions are records with the question, the answer, who decided and when, linked to the issue, so an agent finds them in one call instead of reopening them.

In the loop, each role uses the workspace because its instructions say so:

- **Prepare.** The issue-preparer names the areas the change touches, finds their pages by tag and links those pages and decisions instead of restating them.
- **Implement.** The implementer reads those pages first. When the change makes a page wrong or adds something new, it proposes the new text in the pull request.
- **Review.** The reviewer checks the code against the pages and reviews the proposed page text with it.
- **Merge.** After the merge, the integrator applies the page changes, so the workspace describes what shipped.

## Also, it looks good

Less serious, but it matters to us: Uberblick is nice to look at. Our [CI numbers](https://uberblick.ai/blog/2026/faster-ci-observe-it) are charts on a page instead of screenshots in a pull request. Flows are [drawn as text](https://uberblick.ai/guides/flows) that an agent writes in a few lines and people see as a diagram, and the [ub-agents-flow](https://skills.uberblick.ai/skills/ub-agents-flow) skill draws your own loop that way. A page reads like a page instead of a wall of text, and its [history](https://uberblick.ai/docs/history) shows what changed and who changed it. No more scrolling back through a chat to find what we agreed, and losing, quite literally, the Überblick. (It's German for overview. Pun intended.)

## Why they're separate

ub-agents knows nothing about Uberblick. The connection is an MCP server entry and a short rule in your [shared policy](/docs/configuration/instructions.html#shared-policy).

- **The loop stays small.** No server, account or database: the queue and history stay in GitHub labels and comments, with or without Uberblick.
- **Use either alone.** You can run the loop with product intent in a docs folder, in another tool with an MCP server, or nowhere. Uberblick is just as useful in an interactive session with no loop at all.
- **Replace either.** The rule in your policy names the server, so a different source of product intent is a config change, not a migration of your loop.
- **A failure stays on one side.** If the workspace can't be reached, agents work without the pages and the loop keeps running. If the loop stops, the pages are still there.
- **Separate releases.** Upgrading one never requires upgrading the other.

## What each gets from the other

- **The loop gets a spec.** Issues stay short because they point to pages. The implementer builds against written rules instead of guessing, the reviewer has something to check the code against, and a settled question doesn't become another stop for a person.
- **Less feature creep.** Agents like to build more than was asked for. Pages that say what the product is for, and what it deliberately isn't, give them a reason to stop: the implementer builds what the issue needs, and the reviewer can point to the page when a change grows past it.
- **The pages stay true.** Docs go stale because updating them is nobody's step. In the loop it is a step: a pull request that changes behavior carries its page updates, and the integrator applies them after the merge.
- **Retros improve both.** A [retro](/docs/best-practices/retrospectives.html) can propose a change to the loop's instructions or to a page, and a person decides each one.

To connect them, [init](/docs/commands/init.html) can register the server, and [Agents over MCP](https://uberblick.ai/docs/agents) on the Uberblick site has the rest.
