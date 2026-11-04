# ub-agents monitor

Run `ub-agents monitor` to watch the loop in a browser: what needs a person, what
runs now and what ended in the last 24 hours, on every machine that runs a launcher
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
- **Expired without a result**: runs whose launcher stopped renewing them. The next
  launcher that picks the item up recovers it.
- **Recent activity**: runs that ended in the last 24 hours, with result, machine,
  duration, tokens and cost. Click a row for the full summary.

Records count only from accounts that the launcher trusts: write access or higher,
and listed in [`launchers`](/docs/configuration/launchers.html) when that is set.

## Polling and notifications

The page polls every 30 seconds; press `r` to poll now. **GH API budget** in the
status bar shows the share of your account's GitHub REST calls left this hour, since
every tool using the account draws from them; hover it for the counts.
A poll costs about one counted call while nothing changed, because unchanged reads
answer "not modified".

Click **notify: off** to switch on browser notifications. The page sends one test
notification and, for 20 seconds, says where to look if none appeared (on macOS:
System Settings › Notifications › your browser, and Focus). **notify when** sets what
notifies: an item gets a stop label (the default), a run reports blocked or one
of the outcomes your config sends to a stop label (such as `maintainer-merge`),
or a run starts, ends or expires. Stop labels, blocked reports and those outcomes
stay on screen until dismissed;
clicking a notification opens the item. Nothing notifies for what was already
there when the page opened. With notifications on, a hidden tab keeps polling.

## Security

The server listens on `127.0.0.1` only. It answers only requests addressed to
`127.0.0.1` or `localhost`, so another site can't point its own hostname at it, and
refuses background requests from other sites. The `gh` token stays in the server;
the page gets only the records it shows.

```text
{{help monitor}}
```
