# ub-agents launch

Run `ub-agents launch` to watch the queue in the foreground. In a terminal it opens the terminal view. `q` stops after the current run; Ctrl-C stops right away. Give a number to handle only that item.

Press `r` to refresh the queue. During a continuous launch's assignment, this
read-only refresh shows `running assignment · polling` in the footer until it
finishes, including when
the view is reopened. Repeated presses during the refresh keep that label;
presses during the 10-second cooldown show `poll now available in Ns`.
Rate-limit waits show `rate limited until HH:MM · r unavailable`.
`launch --once` and `launch N` do not refresh the queue during an assignment;
pressing `r` then leaves the footer at `running assignment`.

```text
{{help launch}}
```
