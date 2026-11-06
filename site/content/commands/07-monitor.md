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
ub-agents monitor ~/code/backend ~/code/studio
ub-agents monitor . ../studio --background
```

Give any number of checkout folders or paths to their `ub-agents.yaml` files.
Each checkout applies its own `ub-agents.local.yaml` overrides. With none, the
monitor can read a list saved in the current folder:

```yaml
# ~/Projects/Uberblick/ub-agents-monitor.yaml
checkouts:
  - uberblick-2
  - ub-agents
```

Keep those checkouts next to the file, then run `ub-agents monitor` from
`~/Projects/Uberblick`. The monitor watches them in the listed order. Paths are
relative to the file, and `~` expands to your home folder. You can also list paths
to the checkouts' `ub-agents.yaml` files. This folder needs no `ub-agents.yaml` of
its own.

The command chooses checkouts in this order:

1. Checkouts on the command line, or `--config PATH`, skip the monitor file.
2. Otherwise, `ub-agents-monitor.yaml` in the current folder wins, even when that
   folder also has `ub-agents.yaml`. Parent folders are never searched.
3. Without a monitor file, it uses the current folder's `ub-agents.yaml` as usual.

The monitor file accepts only `checkouts`, a list of at least one path. An unknown
key, a missing or empty list, or a non-path entry is an error naming the file.
An entry whose checkout is missing or whose configuration is invalid stops the
command at start, naming both the entry and the monitor file. The list and the
checkouts' configurations are read once at start; restart to apply edits.

You cannot combine checkouts with `--config`, or give two checkouts that configure
the same repository. Every
configuration must load before the monitor starts; an error names its checkout.

It reads GitHub with your `gh` login, as the launcher does, so there's no token to
paste. Ctrl-C stops it. With `--background` it keeps serving after the terminal
closes: the command waits until the page answers, prints the process id, and
`kill PID` stops it; its output goes to `.ub-agents/monitor.log` next to the monitor
file when using that file, or in the first checkout otherwise.
The background process receives absolute paths for every checkout's configuration.
Running it again while it runs just opens the page.

Closing or reloading the page while a reply is pending is quiet. Other server
errors still print in the terminal or background log.

## What the page shows

The browser tab shows a robot icon and your repository's short name. The title
includes the needs-human count when any items need a person, otherwise the running
count when any runs are active. Until the first snapshot arrives, the title is
`ub-agents`.

With several checkouts, the header has a repository dropdown, styled like Running's
machine dropdown. **All repositories (N)** is selected by default. Each repository
follows in the order you gave them, using its short name or `owner/name` when short
names match. With one checkout, the header stays as the repository name.

Choose a repository to show its panels, counts, banners and Needs human all-clear.
Rows drop their repository name, Eligible shows that checkout's own queue without
a repository line, and the tab title names only that repository. The machine
dropdown includes only machines from the repositories shown, plus this machine
and any kept machine choice. Your browser remembers the repository choice by
`owner/name` across reloads. If the monitor no longer watches it, the choice returns
to **All repositories**. This filters the view: the server still reads every
repository, so GitHub cost stays the same.

With **All repositories** selected, rows start with their repository's short name.
Needs human keeps each repository's order, with repositories in the order you gave
them. Running, Expired and Recent activity combine rows by time. **discuss with
Claude** opens the row's own checkout. The Running machine dropdown includes
machines from every repository.

Eligible groups items under each repository's name, since launchers claim from
their own repository's queue. Each group keeps its own claim order, priority
words, 20-row limit and **next** item. Empty groups disappear; a group with no
successful plan says **Planning unavailable**. The heading sums the counts and
says how many rows are shown when any group is cut.

With **All repositories** selected, the tab count adds all repositories. Its names
identify the repositories needing a person, otherwise those with running work, or
all repositories when idle.
More than two names use the first name plus `+N`, for example:

```text
backend, studio
(2) needs human · backend, studio
(1) needs human · studio
3 running · backend +2
```

- **Needs human**: open items with a [stop label](/docs/configuration/stop-labels.html),
  and runs that reported blocked and weren't retried since. Each shows the actions
  and options of its latest outcome, with links to the Action needed comment and the
  outcome record. **discuss with Claude** opens a Claude Code session in the Claude
  app, in this checkout, with "help me unblock #N using the ub-agents-unblock skill"
  filled in; the app asks you to confirm the folder before you send it.
  Click **dismiss** to move a row out of **Open**, its counts, the tab title count
  and stop notifications. When the shown repositories have dismissed rows, a dropdown
  on the panel's lower border offers **Open (N)** and **Dismissed (N)**. Choose
  **Dismissed** to see those rows, with a heading of **Needs human · N dismissed**;
  click **restore** to return one to **Open** quietly. The repository choice in the
  header filters both lists and their counts. With no open rows, **Open** shows the
  all-clear once every shown repository has been read, and the tab title uses
  running work or repository names. The dropdown stays until nothing shown is dismissed.
  Your browser remembers dismissals and the Open/Dismissed choice in local storage,
  alongside the repository and machine choices. Dismiss and restore send no request
  and change nothing on GitHub or in a colleague's monitor.
  A dismissal covers one stop: the repository and item number, its latest outcome
  or blocked run record, its Action needed notice and its stop labels. A new record,
  notice or set of stop labels makes it open again. Once every watched repository
  has been read at least once, dismissals for stops no longer in Needs human are
  removed across all watched repositories, whichever is selected. Failed reads
  keep the last rows and their dismissals. With no dismissed rows in the chosen
  repositories, the dropdown hides and the panel shows **Open**.
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
  to its item and shows its agents and failures. With `queue.priority` configured,
  it shows the item's effective priority, including inherited priority, only when
  it differs from `queue.priority.default`. With no default set, any effective
  priority shows. Items with no effective priority show no word. Without
  `queue.priority`, no priority word shows. Local usage pauses show the runtime
  and time left. Claimed work, work needing a person, and items waiting for
  blockers or the active milestone stay out.
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
interval and last planning time, or a repository line when watching several
checkouts. If Eligible planning fails, its last result stays
under a **Couldn't refresh** banner while the other panels and their notifications
keep updating. Before the first successful plan, Eligible says **Planning unavailable**.
The next poll retries planning. If another read fails, the page keeps its last result
under the banner. With several checkouts, each repository reads in parallel with
its own cache and five-minute Eligible plan. A failed repository keeps its last
result while the others refresh. Until any shown repository has been read successfully,
the page shows only the read errors. When some repositories have been read and
others have not, their results stay visible, and Needs human withholds its all-clear
until every shown repository has been read once. When several repositories are
shown, refresh errors, unreadable records, newer record formats and Eligible
errors all name their repository. The budget
uses the newest account quota headers and adds the monitor's call counters across
repositories. Each repository costs what one monitor costs: quiet repositories mostly answer
"not modified"; two busy repositories cost about twice one.

Click **notify: off** to switch on browser notifications. The page sends one test
notification and, for 20 seconds, says where to look if none appeared (on macOS:
System Settings › Notifications › your browser, and Focus). **notify when** groups
stops under **a person is needed**. Its indented choices cover blocked reports,
each outcome your config sends to a stop label (with its declaring agents), and
stop labels added another way, such as by hand or a round or attempt limit.
With several repositories, each outcome name has one checkbox. Its text lists
all declaring agents and names the repositories when only some declare it, such
as `a run reports maintainer-merge (integrator · backend only)`. Stop labels combine
across repositories. Notifications include the repository name, so the same item
number in two repositories can notify twice. Notifications and **notify when**
always cover every repository, whichever repository you choose in the header.
Kept choices and the machine filter remain valid when you add checkouts.
A stop-labelled item notifies only for the choice matching its latest outcome,
or for **another way** if no stop outcome explains it. Blocked reports on items
without stop labels use the blocked choice.
A dismissed stop does not notify. A different record, notice or set of stop labels
on the same item is a new stop and notifies according to your choices. Restoring
the same stop adds it back to the panel and tab counts without notifying, including
on the next poll. These rules apply across every watched repository, whichever
repository or Needs human list you choose to view.

Ticking or clearing the parent selects or clears every child. A partial tick means
some children are selected. By default, all these stop choices are selected;
run starts, ends and expiries are clear. Your browser remembers your choices.
Press Esc, click outside the picker, or click **notify when** again to close it.
Stop notifications stay on screen until dismissed. An item gets at most one
notification per poll, with a stop taking precedence over an ordinary run event.
Clicking a notification opens the item. Nothing notifies for what was already
there when the page opened. Eligible never notifies. With notifications on, a
hidden tab keeps polling.

## Security

The server listens on `127.0.0.1` only. It answers only requests addressed to
`127.0.0.1` or `localhost`, so another site can't point its own hostname at it, and
refuses background requests from other sites. The `gh` token stays in the server;
the page gets only the records it shows.

```text
{{help monitor}}
```
