# ub-agents monitor

Run `ub-agents monitor` to watch the loop in a browser: what needs a person, what
runs now, what runs next and what ended in the last 24 hours, on every machine that runs a launcher
for the repository. The terminal view shows only this launcher's runs; the monitor
shows all of them, including a server's. It never changes anything on GitHub.

```sh
ub-agents monitor                      # serves on 127.0.0.1:4377 and opens it
ub-agents monitor --port 4400          # another port
ub-agents monitor --no-open            # serves without opening a browser
ub-agents monitor --background         # keeps serving after the command exits
```

It reads GitHub with your `gh` login, as the launcher does, so there's no token to
paste. Ctrl-C stops it. With `--background` it keeps serving after the terminal
closes: the command waits until the page answers, prints the process id, and
`kill PID` stops it; its output goes to `.ub-agents/monitor.log` in the checkout.
Running it again while it runs just opens the page.

Closing or reloading the page while a reply is pending is quiet. Other server
errors still print in the terminal or background log.

## What the page shows

- **Needs human**: open items with a [stop label](/docs/configuration/stop-labels.html),
  and runs that reported blocked and weren't retried since. Each shows the actions
  and options of its latest outcome, with links to the Action needed comment and the
  outcome record. **discuss with Claude** opens a Claude Code session in the Claude
  app, in this checkout, with "help me unblock #N using the ub-agents-unblock skill"
  filled in; the app asks you to confirm the folder before you send it.
- **Running**: every live run, with its agent, machine and how long it has run.
  The dropdown in the top-right border shows all machines by default. Choose a
  machine to show only its runs; the heading counts the runs shown. The choice
  survives a reload. Options include this machine, machines with live runs or
  activity in the last 24 hours, and your selected machine even after its runs
  age out. All Machines counts those machines, including this machine. The filter
  affects only Running; other panels and notifications still cover every machine.
- **Eligible**: the next 20 items a launcher on this checkout would claim, in the
  terminal view's order. Five rows fit at once; scroll inside the panel to see the
  rest. The heading counts all eligible items and says how many are shown when
  there are more than 20. Ready and recoverable work comes first, then backoff and
  waiting work. The first ready or recoverable item says **next**. Each row links
  to its item and shows its agents, failures and any labelled priority, including
  inherited priority. A configured default priority alone shows no word. Local
  usage pauses show the runtime and time left. Claimed work, work needing a
  person, and items waiting for blockers or the active milestone stay out.
- **Expired without a result**: runs whose launcher stopped renewing them. The next
  launcher that picks the item up recovers it.
- **Recent activity**: runs that ended in the last 24 hours, with result, machine,
  duration, tokens and cost. Click a row for the full summary.

Records count only from accounts that the launcher trusts: write access or higher,
and listed in [`launchers`](/docs/configuration/launchers.html) when that is set.
The host serving the monitor reads **this machine** in every panel and run detail.

## Polling and notifications

The page polls every 30 seconds; press `r` to poll now. **GH API budget** in the
status bar shows the share of your account's GitHub REST calls left this hour, since
every tool using the account draws from them; hover it for the counts.
An unchanged 30-second poll measured on this repository used 1 counted REST call
and 0 GraphQL calls, because unchanged REST reads answer "not modified". Eligible
replans every five minutes, keeping its planning state between polls. Pressing `r`
also keeps Eligible's plan until the five-minute interval ends. An unchanged poll
that also replanned used 2 counted REST calls and 0 GraphQL calls after
warm-up; the initial read used 6 counted REST calls and 1 GraphQL call. Costs vary
with the repository and its configuration. Hover Eligible's heading for its
interval and last planning time. If Eligible planning fails, its last result stays
under a **Couldn't refresh** banner while the other panels and their notifications
keep updating. Before the first successful plan, Eligible says **Planning unavailable**.
The next poll retries planning. If another read fails, the page keeps its last result
under the banner.

Click **notify: off** to switch on browser notifications. The page sends one test
notification and, for 20 seconds, says where to look if none appeared (on macOS:
System Settings › Notifications › your browser, and Focus). **notify when** sets what
notifies: an item gets a stop label (the default), a run reports blocked or one
of the outcomes your config sends to a stop label (such as `maintainer-merge`),
or a run starts, ends or expires. Stop labels, blocked reports and those outcomes
stay on screen until dismissed;
clicking a notification opens the item. Nothing notifies for what was already
there when the page opened. Eligible never notifies. With notifications on, a hidden tab keeps polling.

## Security

The server listens on `127.0.0.1` only. It answers only requests addressed to
`127.0.0.1` or `localhost`, so another site can't point its own hostname at it, and
refuses background requests from other sites. The `gh` token stays in the server;
the page gets only the records it shows.

```text
{{help monitor}}
```
