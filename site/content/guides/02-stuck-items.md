# Unstick an item

Work stops for a reason, and ub-agents says which. Start with [`status`](/docs/commands/status.html), or ask about one item.

```sh
ub-agents status
ub-agents status 214
```

## In the terminal view

Stopped items are listed under **Needs attention** in `ub-agents launch`. Select one and press `4` for the Unblock tab: it shows the item's **Action needed** comment, with each decision, the options and how to resume.

## Needs a person

The item has a [stop label](/docs/configuration/stop-labels.html) such as `needs-human` and an **Action needed** comment that lists the options and how to resume. Decide, remove the stop label, and add the workflow label you want.

## Failed too often

After [`max-attempts`](/docs/configuration/limits.html#attempts) failures in a row the item stops. Fix the cause, then reset the count with a reason using [`retry`](/docs/commands/retry.html).

```sh
ub-agents retry 214 --reason "Fixed the flaky fixture"
```

## Run one item now

Evaluate a single issue or pull request under the usual rules, run it once and exit.

```sh
ub-agents launch 214
ub-agents launch 219 --agent reviewer
```

## Leftover worktrees

The launcher removes each run's worktree when the run ends. A crashed run can leave it behind. Preview, then remove what is safe to remove with [`cleanup`](/docs/commands/cleanup.html).

```sh
ub-agents cleanup
ub-agents cleanup --apply
```

## Logs

Launcher output is also appended to `.ub-agents/launch.log`. Each run keeps its own log under `.ub-agents/runs/`.

```sh
tail -f .ub-agents/launch.log
```
