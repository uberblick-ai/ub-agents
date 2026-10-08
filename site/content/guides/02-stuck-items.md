# Unstick an item

Work stops for a reason, and ub-agents says which. Start with [`status`](/docs/commands/status.html), or ask about one item.

```sh
ub-agents status
ub-agents status 214
```

## In the terminal view

Stopped items are listed under **Needs attention** in `ub-agents launch`. Select one and press `4` for the Unblock tab: it shows the item's **Action needed** comment, with each decision, the options and how to resume.

For a PR run-outcome notice, merging or closing the PR finishes the item; nothing else is needed. To return it to the same agent instead, expand **To send it back to AGENT instead**. The resume steps are separate from the collapsed **Reasoning and evidence** section. Issue notices keep their visible **Then resume AGENT:** steps, as do notices whose item type could not be read. Approval-gate notices keep their visible authorization and resume steps, and older notices display as before.

If a blocked item's comment is unavailable, Unblock shows its blocked reason and the same structure: PRs show the completion line and fold the `ub-agents retry N --agent AGENT --reason "Human resolved the blocker"` command under **To send it back to AGENT instead**; issues and unknown item types keep the command visible. A failed blocked notice post is retried on later launcher passes until the item resumes.

## GitHub failed after a completed run

A failed GitHub request while the launcher finalizes a stored report leaves the lease recoverable. Continuous launch uses its normal GitHub backoff; after lease expiry it finishes the report and its label transition without rerunning the agent. If no report was stored, the agent may run again. A non-retryable error stops the launcher; fix its cause and restart launch to recover.

## Needs a person

The item has a [stop label](/docs/configuration/stop-labels.html) such as `needs-human` and an **Action needed** comment that lists the options and how to resume. Follow the requested action; merging or closing a parked PR finishes it. To resume the same agent, follow the notice's steps to remove the stop label and apply a matching trigger. If a different role must act next, use the project's correction or handoff route.

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
