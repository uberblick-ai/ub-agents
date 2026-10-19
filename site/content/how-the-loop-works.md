# How the loop works

ub-agents passes work between agents with GitHub labels. A label says which role works on an issue or pull request next. GitHub holds all the state, so there is no server to run.

We call this loop engineering. You design the loop once: the roles, the labels and where a person steps in. Then you improve it from what the agents report in their [retros](/docs/best-practices/retrospectives.html), instead of steering each run by hand.

## Labels are the queue

Each loop checks GitHub issues and pull requests for labels. It sorts them by priority and milestone, and hands the next eligible one to its agent.

1. **Claim.** So agents never get in each other's way, each run first claims the issue or pull request with a special GitHub comment.
2. **Run.** Agents always work in worktrees and branches they create for the work. Nothing touches `main`.
3. **Report.** When the work is done, another special comment releases the claim and posts the next steps. The labels move on to the next role. Older claim and report comments are hidden as outdated, so the thread shows the latest state.
4. **Move.** Issues become pull requests, and pull requests are integrated (merged) as the gates you define allow. Some merge automatically, others wait for a maintainer's approval.

The diagrams below show the labels from the starter configuration that `ub-agents init` writes. Every label, role and outcome is a line in `ub-agents.yaml`. Rename them, drop a role or add one; the launcher follows the file. See [Add a step to the workflow](/docs/guides/add-a-step.html).

## Issues: from idea to ready

{{flow issue}}

- **Ask.** When the issue needs a decision, the preparer reports `needs-human` and asks on the issue. A person answers there, removes `needs-human` and adds a trigger again: `needs-preparation` to prepare once more, or `ready` to build it.
- **Skip.** A small issue that is already clear can go straight to `ready`.

### Complex issues

For a more complex issue, the issue-preparer can do one of these instead of reporting `prepared`:

- **Split it.** When the work doesn't fit one reviewable pull request, the preparer creates sub-issues and keeps the original open as their parent, blocked by each of them. Sub-issues start without a label, because agents never set trigger labels. Someone starts each one with `needs-preparation`, so every sub-issue goes through the whole loop, preparation included.
- **Ask for a second opinion.** The preparer hands the issue to an issue-reviewer, ideally running on another model. It approves the issue to `ready` or sends it back with findings, which the preparer applies before finishing.

{{flow issue-review-loop}}

The issue-reviewer only takes issues and the reviewer only takes pull requests, so both use `needs-review`.

## Delivery: implementer, reviewer, integrator

{{flow pr}}

- **Claims.** Each role claims the issue or pull request with a special comment before it starts, and releases it when it reports.
- **Back and forth.** Usually the only round trip is between implementer and reviewer. The integrator can also send a PR back to `needs-changes`, for example when the checks fail after it updates the base or when the PR has a merge conflict.
- **Maintainer merge.** Everything passes, but your merge policy says a person merges this PR, for example because it changes workflows, permissions or release settings. The integrator reports `maintainer-merge`, the PR gets `needs-human`, and the report names the reason. A maintainer reviews and merges it.
- **Three rounds.** The reviewer sends a PR back at most three times. Each finding is P1 (the PR must not merge as is) or P2. A P1 fix is always reviewed again. After the third round, a PR with only P2 findings left is approved and the findings are listed in the review. If a P1 finding is still open, the PR gets `needs-human`.

What may merge, and who decides, is your policy in `.agents/ub_agents.md`. See [Instructions](/docs/configuration/instructions.html).

## Outside contributors and approvals

{{flow approvals}}

The approval flow is especially useful for open source repositories, where anyone can open an issue or a pull request. So that the loop only handles authorized work, a member of your GitHub organization with the maintain or admin role must approve it. They approve by adding the trigger label, and for a pull request also with an approving review of the current head commit.

Without approvals, all labeled work is authorized, no matter who created it. That is usually enough for private repositories. Approvals are on by default for public repositories and off for private ones. See [Approvals](/docs/configuration/approvals.html).

## When work is paused

Agents pause when:

- an issue is ambiguous or contradicts itself, or contradicts your agent instructions such as `AGENTS.md` or `CLAUDE.md`
- they need more input, such as a decision on how to build a feature

The item then gets `needs-human`, or an **Action needed** comment when the role has no such outcome. The agents usually post clear, actionable recommendations, so you can work through them with your own agent and answer on GitHub. In the rare case that an agent crashes or runs out of retries, an error report is posted on GitHub as well.

[Unstick an item](/docs/guides/stuck-items.html) covers each case.
