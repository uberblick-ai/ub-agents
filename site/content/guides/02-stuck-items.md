# Unstick an item

Work stops for a reason, and ub-agents says which. Start with [`status`](/docs/commands/status.html), or ask about one item.

```sh
ub-agents status
ub-agents status 214
```

## In the terminal view

Stopped items are listed under **Needs attention** in `ub-agents launch`. Select one and press `4` for the Unblock tab: it shows the item's **Action needed** comment, with each decision, the options and how to resume.

Expand **To resume AGENT** for the retry steps, or **To send it back to AGENT instead** on a PR. Expand **Reasoning and evidence** for supporting details. Click a title or focus it and press Enter. The [terminal-view reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/terminal-view.md#using-the-view) covers notices and fallback displays.

## GitHub failed after a completed run

A stored report can finish after a GitHub failure without rerunning the agent. If launch stops, fix the reported cause and restart it. See [recovery rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#recovery).

## Needs a person

The item has a [stop label](/docs/configuration/stop-labels.html) such as `needs-human` and an **Action needed** comment that lists the options and how to resume. Follow the requested action; merging or closing a parked PR finishes it. To resume the same agent, follow the notice's steps to remove the stop label and apply a matching trigger. If a different role must act next, use the project's correction or handoff route.

## Failed too often

After [`max-attempts`](/docs/configuration/limits.html#attempts) failures in a row the item stops. Fix the cause, then reset the count with a reason using [`retry`](/docs/commands/retry.html).

```sh
ub-agents retry 214 --reason "Fixed the flaky fixture"
```

On an open item, retry records the reset, removes all configured stop labels
present and adds the selected agent's first trigger if none of its triggers is
present. It names the changes, or says the labels were already in place, then
says a running launcher picks it up on its next poll. A closed item gets the reset
without label changes. If changing labels fails, the reset stays recorded and
retry exits nonzero with the remaining manual label steps.

For a `max-rounds` stop, retry also resets the round count. To allow just one more
round without resetting it, remove the stop label and reapply the agent's trigger
by hand. Another limited outcome parks the item again.

## Run one item now

Evaluate a single issue or pull request under the usual rules, run it once and exit.

```sh
ub-agents launch 214
ub-agents launch 219 --agent reviewer
```

## Leftover worktrees

Restart [`launch`](/docs/commands/launch.html) to clear leftover worktrees and local run branches in the background. Check `.ub-agents/sessions/*.cleanup.log` for what was removed or kept. Removed branches with unpushed commits have an `unpushed tip SHA`; restore one with `git branch <name> <sha>`.

The [cleanup reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#stale-artifact-cleanup) explains what survives a sweep and when to restart after a crash.

## Logs

Launcher output is also appended to `.ub-agents/launch.log`. Each run keeps its own log under `.ub-agents/runs/`.

```sh
tail -f .ub-agents/launch.log
```
