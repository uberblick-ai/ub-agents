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
line, including stop and error messages. This also applies to `launch --once`;
the log is never truncated or rotated. Follow it from another terminal with
`tail -f .ub-agents/launch.log`. See [Stopping and restarting](#stopping-and-restarting)
for signal handling, including during a GitHub request.

When no open issue or PR has a configured trigger label, both `launch` and
`launch --once` name the labels to add. Continuous launch shows the next poll
delay rounded to whole seconds below a minute or whole minutes otherwise.
Empty polls back off according to their REST quota cost, excluding unchanged
reads confirmed by HTTP 304 and reserving half the common account quota for busy
work when ten idle launchers share it. Low quota
adds a wait bounded by the reset. A pass that runs or recovers work returns to
normal `poll-seconds` pacing. The continuous loop retries transient GitHub
discovery failures with bounded waits.
Continuous launch waits out GitHub rate limits without charging poll failures or
item attempts. Owned runs retry rate-limited reads while the lease permits; writes
keep their existing handling. `doctor --verbose` reports request quota and reset time in UTC,
warning below 10% remaining. See [polling and retry limits](configuration.md#top-level)
for the waits and failure limit. `launch --once` and `status` fail on the first
error.

## Stopping and restarting

| Signal or key | Effect | Exit |
|---|---|---|
| `q` in the view | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup. A centered shutdown screen stays visible until exit; idle launchers exit promptly. | 0 |
| `SIGTERM` (`kill -TERM <pid>`) | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup; an in-progress checkout refresh finishes without a claim. When idle it exits promptly. | 0 |
| `SIGINT` (Ctrl-C) | Terminates the active agent and prints `Stopped; supervised execution terminated`, including when idle or during a GitHub request. After confirmed cleanup the lease is released as an operator interrupt: attempt count unchanged, eligible on the next launch without backoff. Also applies during a SIGTERM drain. | 130 |
| `SIGHUP` | Same as Ctrl-C. | 130 |
| Further `SIGTERM` | During a SIGTERM drain: no change, the drain continues. After Ctrl-C or SIGHUP: does not interrupt process-group termination, cleanup or release. | unchanged |

Discovery and idle waits (including rate-limit waits outside a run) wake on SIGTERM
and exit 0, and on Ctrl-C/SIGHUP exit 130. A rate-limit wait inside an owned run
keeps draining on SIGTERM; Ctrl-C/SIGHUP interrupt it like the run. A launcher
error keeps its nonzero exit even during a drain. See the
[configuration reference](configuration.md#top-level) for detailed wait rules
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
upgrade with `brew upgrade ub-agents` (or `git pull` for a development checkout),
then start `ub-agents launch` again. Under tmux, systemd or similar that restarts the
launcher automatically, upgrade first and then send SIGTERM. When a release says
launchers must be upgraded together, stop every launcher for the project before
upgrading any.

## When things go wrong

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

Private worktrees left by crashed runs and retained local branches can be inspected
with `ub-agents cleanup` and removed with `ub-agents cleanup --apply`. Removal needs
an eligible GitHub lease from a trusted account whose recorded host is this machine;
another or missing host, dirty trees, locked trees and uncertain artifacts stay.
Projects can configure a supervised [cleanup hook](configuration.md#project-cleanup-hook)
for resources associated with each private worktree. Document operator-only recovery
steps in a project operations document linked from `AGENTS.md`.

Each run also gets a private scratch directory at
`$XDG_STATE_HOME/ub-agents/<owner>/<repo>/runs/<run>/scratch`, exposed through
`UB_AGENTS_SCRATCH` and `TMPDIR`. Unset, empty or relative `XDG_STATE_HOME` falls
back to `~/.local/state`. Scratch must be outside the target checkout and its
private worktrees; a path inside the checkout fails setup before the agent starts.
Use it for temporary files; the launcher removes it and its per-run directory after
confirmed process termination while retaining run logs. Removal failures leave a
diagnostic and any remaining scratch files without blocking run completion. See
[runtime permissions](configuration.md#runtime-permissions)
for runtimes that need access outside their worktree.
