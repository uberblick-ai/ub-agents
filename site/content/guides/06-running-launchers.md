# Running launchers

What a launcher prints, how to stop it, and what to do when something fails. The
[configuration reference](/docs/configuration/index.html) lists every key, and the
[coordination contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md) has the exact rules for claims, attempts and
recovery.

GitHub requests reuse HTTPS connections with your existing `gh` token. The
launcher reads it with `gh auth token --hostname github.com` once per process and
again after an authentication failure. If it cannot read a token, it uses `gh api`.
No extra setup is needed; `init` and `doctor` still use `gh`. See
[GitHub request handling](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md#quota-and-failure-handling)
for token refresh, timeouts and fallback rules.

## Launch output

In a terminal, `launch` opens a read-only [terminal view](/docs/guides/terminal-view.html) of its own
session. With `--no-ui`, or under a pipe or service, it prints plain lines.

Launch output also goes to `.ub-agents/launch.log` in the control checkout, with a
UTC timestamp on each line. A launch first rotates the log when it is larger than
5 MB: `launch.log` becomes `launch.log.1`, replacing the previous archive. Follow it
with `tail -f .ub-agents/launch.log`.

Discovery lines report the pass kind, elapsed time and request counts; identical
consecutive passes print once. Each run also records its launcher requests in
`events.jsonl`. The `gh calls` count includes requests sent over HTTPS or through
`gh api`. See [request accounting](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md#accounting-and-diagnostics) for
the fields. When discovery declines a ready item, one line names the item, agent and
reason, for example `#400 implementer: declined — Head moved before claim`.

## Stopping and restarting

To stop one active agent from GitHub, add a configured stop label such as
`needs-human` to its issue or PR, close the issue or PR, or merge the PR. Closing or
merging stops execution even when no stop labels are configured. The launcher
terminates execution at the next lease renewal check (10 minutes by default).
A stop label discards any early report. Closure settles a report stored before the
process stopped through the normal validation and transitions. Without a report,
closure leaves labels unchanged and charges no failed attempt.
A label removed or an item reopened before the check has no effect.
Resume a stopped open item with
`ub-agents retry NUMBER --agent ROLE --reason "Ready to resume"`. See
[stop labels](/docs/configuration/stop-labels.html).

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
[terminal view](/docs/guides/terminal-view.html) describes the shutdown screens.

For automatic launcher upgrades, restart behavior under service managers and
installation protection, see [Runtime updates](/docs/configuration/runtime-updates.html#updating-the-launcher).

## When things go wrong

Every launcher error ends with one short line:

```text
ub-agents: <What happened>. <What to do>. Here is the error: <one-line error>
```

Follow the action in that line. The full error, including complete `gh` output or
an unexpected exception's traceback, goes to `.ub-agents/launch.log` before the
short line. The terminal shows only the short line. For one stuck issue or PR, see
[Unstick an item](/docs/guides/stuck-items.html).

When neither the selected configuration nor `.ub-agents` exists, launch prints
the line without creating files. This keeps the checkout clean for `ub-agents init`.
A missing configuration is described as missing, with `ub-agents init` as the next step.

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
An unsafe checkout or Git failure stops it, for example:

```text
ub-agents: The control checkout could not be refreshed. Run git status in /path/to/project, then fix origin access and launch again. Here is the error: fetch of origin/main failed: Git operation failed: authentication denied
```

Follow the checkout action and restart.
Startup validation, `launch --once` and `launch N` also stop on invalid configuration
or unreadable instructions. The error part gives the cause reported by
`ub-agents doctor --offline`, for example:

```text
ub-agents: ub-agents.yaml is invalid. Fix /path/to/project/ub-agents.yaml, then launch again. Here is the error: worker max-attempts must be finite and positive
```

No attempt is charged. See
[execution boundaries](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#execution-boundaries).

**Claims paused for invalid configuration.** Continuous launch, including `--agent`,
stays running when configuration or instructions fail validation after a refresh.
It claims no new runs or recoveries and charges no attempt. The terminal banner,
plain output, `launch.log` and `status` show the error, checkout commit and next step.
Correct the configuration or instruction files; for unknown keys or values, upgrading
ub-agents is another fix. Every poll, and `r`, fetches, fast-forwards and rereads even
with an empty queue. Valid files resume claims automatically without a restart;
a restarted launcher validates at startup as usual. Automatic launcher upgrades
and restarts wait until the configuration pause clears. `q` and `SIGTERM` stop promptly
while waiting. The log records the pause starting, its error or commit changing,
and claims resuming, rather than repeating the error every poll.

**Control-checkout setup fails.** Read the log the message names, fix the install in
the control checkout and launch again. Setup runs on every launch until it succeeds.
See [checkout setup](/docs/configuration/checkout-setup.html).

**Setup fails in a private worktree.** The run ends before the agent starts and
counts as a failed attempt. Fix the hook or environment. If the item has used all its
attempts, run `ub-agents retry NUMBER --agent ROLE --reason "Setup fixed"`.

**GitHub is slow, unreachable or rate limited.** Continuous launch retries setup
checks and polls for up to 2 hours without a successful GitHub request, printing
`Skipped GitHub poll: …; retrying in Ns`. Any successful request, including a 304
or background observation, resets this clock. Time waiting for a rate-limit reset
doesn't count. No item attempt is charged. If the limit is reached, launch exits
with code 1 and a line like this:

```text
ub-agents: Encountered GitHub connection issues, giving up after 2h of retrying. Check the network and `gh auth status`, then restart ub-agents launch. Here is the error: GET repos/acme/app/issues/12/comments: HTTP 401 Bad credentials
```

The terminal view keeps this line on its last screen. `launch.log` keeps the full
last error. Check the network and authentication, then restart launch.

Over direct HTTPS, any request, including writes
and GraphQL mutations, retries if opening a new connection fails before sending
starts. This covers DNS, refused or unreachable connections, TLS handshakes and
connect timeouts. Once sending starts, including on a reused keep-alive connection,
only REST `GET` requests and GraphQL queries retry if no response arrives or GitHub
answers 500, 502, 503 or 504. A write's HTTP 5xx does not retry, even after a
connection-open retry. These failures share at most four attempts in total,
waiting 1, 2 and 4 seconds and using fresh connections. The `gh api` fallback does
not retry these failures in place. Other HTTP 4xx responses and GraphQL `200`
responses containing `errors` do not retry in place; rate limits wait for their
reset. HTTP 401 follows the separate policy below.
Each retry appears in `launch.log` with the method, endpoint and attempt.
Ctrl-C interrupts the wait.
`launch --once`, `launch N` and `status` fail if the request still fails after these
retries; run them again later. A retryable launcher error asks you to check access
to `api.github.com` and wait for GitHub to recover before restarting.
`doctor --verbose` shows the remaining quota. See
[quota and failure handling](https://github.com/uberblick-ai/ub-agents/blob/main/docs/polling.md#quota-and-failure-handling).

After setup passes, continuous launch also retries HTTP 401 responses, including
writes and the `gh api` fallback, after 1, 2 and 4 seconds. If all attempts fail,
it skips the poll and waits 1 minute, doubling to 15 minutes within the 2-hour limit.
The log names the local time of the first 401 and suggests `gh auth status`.
Check that command if failures persist; after `gh auth login`, the next attempt
uses the new token without a restart. Any successful GitHub request resets this
backoff and its start time.

**Launch stops with "GitHub rejected a request that ub-agents does not retry."** The
line names the request and a concrete check:

```text
ub-agents: GitHub rejected a request that ub-agents does not retry. Check the repository in /path/to/project/ub-agents.yaml and `gh auth status`, then restart ub-agents launch. Here is the error: GET repos/acme/app/issues/12: HTTP 404 Not Found
```

During continuous-launch setup, HTTP 401 retries with the setup backoff, from
5 seconds to 60 seconds, within the same 2-hour limit. In `launch --once` or
`launch N`, it still stops after checking for a changed token.
A `gh` authentication failure without an HTTP status, redirects returned by GitHub
and other HTTP 4xx errors also stop. Check `gh auth status` and the repository in
your configuration; sign in with `gh auth login` or correct the repository, then
restart. The restarted launcher recovers any claim it left open.

**A runtime reached its usage limit.** The launcher pauses that CLI until the limit
resets, and other CLIs keep working. No attempt is charged. Restart the launcher to
clear the pause early. See [runtime usage pauses](/docs/configuration/runtimes.html#usage-limits).

**A health check fails.** The agent's items show `waiting` with the check's error.
Fix the dependency; claiming resumes on its own. See
[project health checks](/docs/configuration/agents.html#health-check).

**A runtime update broke a CLI.** The launcher marks the CLI unavailable and uses an
alternative runtime if one is configured. While `gh` is unavailable, no run starts.
Repair or reinstall the CLI; a later check between runs finds it working and resumes.
See [daily runtime maintenance](/docs/configuration/runtime-updates.html).

**A launcher died during a run.** Its claim expires when its lease runs out, 30
minutes after the last renewal by default. Then any
launcher recovers the work, and a recorded outcome finishes without rerunning the
agent. See [recovery](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#recovery).

**Cleanup cannot confirm the run's processes stopped.** Launch stops and keeps the
run's worktree and branch. Make `ps` usable, or confirm that the named process group
has exited, then restart.

```text
ub-agents: Cleanup could not finish safely. Confirm process group 321 has exited, then launch again. Here is the error: Process group 321 survived termination
```

**Launch stops unexpectedly or cannot restart after an update.** An unexpected
exception exits 1 with `The launcher stopped unexpectedly. This looks like a bug
in ub-agents. Please report it with launch.log. Here is the error: …`. Include
`.ub-agents/launch.log` in your bug report. A failed automatic restart says
`The automatic launcher restart failed. Run ub-agents launch again by hand. Here
is the error: …`. Start it with `ub-agents launch`.

**The cleanup hook fails.** The worktree and branch stay, and the run's outcome stands.
Fix the hook; the next launch retries it before removing the worktree. See
[project cleanup hook](/docs/configuration/cleanup.html).

**Leftover worktrees or branches, or a branch you still need.** Restart `launch`; its
startup sweep removes leftovers and logs each decision in
`.ub-agents/sessions/<session>.cleanup.log`. Restore a removed branch from its logged
`unpushed tip SHA` with `git branch <name> <sha>`. See
[stale artifact cleanup](https://github.com/uberblick-ai/ub-agents/blob/main/docs/coordination.md#stale-artifact-cleanup), which also covers
the 7-day retention of run logs.
