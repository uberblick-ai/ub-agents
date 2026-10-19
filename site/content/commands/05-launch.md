# ub-agents launch

`ub-agents launch` runs the loop on your machine. It watches the queue, picks the next item an agent can handle and runs that agent. Start it in a clean checkout of your default branch and leave it running. Teammates can run their own launchers on the same queue.

```bash
ub-agents launch
```

Press `q` to stop after the current run, or Ctrl-C to stop right away.

## One agent or one item

```bash
ub-agents launch --agent issue-preparer   # serve only this agent
ub-agents launch 143                      # handle only #143, then exit
ub-agents launch --once                   # run at most one assignment, then exit
```

`--agent` serves only that agent. This allows you to prepare all issues first, for example.

A numbered launch skips the queue order, but every other check still applies: trigger and stop labels, blockers, approvals and ownership. If nothing starts, launch tells you why and what to do next.

## The terminal view

In a terminal, launch opens a live view: what is running, what needs a person, what is next and what just finished. Select an item and press Enter to see its log in the Agent tab, the issue, earlier runs and how to unblock it. Press `?` for all keys. A few you'll use often:

- `r` refreshes the queue now.
- `u` switches the log between formatted and raw.
- `c` keeps your Mac awake while the launcher runs. It starts off each time, and the display can still sleep.

The footer shows the repository, runtime usage pauses and launcher activity, such as `claude paused for 1d 4h · next poll 26s`. Rate-limit waits say when polling can resume.

Outside a terminal, or with `--no-ui`, launch prints plain lines instead. The [terminal-view reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/terminal-view.md#using-the-view) covers run history and countdown displays.

## Before each run

Before each new run, launch pulls the default branch and starts the agent fresh. Changes to `ub-agents.yaml`, AGENTS.md and the agents' role files apply as soon as they land on the default branch. No need to restart the loop for that. A new ub-agents version does need a restart; see [Upgrading](/docs/upgrading.html).

Checkout setup provides a hook to run your own setup before an agent starts, such as installing dependencies. It runs in every new worktree, and in your checkout when watched files change; see the [checkout setup configuration](/docs/configuration/checkout-setup.html).

Agents with a [health check](/docs/configuration/agents.html#health-check) wait until it passes, without using up attempts. Other agents keep working meanwhile.

## Leftovers from earlier runs

Each launch first clears worktrees and run branches that earlier runs left on this machine. It keeps anything still in use. If it removes a branch with unpushed commits, the cleanup log under `.ub-agents/sessions/` records its SHA, so you can restore it with `git branch <name> <sha>`. Cleanup also provides a hook if you want to run your own code after each agent run; see the [cleanup configuration](/docs/configuration/cleanup.html).

## Logs

Launch writes its output to `.ub-agents/launch.log`, and each run keeps its own log under `.ub-agents/runs/`. Run logs older than 7 days are cleared automatically when launch starts. [Launch output](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output) explains the lines.

```text
{{help launch}}
```
