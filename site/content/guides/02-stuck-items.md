# Unstick an item

Work stops for a reason, and ub-agents says which. Start with [`status`](/docs/commands/status.html), or ask about one item.

```sh
ub-agents status
ub-agents status 214
```

## In the terminal view

Stopped items are listed under **Needs attention** in `ub-agents launch`. Select one and press `4` for the Unblock tab: the item's **Action needed** comment, with each decision, the options and how to resume. Expand **To resume AGENT** for the retry steps, or **Reasoning and evidence** for the details. A dim hint below the source note links to the [ub-agents-unblock skill](https://skills.uberblick.ai/skills/ub-agents-unblock) to decide interactively in Claude Code or Codex.

## GitHub failed after a completed run

A finished run's report is kept, so it completes after a GitHub failure without rerunning the agent. If launch stops, fix the reported cause and restart it. See [recovery rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#recovery).

## Needs a person

The item has a [stop label](/docs/configuration/stop-labels.html) such as `needs-human` and an **Action needed** comment with the options and how to resume. Do what it asks; merging or closing a parked PR finishes it. To resume, remove the stop label and add the trigger of the role that should act next.

## Failed too often

After [`max-attempts`](/docs/configuration/limits.html#attempts) failures in a row the item stops. Fix the cause, then reset the count with a reason using [`retry`](/docs/commands/retry.html).

```sh
ub-agents retry 214 --reason "Fixed the flaky fixture"
```

Retry also removes stop labels and adds the agent's trigger, so a running launcher picks the item up on its next poll. For a `max-rounds` stop it resets the round count too; to allow just one more round, swap the labels by hand instead.

## Run one item now

Evaluate a single issue or pull request under the usual rules, run it once and exit.

```sh
ub-agents launch 214
ub-agents launch 219 --agent reviewer
```

If a launcher already runs on this control checkout, the numbered launch exits
with its pid, host and start time. Stop it first, or use
`ub-agents retry NUMBER --reason "Blocker resolved"` to hand the item to that launcher on its next poll.
[`status`](/docs/commands/status.html) and [`doctor`](/docs/commands/doctor.html)
show which launcher holds the checkout. A separate control checkout can run its
own launcher.

## Leftover worktrees

Restart [`launch`](/docs/commands/launch.html) to clear leftover worktrees and run branches. `.ub-agents/sessions/*.cleanup.log` lists what was removed or kept, with an `unpushed tip SHA` for removed branches that had unpushed commits; restore one with `git branch <name> <sha>`.

## Logs

Launcher output also goes to `.ub-agents/launch.log`, and each run keeps its own log under `.ub-agents/runs/`.

```sh
tail -f .ub-agents/launch.log
```
