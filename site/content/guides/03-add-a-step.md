# Add a step to the workflow

The starter workflow passes every change through four roles: issue-preparer, implementer, reviewer and integrator. Your flow may need more, such as another check or a person looking at some changes before they merge. Add those as steps of their own.

## How a step fits in

A step is one more [agent](/docs/configuration/agents.html) with its own trigger label. Give the role before it an [outcome](/docs/configuration/outcomes.html) that adds this label, and give the new agent outcomes that add the next role's label.

## Example: you look at every UI change

Say you want to see every UI change before it merges, while backend fixes keep flowing without you. So you add a visual review after the reviewer. The reviewer has read the whole diff, so it decides which approvals change the UI. Those go to the visual review, which posts screenshots and waits for you. The rest merge as before.

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
the comment above. Approve the PR and swap needs-human for ready-to-merge, or
request changes and swap it for needs-changes."
```

GitHub has no API for attaching images to comments, so the screenshots live on their own branch; keep it while you want the comments to show them. Allow the agent to start your app and a browser in its [permissions](/docs/configuration/permissions.html).

### Make sure nothing skips you

So a label alone can't skip your look, add this to the Merging section of your [shared policy](/docs/configuration/instructions.html#shared-policy), which the integrator checks before merging:

```markdown
A PR that changes the UI merges only after a maintainer's approving review of its head.
```

### Turn it on

Run `ub-agents doctor` to create `needs-visual-review` on GitHub, then merge the change. Running loops pick it up.

### Your part

A pull request with UI changes now waits for you with `needs-human`, screenshots, test steps and an **Action needed** comment. Review it on GitHub:

- **Approve** it, then swap `needs-human` for `ready-to-merge`. The integrator merges it.
- **Request changes**, then swap `needs-human` for `needs-changes`. The implementer revises it, and it comes back through both reviews.

To filter for visual reviews on GitHub, add a [stop label](/docs/configuration/stop-labels.html) of their own, such as `needs-look`, and let `posted` add it instead of `needs-human`.

## More ideas

The same pattern adds any check your team cares about, such as a security review for sign-in code or a docs check. Keep each step small, and use [agent retros](/docs/best-practices/retrospectives.html) to see whether it earns its run.
