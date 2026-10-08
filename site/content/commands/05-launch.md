# ub-agents launch

Run `ub-agents launch` to watch the queue in the foreground. In a terminal it opens the terminal view. `q` stops after the current run; Ctrl-C stops right away. Give a number to handle only that item.

Configured [agent health checks](/docs/configuration/agents.html#health-check) make otherwise-ready items wait in Eligible without spending attempts. The terminal view shows the latest failure, changed error or recovery in one line above the panes, also printed in plain launch output and `.ub-agents/launch.log`. Claiming resumes automatically when the check passes; other agents keep claiming while it fails.

While a new run's log file has not appeared yet, Log shows `No log output yet.`
without a read error and follows output automatically once it appears. Other read
failures, including a log that disappears after being read, still show `Read error`.

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

`N today` counts cached outcomes dated today. When older outcomes have been
evicted, `N older outcomes not retained` appears below the header. The header and
notice stay fixed above the scrolling rows, including at the oldest retained
outcome. Scrolling
reaches rows outside the viewport, but cannot recover outcomes removed from the
20-outcome cache. The notice uses the existing snapshot and needs no extra
GitHub requests.

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
