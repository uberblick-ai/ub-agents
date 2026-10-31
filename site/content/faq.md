# FAQ

## How can I trust the agents?

You decide how far they go. Agents never commit to your default branch: each run works in its own worktree and branch, and every change arrives as a pull request. A reviewer agent checks it, ideally on a [different model](/docs/configuration/agents.html#different-runtime-from) from the one that wrote it, and sends it back with findings until it is fit to merge. A serious finding still open after three rounds goes to a person. Your CI runs on every pull request, and [a ruleset with required checks](/docs/best-practices/launcher-account.html#limit-what-agents-can-reach) keeps anything red off your default branch.

The loop merges as quickly or as cautiously as you define. Your merge policy in `.agents/ub_agents.md` says what the integrator may merge on its own and what [waits for a maintainer](/docs/best-practices/people-decide.html#merges-that-need-a-person), such as changes to workflows, permissions or the agents' own instructions. Leave out the integrator and every merge is yours.

When an agent is unsure, it stops with `needs-human` and asks, instead of guessing. Its instructions live in your repository and change through pull requests, and its [retros](/docs/best-practices/retrospectives.html) show where they fall short. See [How the loop works](/docs/how-the-loop-works.html).

## What does it cost?

ub-agents is free and MIT licensed. Agents run with the logins your Claude Code or Codex CLI already has: a seat or an API key set in the launcher's environment. Check your provider's terms for unattended use. When a seat reaches its usage limit, only that CLI pauses until the limit resets.

## Do I need a server?

No. A launcher runs on your own machine, and the queue lives on GitHub as labels and comments. For overnight runs, a small server that can run your builds and tests is enough for several loops. To run your own Mac overnight instead, press `c` in the [terminal view](/docs/commands/launch.html) and it stays awake while the launcher runs. See [Add more launchers](/docs/guides/add-a-launcher.html).

## What can agents do on my machine?

Agents run as the launcher user, with the CLI permissions you grant, the launcher's environment and its GitHub account. A worktree does not isolate them from the rest of the machine. See [Security model](/docs/security-model.html) for what agents can reach and how to limit it.

## Is it safe on a public repository?

Outside issues, comments and pull requests reach agents only after a maintainer starts the item. Fork pull requests are reviewed as a diff and their code never runs on a launcher. See [Run the loop on a public repository](/docs/guides/public-repos.html).

## What if a run hangs or a launcher dies?

A run that goes past its [timeout](/docs/configuration/limits.html#timeout) is stopped and retried. If a launcher dies, another launcher picks up its work once the claim expires. If the last run already opened a pull request, the next run continues it instead of starting over. After repeated failures the item stops and waits for you; see [Unstick an item](/docs/guides/stuck-items.html).

## How do I stop or step in?

Add `needs-human` to an issue or pull request and the loop leaves it alone until you remove it. Agents add the same label when they need a decision, with a comment that says what to decide. See [Stop labels](/docs/configuration/stop-labels.html).

## Can the loop work with my own PRs?

Yes. Open a pull request and add `needs-review`. The loop reviews it, fixes it and merges it like one of its own. Mark it ready for review first, and link the issue with `Closes #N` or say in the description what the change should do. Review fixes are pushed to your branch, so pull before you carry on. A clear issue can skip preparation the same way, starting at `ready`.

## Which agents does it run?

Claude Code, Codex, or any command. Each role picks its own CLI and model, and can list alternatives for when one is unavailable. See [Runtimes](/docs/configuration/runtimes.html).

We plan to support more runtimes out of the box. [Open an issue](https://github.com/uberblick-ai/ub-agents/issues/new) to tell us which one you need, and we'll look into it.

## Which models should I pick?

Give the reviewer a capable model, different from the implementer's. With a strong reviewer, a cheaper implementer can often do the writing, because the review rounds catch what it misses. Whether that holds for you depends on your code base, how clean and well tested it is, how disciplined your issues and checks are, and the CLI you run. We make no general claims: try a few pairings on real issues and compare rounds, time and cost. Each role picks its own model; see [Runtimes](/docs/configuration/runtimes.html).
