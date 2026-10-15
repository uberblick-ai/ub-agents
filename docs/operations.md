# Running launchers

How a launcher reports what it does, how to stop and upgrade it, and what happens
when work fails. The [configuration reference](configuration.md) covers every key,
and the [coordination contract](coordination.md) the exact rules.

## Launch output

Interactive launches open a read-only [terminal view](terminal-view.md) of
their own session; `q` stops after the current run, while Ctrl-C stops it immediately.
Use `launch --no-ui` for plain lines. Pipes and services stay plain.

Launch output is flushed immediately to the terminal when the view is closed and appended to
`.ub-agents/launch.log` in the control checkout, with a UTC timestamp on each file
line, including stop and error messages. This also applies to `launch --once`.
Before opening the log, a launch rotates it if it is larger than a fixed 5 MB
cap: `launch.log` is renamed to `launch.log.1`, replacing the earlier archive.
The new launcher writes to a fresh log and records the rotation in one line.
Existing launchers keep their open handles and can append to the renamed log;
rotation never rewrites their lines. One archive is retained, keeping disk use
around two caps at launch starts. Follow it from another terminal with
`tail -f .ub-agents/launch.log`. See [Stopping and restarting](#stopping-and-restarting)
for signal handling, including during a GitHub request.

Discovery reports pass kind, elapsed time and request counts. Consecutive passes
with the same kind, counts and candidate count are printed once; each run also
records its launcher requests in `events.jsonl`.
See [request accounting](polling.md#accounting-and-diagnostics) for field
meanings and what is included. [Session snapshots](session-snapshots.md) support
local observation without rereading GitHub.

After execution ends, completion shares each item's history and item reads until
the next GitHub write attempt. Every durable write still checks ownership against
assignment history read after the preceding write. Renewal reads ownership
independently. Recovery shares reads after claiming its recovery lease and retains
its existing validation and replay rules.

If discovery reaches a ready plan and declines it, a line names its item, agent
and reason: the refreshed re-plan is no longer ready, the head moved, a start
gate changed, the fresh plan or runtime changed, blockers opened, the
recovery outcome no longer matches, or the claim election was lost. For example:
`#400 implementer: declined — Head moved before claim`. Existing trust, approval,
runtime and health messages still explain their waits without a second decline
line; targeted launches retain their fresh refusal message. Consecutive claiming
passes repeating the same decline for the same item and agent print it once.

When no open issue or PR has a configured trigger label, both `launch` and
`launch --once` name the labels to add. Continuous launch shows the next poll
delay rounded to whole seconds below a minute or whole minutes otherwise.
After work, claiming resumes immediately, subject to active rate-limit waits and
runtime usage pauses. Idle and observation passes may wait
longer than `poll-seconds` for request budget or low quota. Continuous launch retries
network and DNS failures, request timeouts and GitHub outages indefinitely, backing
off from 5 seconds to a 60-second cap. A failed gh call without an HTTP status is
retryable regardless of its text, except authentication required (exit 4) or a
gh binary that cannot run. HTTP 5xx and rate limits retry; other HTTP 4xx stop.
Each failed pass prints `Skipped GitHub poll: …; retrying in Ns` and the terminal
view shows the launcher waiting. These waits do not charge item attempts.
`launch --once` and `status` fail on their first error. `doctor --verbose` shows
quota and reset time, warning below 10% remaining. See [Polling and GitHub requests](polling.md)
for the exact accounting and waits.

A retryable failure after a claim write also returns to polling, including an
ownership check that cannot reach GitHub. The launcher stops its owned process
and renewal and makes no further writes for that lease. The live lease prevents
a second run until expiry; a stored outcome then finishes through recovery without
rerunning the agent. An uncertain claim POST is reconciled and withdrawn before
replanning. Authentication, other HTTP 4xx, configuration and other non-retryable
errors stop the launcher. Fatal GitHub errors say `ub-agents does not retry this
error. Fix the cause and restart ub-agents launch.` Resolve the cause and restart
to recover the lease.

## Stopping and restarting

| Signal or key | Effect | Exit |
|---|---|---|
| `q` in the view | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup. A centered shutdown screen stays visible until exit; idle launchers exit promptly. | 0 |
| `SIGTERM` (`kill -TERM <pid>`) | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup; an in-progress checkout refresh or setup finishes without a claim. When idle it exits promptly. | 0 |
| `SIGINT` (Ctrl-C) | Terminates the active agent and prints `Stopped; supervised execution terminated`, including when idle or during a GitHub request. During checkout setup, terminates setup and names its log and recovery step. After confirmed cleanup the lease is released as an operator interrupt: attempt count unchanged, eligible on the next launch without backoff. Also applies during a SIGTERM drain. | 130 |
| `SIGHUP` | Same as Ctrl-C. | 130 |
| Further `SIGTERM` | During a SIGTERM drain: no change, the drain continues. After Ctrl-C or SIGHUP: does not interrupt process-group termination, cleanup or release. | unchanged |

Discovery and idle waits (including rate-limit waits outside a run) wake on SIGTERM
and exit 0, and on Ctrl-C/SIGHUP exit 130. A rate-limit wait inside an owned run
keeps draining on SIGTERM; Ctrl-C/SIGHUP interrupt it like the run. A launcher
error keeps its nonzero exit even during a drain. See the
[polling contract](polling.md#quota-and-failure-handling) for detailed wait rules
and the [coordination contract](coordination.md#execution-boundaries) for
execution and cleanup boundaries.

The view closes and restores the terminal on launcher exit, including errors.
Ctrl-C in the view shows a centered Stopping screen through termination and cleanup,
including after `q`. Repeated `q` presses do nothing; repeated Ctrl-C presses on
the Stopping screen do nothing. Either key closes a standalone view immediately.
A view crash or kill restores the terminal and resumes plain output with one line
reporting the failure. Launcher and view reap their owned processes.

The launcher does not reload code; restart it after an upgrade or after checkout
refresh pulls code changes. An update banner above the terminal panes (or one
plain-output line) names the upgrade command for installed releases, or asks you
to restart when a normal control-checkout fetch finds newer code. See
[terminal view](terminal-view.md#using-the-view) for update-check behavior.

## Upgrading

Before upgrading, check the [changelog](../CHANGELOG.md) and
[GitHub release notes](https://github.com/uberblick-ai/ub-agents/releases) for any
required configuration edits. Send SIGTERM and wait for the launcher to exit,
upgrade with `brew update && brew upgrade ub-agents` (or `git pull` for a development checkout),
then start `ub-agents launch` again. Under tmux, systemd or similar that restarts the
launcher automatically, upgrade first and then send SIGTERM. When a release says
launchers must be upgraded together, stop every launcher for the project before
upgrading any.

## When things go wrong

Checkout refresh failures, unconfirmed run cleanup and fatal GitHub request failures
end `launch` with one `ub-agents:` line naming the problem and the next step, including
`launch --once` and `launch N`. Resolve the reported problem before launching again.
If cleanup cannot confirm termination, make `ps` usable or confirm the named process
group has exited. Retained artifacts remain subject to
[stale artifact cleanup](configuration.md#stale-artifact-cleanup). Failures in the
startup artifact sweep are logged and launch continues to poll.

After local configuration and repository validation, launch publishes its session
and opens the terminal view before the setup checks, showing `checking setup`.
In continuous launch, retryable GitHub failures during setup show `waiting` and
retry with poll backoff from 5 seconds up to 60 seconds, without a retry limit. `--no-ui` prints one
`Skipped GitHub poll: …; retrying in Ns` line per wait, with no setup activity line.
The view also opens before setup for `--once` and `launch N`, which fail on their
first error. The update watcher, startup sweep and queue begin only after setup passes. Quitting during
setup or its retry wait exits without a claim; fatal setup errors restore the
terminal before printing the error.

Before its first pass, `launch` requires a clean control checkout on the default
branch with no commits outside the local `origin` ref. `doctor` warns about the
same checkout conditions during setup; it does not fetch or fast-forward.

Before every new agent run, the launcher fetches `origin`, fast-forwards the
operator's control checkout on the repository's default branch, and rereads that
role's instruction file, shared policy and `ub-agents.yaml`. It replans the claim with the refreshed
configuration. Keep that checkout clean and free of local-only commits.
Unsafe checkout state, Git refresh failures, invalid configuration or invalid
instructions stop the launcher
with a nonzero exit and an actionable message; fix the checkout and restart.
No attempt is charged, and the assignment is not marked blocked or retrying.
Refresh happens between executions and cleanup hooks, never during a run or
durable-outcome recovery.
Projects can configure [checkout setup](configuration.md#checkout-setup) to
reinstall control-checkout dependencies after watched files change. The view and
plain output show setup running and the file that triggered it; command output is
captured in the named log. A failed exit, start failure, timeout or interruption
ends launch before a claim, without charging an attempt or marking the item blocked
or retrying. Read the log, fix the install in the named checkout, and launch again.
Setup stays pending and retries even if there is nothing left to pull, until it
succeeds. If process termination could not be confirmed, follow the message's
process-group recovery step before retrying.

Instruction text and configuration stay fixed for each run; PR candidates are not
rebased. See [Stopping and restarting](#stopping-and-restarting) for signal handling
during checkout refresh and how to restart after code updates.

Each claim has a fixed 30-minute lease, renewed every 10 minutes throughout setup,
execution, completion and cleanup. If a launcher dies, its
claims expire and another launcher recovers the work: a recorded outcome is
validated and its label transition finished without rerunning the role.
`max-attempts` limits consecutive failures per item and configured agent. Accepted
success resets the count, including outcome-only recovery and PR revisions. An
operator interrupt preserves the count and allows pickup on the next launch without
backoff. An interrupt during completion leaves the reported outcome for expiry
recovery. Agent-reported `blocked` outcomes and human-paused transitions preserve the
count and park the item. A blocked PR becomes eligible when its head differs from
the head recorded at the block, without resetting attempts or other pickup gates.
Commits the run pushed before reporting blocked do not clear its own block;
issues and unchanged PR heads still need `ub-agents retry`.
Crashes without a report, timeouts, exits without a report
(zero or nonzero), setup failures and agent-reported `retry` increment it and retry
with backoff. Invalid success reports, unconfirmed cleanup and unclassified failures
increment it and park the item. `ub-agents retry` resets
the count to 0 and clears the failure block and backoff. It prints the reset record's
link, then explains whether the item is closed, needs stop labels removed or a
trigger label added, or can be picked up by a running launcher on its next poll.
It leaves labels unchanged; `ub-agents status` shows progress and other pickup gates.
The issue and handoff PR keep separate counts; `ub-agents status` shows the
consecutive failure count in `attempts`.
Stopped rows also show their trusted Action needed notice link and waiting time.
`status --json` includes `notice_url`, `waiting_since`, `stop_labels` and
`attention_reason` on every assignment, with the same attention details as the
terminal view. Other states carry null values and an empty `stop_labels` list.
See the [status reference](https://agents.uberblick.ai/docs/commands/status.html) for
notice selection and fallback details.
A success counts only after the launcher has checked the result on GitHub; an exit
code alone never does.
A report followed by a nonzero exit is validated and applied normally after
confirmed cleanup; the launcher logs the exit code.

When a run reports a Claude or Codex usage limit, its launcher pauses new runs
on that CLI in memory until reset plus one minute, without charging item attempts.
An unusable reset uses a 15-minute pause. Other CLIs and their runtime alternatives
keep working. Each pause start or changed end time prints the CLI and UTC end time
in launch output and `.ub-agents/launch.log`. Restarting the launcher clears its
pauses. See
[runtime usage pauses](configuration.md#runtime-usage-pauses).

A stop label such as `needs-human` on the assignment or its handoff PR pauses a
transition before it starts. After removing it, set the workflow labels you want or
run `ub-agents retry`. The exact rules for claims, attempts, transitions and recovery
are in the [coordination contract](coordination.md).

At run end, the launcher removes the private worktree, fetches origin branches
and deletes the run's local branch if its tip is reachable from one of them.
Unpublished tips and fetch, reachability or deletion failures keep the branch;
the log names the branch, reason and full tip SHA. Deletion compares the tip so
a moved branch survives. Keeping a branch leaves the outcome, cleanup status and
lease unchanged. A kept worktree also keeps its branch. PR worktrees are detached.

Every `ub-agents launch` invocation sweeps private worktrees left by crashed runs
and leftover local branches once, after startup checks and before the first poll
or claim, including `--agent`, `NUMBER` and `--once`. The terminal view opens before
those setup checks. The sweep covers all roles' registered worktrees and unregistered
entries under `.ub-agents/worktrees/`, plus local
`ub-agents/<agent>/<number>/<run>` and older `ub-agent/` branches. Symlinks are removed
as links. It keeps artifacts of live leases on this host, branches heading open
PRs or checked out in remaining worktrees, and artifacts with unconfirmed cleanup
or a process group not confirmed stopped. Expired local leases with a recorded
group require its confirmed absence. Missing, unreadable, older-version or other-host
leases, ended runs without a PR, dirty trees and locked trees do not prevent removal.
The cleanup hook runs before every worktree removal with unknown context fields
set to `null`; a failed hook preserves its worktree and branch for retry. Repository
coordination scan or PR lookup failures keep affected artifacts and log the reason.
Each `removed` or `kept` line includes its reason in `.ub-agents/launch.log` and the
terminal view's Startup cleanup fold, or plain output with `--no-ui`. Sweep
failures are logged without stopping launch. Removed branches whose tips are not
reachable from fetched origin branches or merged PR heads include `unpushed tip SHA`,
also when reachability cannot be checked. Restore one with `git branch <name> <sha>`.
See [stale artifact cleanup](configuration.md#stale-artifact-cleanup) for process
uncertainty and recovery details. A crashed run's local lease remains live until
expiry, so a quick restart leaves its worktree until a later launch start. There
is no periodic sweep.

The startup sweep also prunes `.ub-agents/runs/<run>/` when its newest file,
including setup and cleanup-hook records, is more than 7 days old. It preserves
runs with live local leases, unconfirmed cleanup, process groups not confirmed
gone, unreadable or redirected diagnostics, or worktrees or branches the sweep
kept. Coordination or safety-check failures preserve the affected logs. A
symlinked entry is removed only as a link; its target is never followed.

Ended or stale [session snapshots](session-snapshots.md#publication-and-liveness)
older than 7 days are pruned with their companion cleanup logs. Old orphan
temporary files and cleanup logs are pruned when no live snapshot protects them.
Live sessions and this launcher's own files survive, and the existing cap of
20 inactive sessions still applies. Both retention periods are fixed, with no
configuration key. When anything is pruned, one summary line appears in launch
output, `launch.log` and the Startup cleanup fold, including with `--no-ui`:

```text
pruned 63 run directories and 4 session files older than 7 days (41 MB)
```

Session counts include companion logs and temporary files; the size is the total
file bytes removed. Nothing is printed for pruning when nothing was removed.
Each removal failure is logged once with its reason and launch continues.

Projects can configure a supervised [cleanup hook](configuration.md#project-cleanup-hook)
for resources associated with each private worktree. Document operator-only recovery
steps in a project operations document linked from `AGENTS.md`.

Each run also gets a private scratch directory at
`$XDG_STATE_HOME/ub-agents/<owner>/<repo>/runs/<run>/scratch`, exposed through
`UB_AGENTS_SCRATCH` and `TMPDIR`. Unset, empty or relative `XDG_STATE_HOME` falls
back to `~/.local/state`. Scratch must be outside the target checkout and its
private worktrees; a path inside the checkout fails setup before the agent starts.
Use it for temporary files; the launcher removes it and its per-run directory after
confirmed process termination while retaining run logs until the startup sweep's
7-day retention expires, or longer while a safety condition protects them.
Removal failures leave a diagnostic and any remaining scratch files without
blocking run completion. See
[runtime permissions](configuration.md#runtime-permissions)
for runtimes that need access outside their worktree.
