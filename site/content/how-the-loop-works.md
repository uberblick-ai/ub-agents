# How the loop works

ub-agents passes work between agents with GitHub labels. A label says which role works on an issue or pull request next. GitHub holds all the state, so there is no server to run.

We call this loop engineering. You design the loop once: the roles, the labels and where a person steps in. Then you improve it from the agents' [retros](/docs/best-practices/retrospectives.html) instead of steering each run by hand.

## Labels are the queue

Each launcher looks for labeled issues and pull requests, sorts them by priority and milestone, and hands the next eligible one to its agent.

1. **Claim.** The run claims the item with a comment, so no other agent picks it up.
2. **Run.** The agent works in its own worktree and branch. Nothing touches `main`.
3. **Report.** A second comment releases the claim and posts the next steps, and the labels move on to the next role. Older claim and report comments are hidden, so the thread shows the latest state.
4. **Move.** Issues become pull requests, and pull requests merge as your gates allow, some automatically, others after a maintainer approves.

The diagrams show the starter labels that `ub-agents init` writes. Every label, role and outcome is a line in `ub-agents.yaml`; change it and the launcher follows. See [Add a step to the workflow](/docs/guides/add-a-step.html).

## Issues: from idea to ready

{{flow issue}}

- **Ask.** When the issue needs a decision, the preparer reports `needs-human` and asks on the issue. Answer there, remove `needs-human` and add `needs-preparation` to prepare again, or `ready` to build it.
- **Skip.** A small, clear issue can go straight to `ready`.

### Complex issues

Instead of reporting `prepared`, the issue-preparer can:

- **Split it.** When the work doesn't fit one reviewable pull request, the preparer creates sub-issues and keeps the original open, blocked by each of them. Agents never set trigger labels, so someone starts each sub-issue with `needs-preparation`.
- **Ask for a second opinion.** An issue-reviewer, ideally on another model, approves the issue to `ready` or sends it back with findings for the preparer to apply.

{{flow issue-review-loop}}

Both reviewers use `needs-review`: the issue-reviewer takes only issues, the reviewer only pull requests.

## Delivery: implementer, reviewer, integrator

{{flow pr}}

- **Merge checks.** The integrator runs the project's declared checks and waits for CI required by your merge policy to pass on the exact head it merges, including its own base merge. Pending checks, even a just-pushed head with none reported yet, mean wait; a policy requiring no CI has nothing to wait for. Immediately before merging, it checks the item's labels and reports blocked if a configured [stop label](/docs/configuration/stop-labels.html) is present. It rechecks `UNKNOWN` mergeability and, if the merge command fails, checks whether the PR actually merged before reporting.
- **Back and forth.** The usual round trip is between implementer and reviewer. The integrator also sends a PR back to `needs-changes`, for example when checks fail after it updates the base or the PR has a merge conflict.
- **Maintainer merge.** When your merge policy says a person merges this PR, for example because it changes workflows, permissions or release settings, the integrator reports `maintainer-merge` with the reason and the PR gets `needs-human`.
- **Three rounds.** Each review finding is P1 (must not merge as is) or P2. The reviewer sends a PR back at most three times, and always reviews a P1 fix again. After the third round, a PR with only P2 findings left is approved with them listed; an open P1 gets `needs-human`.

What may merge, and who decides, is your policy in `.agents/ub_agents.md`. See [Instructions](/docs/configuration/instructions.html).

## Outside contributors and approvals

{{flow approvals}}

On open source repositories anyone can open an issue or a pull request. With approvals on, someone with the maintain or admin role starts the work by adding the trigger label, and approves a pull request's current head with a review. Without approvals, any labeled work runs, which is usually enough for private repositories. Approvals are on by default for public repositories. See [Approvals](/docs/configuration/approvals.html).

## When work is paused

Agents pause when an issue is ambiguous, contradicts itself or your instructions (such as `AGENTS.md`), or needs a decision. The item gets `needs-human`, or an **Action needed** comment when the role has no such outcome, with concrete recommendations you can answer on GitHub. A crash or exhausted retries also posts an error report. [Unstick an item](/docs/guides/stuck-items.html) covers each case.
