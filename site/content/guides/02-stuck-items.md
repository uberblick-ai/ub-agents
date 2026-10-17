# Unstick an item

Work stops for a reason, and ub-agents says which. Start with [`status`](/docs/commands/status.html), or ask about one item.

```sh
ub-agents status
ub-agents status 214
```

## In the terminal view

Stopped items are listed under **Needs attention** in `ub-agents launch`. Select one and press `4` for the Unblock tab: it shows the item's **Action needed** comment, with each decision, the options and how to resume.

Each item appears once, with contributing agents and states comma-separated,
followed by distinct reasons comma-separated. The count is items and the waiting
time is the longest known wait. Unblock remains available while any contributing
agent needs attention. Unblock starts with the trusted comment's short lead,
asks and distinct options, without repeating its summary or the states already
in the header. Other roles with different blockers get compact labelled sections
with their own short reasons. Issue keeps every contributing agent's full state
and reasons, and shows the cached notice or fallback resume steps below its description.

For a PR run-outcome notice, merging or closing the PR finishes the item; nothing else is needed. To return it to the same agent instead, expand **To send it back to AGENT instead**. Issue notices and notices whose item type could not be read fold their same-role steps under **To resume AGENT**. These controls contain the retry command or manual label steps, plus same-role guidance, separate from the collapsed **Reasoning and evidence** section. The reporter's requested actions stay visible above the decision options. Both controls start collapsed; click a title or focus it and press Enter to expand it. Approval-gate notices keep their visible authorization and resume steps, and older notices display as before.

If an item's comment is unavailable, Unblock labels its reason as a fallback. Blocked PRs show the completion line and fold the `ub-agents retry N --agent AGENT --reason "Human resolved the blocker"` command under **To send it back to AGENT instead**; blocked issues and unknown item types use **To resume AGENT**. A failed blocked notice post is retried on later launcher passes until the item resumes.
Merged items keep a separate retry command for each blocked agent, together in
**To resume these agents** or, for PRs, **To send it back to these agents instead**.

## GitHub failed after a completed run

A failed GitHub request while the launcher finalizes a stored report leaves the lease recoverable. Continuous launch uses its normal GitHub backoff; after lease expiry it finishes the report and its label transition without rerunning the agent. If no report was stored, the agent may run again. A non-retryable error stops the launcher; fix its cause and restart launch to recover.

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

The launcher removes each run's worktree when the run ends. A crashed run can
leave it behind. Restart [`launch`](/docs/commands/launch.html) to sweep all roles'
leftover worktrees and local run branches from this machine in the background, with the
[cleanup hook](/docs/configuration/cleanup.html) before each worktree removal.
The terminal view opens before setup checks; the sweep waits until they pass.
The sweep covers registered worktrees and unregistered entries under
`.ub-agents/worktrees/`, plus `ub-agents/<agent>/<number>/<run>` and older
`ub-agent/` local branches. Symlinks are removed as links. It keeps live leases
on this host, branches heading open PRs or checked out in remaining worktrees,
and artifacts with unconfirmed cleanup or a process group not confirmed
stopped. Expired local leases with a recorded group require its confirmed absence.
Missing, unreadable, older-version or other-host leases, ended runs without a PR,
dirty trees and locked trees do not prevent removal. A failed hook keeps its
worktree and branch for retry; unknown hook context fields are `null`. GitHub
coordination scan or PR lookup failures keep affected artifacts and log the reason.
Removed branches whose tips are not reachable from fetched origin branches or
merged PR heads include `unpushed tip SHA`, also when reachability cannot be checked.
Restore one with `git branch <name> <sha>`. Each unpushed tip is flushed to the
cleanup log before deletion. Every decision, failure and pruning summary goes only
to `.ub-agents/sessions/<session>.cleanup.log`, including with `--no-ui`. Without
a session, the sweep creates a unique `*.cleanup.log` in the same directory.
Without a writable log, nothing is removed. The view and polling start without
waiting, and sweep failures leave launch and its runs running.

A crashed run's local lease stays live until expiry, so a quick restart keeps its
worktree until a later launch start. The sweep runs once per launch invocation,
including `--agent`, `NUMBER` and `--once`; it does not run periodically.

## Logs

Launcher output is also appended to `.ub-agents/launch.log`. Each run keeps its own log under `.ub-agents/runs/`.

```sh
tail -f .ub-agents/launch.log
```
