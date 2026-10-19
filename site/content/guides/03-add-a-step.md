# Add a step to the workflow

The starter workflow passes every change through four roles: issue-preparer, implementer, reviewer and integrator. It's a solid minimal default: each change is prepared, built, reviewed by a second agent and merged under your policy. Your flow may need more, such as another check or a person looking at some changes before they merge. Add those as steps of their own.

## How a step fits in

A step is one more [agent](/docs/configuration/agents.html) with its own trigger label. Give the role before it an [outcome](/docs/configuration/outcomes.html) that adds this label, and give the new agent outcomes that add the label the next role waits for. The launcher follows the file; nothing else changes.

## Example: you look at every UI change

Say you want to see every relevant UI change yourself before it merges, while backend fixes and refactors keep flowing without you. Agents check code and tests well, but whether a screen looks right is your call.

So you add a visual review after the reviewer. The reviewer has read the whole diff, so it decides which approvals change the UI. Those go to the visual review, which posts screenshots and waits for you. The rest merge as before.

{{flow visual-review}}

### Give the reviewer a new outcome

In `ub-agents.yaml`, add `ui-changed` next to `approved`:

```yaml
reviewer:
  outcomes:
    approved: {add: [ready-to-merge]}
    ui-changed: {add: [needs-visual-review]}
    changes-requested: {add: [needs-changes], max-rounds: 3}
```

Then say when to use it in `.agents/reviewer.md`:

```markdown
When you approve a PR with a visible change worth a look, such as a new screen,
layout or component, report ui-changed instead of approved. Minor changes, such
as a typo fix or a spacing tweak, don't count.
```

### Add the agent

The new agent picks up `needs-visual-review` and parks the pull request for you:

```yaml
visual-review:
  runtime: "claude:claude-opus-5-5:high"
  kind: pr
  trigger: needs-visual-review
  instructions: .agents/visual-review.md
  worktree: true
  outcomes:
    posted: {add: [needs-human]}
```

### Tell it what you want to see

In `.agents/visual-review.md`, say what you need to judge the change, here screenshots and the steps to try it yourself:

```markdown
# Visual review

Start the app at the PR head and take a screenshot of each changed screen, in
each state that matters. Commit them to the ui-screenshots branch under pr-N/
and push it; create the branch without history if it doesn't exist.
Then comment on the PR with:

- what changed and what to look out for, with each screenshot embedded as
  ![Name](https://github.com/OWNER/REPO/blob/ui-screenshots/pr-N/NAME.png?raw=true)
- how to try it yourself: `gh pr checkout N`, the commands that start the app,
  and where to click to reach each screen

Report posted with this action: "Maintainer: check the screenshots and steps in
the comment above, then approve the PR or request changes."
```

GitHub has no API for attaching images to comments, so the screenshots live on their own branch. Keep it as long as you want the comments to show them. Allow the agent to start your app and a browser in its [permissions](/docs/configuration/permissions.html).

### Make sure nothing skips you

So a label alone can't skip your look, add this to the Merging section of your [shared policy](/docs/configuration/instructions.html#shared-policy). The integrator checks it before merging.

```markdown
A PR that changes the UI merges only after a maintainer's approving review of its head.
```

### Turn it on

Run `ub-agents doctor`. It offers to create `needs-visual-review` on GitHub. Merge the change, and the loops pick it up automatically.

### Your part

A pull request with UI changes now waits for you with `needs-human`, the agent's screenshots and test steps, and an **Action needed** comment. Review it on GitHub as usual:

- **Approve** it, then swap `needs-human` for `ready-to-merge`. The integrator merges it.
- **Request changes**, then swap `needs-human` for `needs-changes`. The implementer revises the pull request from your review, and it comes back through review and visual review.

To filter for visual reviews on GitHub, add a [stop label](/docs/configuration/stop-labels.html) of their own, such as `needs-look`, and let `posted` add it instead of `needs-human`.

## More ideas

The same pattern adds any check your team cares about, such as a security review for sign-in code or a docs check before merging. Keep each step and its instructions small, and use [agent retros](/docs/best-practices/retrospectives.html) to see whether it earns its run.
