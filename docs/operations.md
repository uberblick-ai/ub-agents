# Running launchers

What a launcher prints, how to stop it, and what to do when something fails. The
[configuration reference](configuration.md) lists every key, and the
[coordination contract](coordination.md) has the exact rules for claims, attempts and
recovery.

## Launch output

In a terminal, `launch` opens a read-only [terminal view](terminal-view.md) of its own
session. With `--no-ui`, or under a pipe or service, it prints plain lines.

Launch output also goes to `.ub-agents/launch.log` in the control checkout, with a
UTC timestamp on each line. A launch first rotates the log when it is larger than
5 MB: `launch.log` becomes `launch.log.1`, replacing the previous archive. Follow it
with `tail -f .ub-agents/launch.log`.

Discovery lines report the pass kind, elapsed time and request counts; identical
consecutive passes print once. Each run also records its launcher requests in
`events.jsonl`. See [request accounting](polling.md#accounting-and-diagnostics) for
the fields. When discovery declines a ready item, one line names the item, agent and
reason, for example `#400 implementer: declined — Head moved before claim`.

## Stopping and restarting

To stop one active agent from GitHub, add a configured stop label such as
`needs-human` to its issue or PR, close the issue or PR, or merge the PR. Closing or
merging stops execution even when no stop labels are configured. The launcher
terminates execution at the next lease renewal check (10 minutes by default),
discards any early report and leaves the labels unchanged. No failed attempt is
charged. A label removed or an item reopened before the check has no effect.
A closed item needs no further action. Resume a stopped open item with
`ub-agents retry NUMBER --agent ROLE --reason "Ready to resume"`. See
[stop labels](https://agents.uberblick.ai/docs/configuration/stop-labels.html).

Every launch mode holds one exclusive lock for its resolved control checkout.
A second launch exits before startup and names the running launcher's pid, host
and start time. Stop that launcher first, or use
`ub-agents retry NUMBER --reason "Blocker resolved"` to hand it an item.
Separate control checkouts can each launch.

`ub-agents status` and `ub-agents doctor` (also `--offline`) show the running
launcher or that none is running. Both JSON outputs add `launcher`: an object
with `pid`, `host` and UTC `started_at`, or `null`. Doctor treats this as information,
without adding a warning or failure. The lock is released as soon as the launcher
exits, including on SIGKILL, and is not retained by its agents or helpers. A leftover
owner record never blocks a restart; do not delete the lock files while launching.

| Signal or key | Effect | Exit |
|---|---|---|
| `q` in the view | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup. | 0 |
| `SIGTERM` (`kill -TERM <pid>`) | Same as `q`. A checkout refresh or setup in progress also finishes, without a claim. | 0 |
| `SIGINT` (Ctrl-C) | Terminates the active agent or checkout setup, cleans up, and releases the claim as an operator interrupt: no attempt charged, eligible on the next launch without backoff. Also works during a `SIGTERM` drain. | 130 |
| `SIGHUP` | Same as Ctrl-C. | 130 |
| Further `SIGTERM` | Does not interrupt a drain or a Ctrl-C cleanup. | unchanged |

An idle launcher, including one waiting for a rate limit outside a run, exits promptly. A launcher
error keeps its nonzero exit even during a drain. The
[terminal view](terminal-view.md) describes the shutdown screens.

The launcher does not reload its own code. Restart it after an upgrade, or when the
update banner says a checkout refresh pulled newer code. To upgrade, follow
[Upgrading](https://agents.uberblick.ai/docs/upgrading.html). Stop every launcher of
the project first only when the release notes say launchers must upgrade together.

## When things go wrong

Most failures end `launch` with one `ub-agents:` line that names the problem and the
next step. Fix the cause and launch again. For one stuck issue or PR, see
[Unstick an item](https://agents.uberblick.ai/docs/guides/stuck-items.html).

**Launch refuses to start because of the control checkout.** It must be clean, on
the repository's default branch and have no commits missing from `origin`. Commit,
stash or remove local changes, switch to the default branch, and push or drop local
commits. If the `origin` ref is missing, run `git fetch origin`. `doctor` reports the
same conditions as warnings.

**Launch refuses to start because a label is missing.** A trigger or transition label
does not exist on GitHub. Run `ub-agents doctor` to create it. Missing stop labels do
not block launch.

**Launch stops before a run.** Before each new run, the launcher fetches `origin`,
fast-forwards the control checkout and rereads the configuration and instructions.
An unsafe checkout or Git failure stops it. Fix the checkout and restart.
Startup validation, `launch --once` and `launch N` also stop on invalid configuration
or unreadable instructions, with the same errors as `ub-agents doctor --offline`.
No attempt is charged. See
[execution boundaries](coordination.md#execution-boundaries).

**Claims paused for invalid configuration.** Continuous launch, including `--agent`,
stays running when configuration or instructions fail validation after a refresh.
It claims no new runs or recoveries and charges no attempt. The terminal banner,
plain output, `launch.log` and `status` show the error, checkout commit and next step.
Correct the configuration or instruction files; for unknown keys or values, upgrading
ub-agents is another fix. Every poll, and `r`, fetches, fast-forwards and rereads even
with an empty queue. Valid files resume claims automatically without a restart;
a restarted launcher validates at startup as usual. `q` and `SIGTERM` stop promptly
while waiting. The log records the pause starting, its error or commit changing,
and claims resuming, rather than repeating the error every poll.

**Control-checkout setup fails.** Read the log the message names, fix the install in
the control checkout and launch again. Setup runs on every launch until it succeeds.
See [checkout setup](configuration.md#checkout-setup).

**Setup fails in a private worktree.** The run ends before the agent starts and
counts as a failed attempt. Fix the hook or environment. If the item has used all its
attempts, run `ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"`.

**GitHub is slow, unreachable or rate limited.** Continuous launch waits and retries
without a limit, printing `Skipped GitHub poll: …; retrying in Ns`. No attempt is
charged and no action is needed. `launch --once`, `launch N` and `status` fail on the
first error; run them again later. `doctor --verbose` shows the remaining quota. See
[quota and failure handling](polling.md#quota-and-failure-handling).

**Launch stops with "ub-agents does not retry this error."** Authentication failures,
other HTTP 4xx errors are not retried. Fix the cause, for
example with `gh auth login`, and restart. The restarted launcher recovers any claim
it left open.

**A runtime reached its usage limit.** The launcher pauses that CLI until the limit
resets, and other CLIs keep working. No attempt is charged. Restart the launcher to
clear the pause early. See [runtime usage pauses](configuration.md#runtime-usage-pauses).

**A health check fails.** The agent's items show `waiting` with the check's error.
Fix the dependency; claiming resumes on its own. See
[project health checks](configuration.md#project-health-checks).

**A runtime update broke a CLI.** The launcher marks the CLI unavailable and uses an
alternative runtime if one is configured. While `gh` is unavailable, no run starts.
Repair or reinstall the CLI; a later check between runs finds it working and resumes.
See [daily runtime maintenance](configuration.md#daily-runtime-maintenance).

**A launcher died during a run.** Its claim expires when its lease runs out, 30
minutes after the last renewal by default. Then any
launcher recovers the work, and a recorded outcome finishes without rerunning the
agent. See [recovery](coordination.md#recovery).

**Cleanup cannot confirm the run's processes stopped.** Launch stops and keeps the
run's worktree and branch. Make `ps` usable, or confirm that the named process group
has exited, then restart.

**The cleanup hook fails.** The worktree and branch stay, and the run's outcome stands.
Fix the hook; the next launch retries it before removing the worktree. See
[project cleanup hook](configuration.md#project-cleanup-hook).

**Leftover worktrees or branches, or a branch you still need.** Restart `launch`; its
startup sweep removes leftovers and logs each decision in
`.ub-agents/sessions/<session>.cleanup.log`. Restore a removed branch from its logged
`unpushed tip SHA` with `git branch <name> <sha>`. See
[stale artifact cleanup](configuration.md#stale-artifact-cleanup), which also covers
the 7-day retention of run logs.
