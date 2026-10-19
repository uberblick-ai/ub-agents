# Add a step to the workflow

The starter workflow passes every change through four roles: the issue-preparer, the implementer, the reviewer and the integrator. That is a solid minimal default. Each change is prepared, built, reviewed by a second agent and merged under your policy. It may not fit your flow, though. You may want another check, or a person to look at some changes before they merge. Add those as steps of their own.

## How a step fits in

A step is one more [agent](/docs/configuration/agents.html) with its own trigger label. To put it between two roles, point the earlier role's [outcome](/docs/configuration/outcomes.html) at the new label, and give the new agent outcomes that add the label the next role waits for. The launcher follows the file, so nothing else changes.

## Example: you look at every UI change

Say you want to see every relevant UI change yourself before it merges. Agents check code and tests well, but whether a screen looks right is your call. Everything else, such as backend fixes and refactors, should keep flowing without you.

So you add a visual review between the reviewer and the integrator. It lets pull requests without UI changes pass straight on, and stops the others for you.

{{flow visual-review}}

### Send approved pull requests to the new step

Today the reviewer's approval adds `ready-to-merge`, and the integrator merges. In `ub-agents.yaml`, let it add `needs-visual-review` instead:

```yaml
reviewer:
  outcomes:
    approved: {add: [needs-visual-review]}
    changes-requested: {add: [needs-changes], max-rounds: 3}
```

### Add the agent

The new agent picks up `needs-visual-review`. It either passes the pull request on to the integrator, or stops it for you with `needs-human`:

```yaml
visual-review:
  runtime: "claude:claude-opus-5-5:high"
  kind: pr
  trigger: needs-visual-review
  instructions: .agents/visual-review.md
  worktree: true
  outcomes:
    no-ui-change: {add: [ready-to-merge]}
    ui-changed: {add: [needs-human]}
```

### Tell it what you want to see

In `.agents/visual-review.md`, say which changes you care about and what you need in order to judge them:

```markdown
# Visual review

Decide whether this pull request changes what users see: pages, components,
styles, copy or images. Tests and refactors that leave the UI as it was don't count.

If nothing visible changed, report no-ui-change.

Otherwise, start the app at the PR head and open each changed screen. Comment on
the PR with what changed, where to find it and what to check, with a preview link
if the project has one. Then report ui-changed with this action:
"Maintainer: check the screens listed above, then approve the PR and add
ready-to-merge, or add needs-changes with what to fix."
```

If a path rule is enough, for example any change under `web/`, a [command agent](/docs/configuration/command-agents.html) can make the call with a short script instead of a model.

### Make sure nothing skips you

So that a label alone can't skip your look, add a line to the Merging section of your [shared policy](/docs/configuration/instructions.html#shared-policy). The integrator checks it before merging.

```markdown
A PR that changes the UI merges only after a maintainer's approving review of its head.
```

### Turn it on

Run `ub-agents doctor`. It offers to create `needs-visual-review` on GitHub. Merge the change, and the next run uses it.

### Your part

A pull request with UI changes now waits for you with `needs-human` and an **Action needed** comment that points to the screens. Once you've looked, either:

- approve the pull request on GitHub, remove `needs-human` and add `ready-to-merge`, or
- comment what to fix, remove `needs-human` and add `needs-changes`. The implementer revises the pull request, and it comes back through review and visual review.

To find your visual reviews quickly on GitHub, give them their own [stop label](/docs/configuration/stop-labels.html), such as `needs-look`, and let `ui-changed` add that instead.

## More ideas

The same pattern adds any check your team cares about, such as a security review for changes to sign-in code, or a docs check before merging. Keep each step small and its instructions short, and use [retrospectives](/docs/best-practices/retrospectives.html) to see whether it earns its run.
