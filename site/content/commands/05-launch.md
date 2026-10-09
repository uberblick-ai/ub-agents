# ub-agents launch

Run `ub-agents launch` to watch the queue in the foreground. In a terminal it opens the terminal view. `q` stops after the current run; Ctrl-C stops right away. Give a number to handle only that item.

Use `ub-agents launch --agent NAME` to serve only one agent named in your
configuration, or `launch --once --agent NAME` to observe once and run at most
one selected assignment. Queue priority, milestone policy and all other gates
still apply. The filter also limits recovery and notice reconciliation; other
agents' leases, pending outcomes and notices stay untouched. Started runs keep
normal lease renewal, stopping and process cleanup. An unknown name exits
nonzero before observing the queue and lists configured names. If a reload
removes the selected agent, it claims nothing and says the agent is no longer
configured.

For preparation first, run `ub-agents launch --agent issue-preparer`, substituting
your configured preparation agent name. Watch a milestone's issues finish
preparation or reach human decisions, stop the launcher, resolve the decisions,
then run `ub-agents launch` for the normal queue. The filtered launcher keeps
polling until you stop it, even when its work runs out. It does not override
milestones, bypass blockers or automatically hand control to another agent.

The terminal view and `--no-ui` output name the selected agent. When idle, the
message distinguishes no open item with its trigger labels, naming those labels,
from waiting work counted by state and reason, such as blocked, parked, awaiting
input approval or owned elsewhere. Neither message means every issue is prepared.

`launch N` skips queue priority and milestone policy, including the `gate` hold
during planning, after checkout refresh, at claim time and during approval parking.
It can start later-milestone or unmilestoned work. Every other gate still applies,
including launcher trust, item state, trigger labels, stop labels, dependencies,
input approval, ownership, attempts, backoff and runtime availability, with fresh
checks and claim elections. Only triggered agents can act: `--agent NAME` narrows
evaluation to that agent and still requires its trigger. Otherwise the first
eligible agent in configuration order acts. Unnumbered `launch` and `launch --once`
keep the queue's selection policy.

`ub-agents launch N [--agent NAME]` exits nonzero when it starts no assignment or
recovery, leaving one short refusal line per evaluated agent visible after the
view closes. Plain output ends with the same reasons after discovery counters.
`--agent NAME` evaluates only that agent. Reasons name the owning role, account,
host and lease end, including the owning item when another item's run holds the
branch, plus stop labels or open blockers, and the next step
where one exists. A stop label explains acting on the Action needed notice,
removing the label and restoring a trigger, even if the trigger is already gone.
A decline during refresh or claiming reports its final cause. GitHub read
failures remain errors.

After a run or recovery finishes, continuous launch immediately checks for the
next item, including after retry or blocked results. Rate-limit waits, runtime
usage pauses and graceful stops still apply. With no assignment, Running shows
`Idle · polling` during a pass, then `Idle · nothing eligible for this launcher`
only after a complete pass with nothing claimable.

Launch output and `.ub-agents/launch.log` report each discovery pass's kind,
wall time, attempted `gh` calls, REST quota responses, HTTP 304 responses,
GraphQL calls and distinct candidates reached. Consecutive identical pass counts
print once, even if wall time differs. Observation passes measure their own client.
The run's `events.jsonl` `released` event records the launcher's requests from
the successful claim's fresh checks through release, including renewals and
finalization, excluding observation-worker and agent traffic. A reached ready
plan that is declined names the item, agent and reason; consecutive identical
declines print once. See [launch output](https://github.com/uberblick-ai/ub-agents/blob/main/docs/operations.md#launch-output)
for the fields and decline reasons.

When a supervised run's outcome lands, Work immediately refreshes that item's
rows for every evaluated agent, even during a partial pass. A filtered queue
refreshes only its selected agent. Rows for its next
role appear and obsolete rows disappear before the launcher claims again. This
display refresh reads only that item's inputs; it makes no claims or repository
discovery, and a failed read leaves the result and next pass unchanged.

Needs attention shows each item once and counts items. When several agents need
attention, its second line lists each agent with its state comma-separated, then
the distinct reasons comma-separated. A single agent keeps `agent · state · reason`.
The row keeps its first agent's glyph and shows the longest known waiting time.
Selection stays on the item as polls, partial passes and post-run refreshes change
its agents and reasons. Other agents for that item can still appear in Running or
Eligible; Issue and Unblock preserve each attention agent's reasons and resume steps.

Configured [agent health checks](/docs/configuration/agents.html#health-check) make otherwise-ready items wait in Eligible without spending attempts. The terminal view shows the latest failure, changed error or recovery in one line above the panes, also printed in plain launch output and `.ub-agents/launch.log`. Claiming resumes automatically when the check passes; other agents keep claiming while it fails.

While a new run's log file has not appeared yet, Log shows `No log output yet.`
without a read error and follows output automatically once it appears. Other read
failures, including a log that disappears after being read, still show `Read error`.

Open Issue with `2`, a tab click, or select another item while Issue is active to
load its missing title and description from GitHub. It shows `Loading #N…` while
the read is pending, then caches the result. Reopening a cached item makes no read;
press `g` to retry a failure. Descriptions are limited to 2,048 characters with a
shortening notice. The session snapshot carries no description text and keeps
Running, Needs attention and the first ten Eligible items ahead of hidden plans
and older history; Eligible's heading keeps the full count when hidden plans are
left out to fit the size limit.

Pane and overlay scrollbars stay hidden until you scroll. Each muted bar uses
one reserved column, so appearing or disappearing never shifts the content.
It hides after 1.5 seconds of inactivity; hovering or dragging keeps it visible.
Log follow updates and automatic position changes leave the bar hidden.

Recent activity fills the lower half of Work and scrolls independently of the
live sections above it. Use the mouse wheel or trackpad to reach all 20 retained
outcomes; arrow keys keep the cursor visible, and clicking a row selects it.
`Enter` opens its details and local log. Selection and position survive refreshes;
new outcomes keep older rows in view when browsing away from the top. Resizing
keeps the selected retained outcome visible. Recent activity follows the same
scrollbar convention as the other areas.

The fixed, non-selectable header reads `Recent activity · showing N`, counting
all listed outcomes, including those outside the viewport, up to 20. It reads
`showing 0` when empty. Rows start directly below the header; there is no notice
or reserved header line for older outcomes. Scrolling reaches rows outside the
viewport, but cannot recover outcomes removed from the
20-outcome cache.

Before each new run, launch refreshes the control checkout and reloads its configuration. Optional [checkout setup](/docs/configuration/checkout-setup.html) reinstalls dependencies when watched files changed. The view names the triggering file; command output stays in the reported log. A failed setup stops launch before a claim or charged attempt and retries on the next launch, even with nothing to pull.

Press `r` to refresh the queue. During a continuous launch's assignment, this
read-only refresh shows `running assignment · polling` in the footer until it
finishes, including when
the view is reopened. Repeated presses during the refresh keep that label;
presses during the 10-second cooldown show `poll now available in Ns`.
Rate-limit waits show `rate limited until HH:MM · r unavailable`.
`launch --once` and `launch N` do not refresh the queue during an assignment;
pressing `r` then leaves the footer at `running assignment`.

Continuous launch retries transient discovery and claim POST failures, including
empty or truncated responses reported by `gh` as `unexpected end of JSON input`.
It waits 5 seconds initially, doubles the delay up to 60 seconds, and stops after
six consecutive failures. If a failed claim POST created a comment, the next pass
withdraws it before planning, without spending an attempt or adding item backoff.
If the launcher restarts first, the claim expires normally. `launch --once` fails
on the first error. See [polling and retry limits](https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#top-level)
for details.

```text
{{help launch}}
```
