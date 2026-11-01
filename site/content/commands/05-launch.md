# ub-agents launch

`ub-agents launch` runs the loop on your machine: it watches the queue and runs the agent for the next item. Start it in a clean checkout of your default branch and leave it running. Teammates can run their own launchers on the same queue.

```bash
ub-agents launch
```

Press `q` to stop after the current run, or Ctrl-C to stop right away.

Only one launcher can run on a control checkout at a time, in any launch mode.
A second launch exits before startup, naming the running launcher's pid, host and
start time. Stop that launcher first, or use [`retry`](/docs/commands/retry.html)
to hand it an item. Different paths resolving to the same checkout share this
limit; separate control checkouts can each run a launcher.

[`status`](/docs/commands/status.html) and [`doctor`](/docs/commands/doctor.html)
show the launcher running on this checkout. The lock is released when its process
exits, including after SIGKILL, even while its agents or helpers finish.

## One agent or one item

```bash
ub-agents launch --agent issue-preparer   # serve only this agent
ub-agents launch 143                      # handle only #143, then exit
ub-agents launch --once                   # run at most one assignment, then exit
```

`--agent` lets you prepare all issues first, for example. A numbered launch skips the queue order, but labels, blockers, approvals and claims still apply. If nothing starts, launch tells you why.

## The terminal view

In a terminal, launch opens a live view of what is running, what needs a person, what is next and what just finished. Select an item and press Enter for its log, the issue, earlier runs and how to unblock it. Press `?` for all keys. A few you'll use often:

- `r` refreshes the queue now.
- `u` switches the log between formatted and raw.
- `c` keeps your Mac awake while the launcher runs. It starts off each time, and the display can still sleep.

The Runs tab lists every run the launcher knows for the selected item, oldest
first, including failures and runs by other launchers. It reads private local
history files without accessing GitHub. Its tokens column shows an exact count
below 1,000, rounded `Nk` below 1M and `N.NM` from 1M, with a blank cell when no
usage was recorded. Counts come from GitHub coordination records, including for
other launchers. GitHub result lines also show a dollar figure when the CLI
reports a cost. On a Claude subscription, this is what the run would have cost
at API prices, not a charge. Codex reports no cost.

Formatted Claude and Codex logs show local times. A `~` marks when the supervisor
observed output without a producer timestamp, including output written before
you opened the view. Earlier runs without recorded timing have no historical
times; live output uses the view's capture time when needed.

The footer shows usage pauses and launcher activity, such as `claude paused for 1d 4h · next poll 26s`. Outside a terminal, or with `--no-ui`, launch prints plain lines instead. See the [terminal-view reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/terminal-view.md#using-the-view).

## Before each run

Before each run, launch pulls the default branch and starts the agent fresh, so changes to `ub-agents.yaml`, AGENTS.md and the role files apply once they land, without a restart. A new ub-agents version does need one; see [Upgrading](/docs/upgrading.html).

If `ub-agents.yaml`, `ub-agents.local.yaml` or an instruction file fails validation
after a refresh, continuous launch (including `--agent`) pauses claims. It starts
no new runs or recoveries and uses no attempts. The terminal banner, plain output,
`launch.log` and [`status`](/docs/commands/status.html) show the error, checkout commit
and next step. Correct the file, or upgrade ub-agents for an unknown key or value.
Every poll, or `r`, fetches and rereads even with an empty queue; valid files resume
claims automatically. `q` and `SIGTERM` stop while paused as they do while idle.
Startup validation, `--once` and numbered launches still fail on invalid files.
Unsafe checkouts and failed fetches or fast-forwards still stop launch.

[Checkout setup](/docs/configuration/checkout-setup.html) runs your own setup, such as installing dependencies, in every new worktree. Agents with a [health check](/docs/configuration/agents.html#health-check) wait until it passes, without using up attempts.

## Leftovers from earlier runs

Each launch first clears worktrees and run branches that earlier runs left on this machine, keeping anything still in use. A removed branch with unpushed commits has its SHA in the cleanup log under `.ub-agents/sessions/`; restore it with `git branch <name> <sha>`. To run your own code after each run, see [Cleanup](/docs/configuration/cleanup.html).

## Logs

Launch writes to `.ub-agents/launch.log`, and each run keeps its log under `.ub-agents/runs/` for 7 days. [Launch output](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output) explains the lines.

```text
{{help launch}}
```
