# ub-agents

ub-agents runs coding agents in an engineering loop driven by GitHub. Each project
defines its workflow; the launcher watches GitHub, claims matching work, runs the
configured agent, and records its outcome.

> Early stage. The starter workflow runs end to end on this repository. PyPI
> releases are planned in [#4](https://github.com/uberblick-ai/ub-agents/issues/4).

## How it works

```text
Observe GitHub → match a label → claim the item → run the agent → record the outcome → repeat
```

- **Labels are the queue.** A label such as `ready` or `needs-review` on an issue or PR
  says what should happen next. The agent whose trigger matches picks it up.
  PR work runs first; the [queue settings](docs/configuration.md#queue) optionally
  rank priorities and either gate new issues by milestone or order them by
  milestone before priority. New issues wait for open GitHub blockers by default;
  blockers inherit priority from open local dependents and, in milestone `order`
  mode, their earliest milestone. PRs inherit priority from the open issues they
  close. Defaults use FIFO and ignore milestones.
- **GitHub is the state.** Claims, attempts and outcomes are comments on the issue or
  PR. A restarted launcher rebuilds coordination from GitHub; there is no separate
  database or service.
- **Runtime maintenance is optional.** Projects can enable
  [daily Claude Code and Codex updates](docs/configuration.md#daily-runtime-maintenance).
  Cooldowns and locks live in shared per-user local state; active runs continue.
- **The project owns workflow policy.** You choose what labels mean and what should
  happen after each role finishes. The launcher matches triggers, supervises runs,
  retries and validates handoffs, then applies the label transition declared for
  the role's reported outcome.

## Use it in your project

You need macOS or Linux, `git`, an authenticated `gh`, and the agent CLIs you want to
use, such as `codex` or `claude`. Homebrew installs Python and `gh`; checkout installs
require Python 3.11+. Launchers may use different GitHub accounts with `write`,
`maintain` or `admin` repository access and share one queue. By default, records
from any account with those roles count, including humans with write access.
These humans can already change labels and push, so posting records adds little
authority. Optional `launchers: [bot-a, alice]` narrows that set; listed accounts
still require `write` or higher. See [launcher accounts](docs/configuration.md#launcher-accounts).

**Upgrading for shared accounts:** older launchers trust only their own account.
Stop all of the project's launchers and upgrade them together before mixing accounts.

```sh
brew install uberblick-ai/tap/ub-agents
cd your-project
ub-agents init         # starter ub-agents.yaml and .agents/ policy and roles
ub-agents doctor       # check the machine, GitHub labels/access and runtimes
ub-agents launch       # run the loop in the foreground; Ctrl-C stops it
```

`doctor` shows every warning and failure with its remedy, then summarizes passed
and skipped checks by area. Each label has one result naming every agent and use,
and counts once in the totals. Use `doctor --verbose` for the full per-check list,
or `doctor --json` for all
structured results; `--verbose` does not change JSON output.

Interactive launches open a read-only [terminal view](docs/terminal-view.md) of
their own session; `q` stops after the current run, while Ctrl-C stops it immediately.
Use `launch --no-ui` for plain lines. Pipes and services stay plain.

Launch output is flushed immediately to the terminal when the view is closed and appended to
`.ub-agents/launch.log` in the control checkout, with a UTC timestamp on each file
line, including stop and error messages. This also applies to `launch --once`;
the log is never truncated or rotated. Follow it from another terminal with
`tail -f .ub-agents/launch.log`. See [Stopping and restarting](#stopping-and-restarting)
for signal handling, including during a GitHub request.

When no open issue or PR has a configured trigger label, both `launch` and
`launch --once` name the labels to add. Continuous launch shows the next poll
delay rounded to whole seconds below a minute or whole minutes otherwise.
Empty polls back off according to their REST quota cost, excluding unchanged
reads confirmed by HTTP 304 and reserving half the common account quota for busy
work when ten idle launchers share it. Low quota
adds a wait bounded by the reset. A pass that runs or recovers work returns to
normal `poll-seconds` pacing. The continuous loop retries transient GitHub
discovery failures with bounded waits.
Continuous launch waits out GitHub rate limits without charging poll failures or
item attempts. Owned runs retry rate-limited reads while the lease permits; writes
keep their existing handling. `doctor --verbose` reports request quota and reset time in UTC,
warning below 10% remaining. See [polling and retry limits](docs/configuration.md#top-level)
for the waits and failure limit. `launch --once` and `status` fail on the first
error.

In an interactive terminal, `init` and `doctor` explain the missing workflow labels
with the effect first (such as starting an implementer), followed by the reports
that add or remove them. GitHub label descriptions use the same wording, up to
GitHub's 100-character limit. Both commands offer to create missing labels; the
default is no. They write labels only after a confirmed
`y` or `yes` and never change existing labels. `doctor` offers after its report,
then reads the labels again so its final counts and exit status reflect that read.
Declining, end of input, CI, pipes and `doctor --json` leave runnable `gh label create`
commands without creating labels. Noninteractive `init` makes no GitHub calls beyond
repository inference; `doctor` still reads GitHub to diagnose prerequisites.
`doctor` fails for labels with any trigger or transition use and warns for labels
used only to stop work. `launch` refuses to start when a trigger or transition label
is missing and points to `ub-agents doctor` for setup commands.

In an interactive terminal, `init` also asks once whether to enable starter
[permissions](docs/configuration.md#runtime-permissions) for all four agents;
the default is no. Codex gets full access without the sandbox. Claude gets
unattended edits plus git, gh and report commands; add your project's check commands
to `--allowedTools`. Declining, end of input and noninteractive runs leave the
matching `runtime-args` examples commented out and print the permission setup step.

Before launching, create any missing labels, document build and test commands in
the project guidance your runtimes load, fill in `.agents/ub_agents.md`, and enable
or customize each agent's `runtime-args` to grant the permissions its job needs.
`doctor` groups runtime agents without arguments into one warning. Run `ub-agents check`,
commit and push the starter files, then run `ub-agents doctor`. `init` prints relative
paths for the configuration, policy and roles, followed by a `next:` setup line.
It leaves existing guidance files untouched. It names the
guidance file each configured runtime loads and warns when there is none. Codex
loads `AGENTS.md`; Claude loads `CLAUDE.md` or `.claude/CLAUDE.md`, falling back to
`AGENTS.md` when neither exists. The starter policy points to existing checks in
any of these files, preferring one the runtime loads. If Codex loads none, `init`
names the existing Claude guidance and suggests a one-line `AGENTS.md`, such as
`Read .claude/CLAUDE.md`. Checks placeholders appear only when no guidance file exists.

Customize these parts:

- In `ub-agents.yaml`, set each role's trigger labels, CLI and model, runtime
  permissions, worktree choice, named outcomes and their label transitions, and
  any labels that should pause all work.
- In `.agents/ub_agents.md`, define loop checks, merge authority, who answers
  decisions and review priorities. `shared-instructions` sends this policy to every
  role, after the [launcher contract](docs/coordination.md#run-prompt).
- In `.agents/<role>.md`, define the role's procedure and successful handoff.
  Keep build commands and conventions in the repository's own runtime guidance.
  Humans own priority and human-only decisions.
- In GitHub, create the labels and set branch protection or required reviews that
  match your merge policy.

A trigger selects work. An agent reports a declared outcome with
`ub-agents report --outcome NAME --summary TEXT [--handoff PR] [--action TEXT] [--option TEXT]`; the launcher validates
it and applies the project's transition. `--status retry|blocked` changes no labels.
Stop reports (`--status blocked` or outcomes adding a configured stop label) require
at least one `--action` or `--option`. Repeat `--action` for independent asks that
are all needed; repeat `--option` for alternative ways to unblock, with the
recommendation first. Each is one non-empty line of at most 300 characters
(8000 total across both). Each **Action needed** notice shows the reason, asks,
numbered options and resume steps; full Markdown reasoning and evidence are
collapsed by default. Retry and other outcomes do not require an ask or option.
Agents replace `ub-agents` in that command with the literal `report_command` from
their assignment context, also supplied as `UB_AGENTS_REPORT`, to use the launcher's
own installation even when a login shell changes PATH.
Review the [coordination contract](docs/coordination.md) and
[configuration reference](docs/configuration.md) for recovery and permissions.

Run `ub-agents`, `ub-agents help` or `ub-agents --help` for the same compact, aligned
command overview. Rows show one usage form; detailed help includes all options and
alternatives. Use `ub-agents help COMMAND` or `ub-agents COMMAND --help` for purpose,
usage, arguments, options and examples. Help works outside a configured repository,
without GitHub authentication or network access, and creates no files. Square
brackets indicate optional arguments: `launch [NUMBER]` takes an optional item,
while `retry NUMBER` and `approve NUMBER` require one.

Read related issues and PRs with `ub-agents read N`. It prints JSON filtered by
the [assignment input rules](docs/approvals.md#reading-other-issues-and-prs), including
withheld counts. Inside a run, use the launcher's literal `report_command` followed
by `read N`; it uses the launcher's repository and configuration. The top-level
`trusted-bots: [copilot-pull-request-reviewer, Copilot]` list trusts feedback only when
GitHub identifies that login as a bot, including Copilot's separate inline-comment
login. Matching ignores the optional `[bot]` suffix. Listed bots gain no maintainer
or launcher authority.

| Command | What it does |
|---|---|
| `ub-agents help [COMMAND]` | Show the overview, or detailed command help with examples |
| `ub-agents status` | Show matching work, lease details, whether local agents are running, what they reported, and recorded permission denial counts |
| `ub-agents launch [--no-ui]` | Watch the queue; open the installed view on a TTY or keep plain output with `--no-ui` |
| `ub-agents launch --once [--no-ui]` | Run at most one assignment, then exit |
| `ub-agents launch N [--agent NAME] [--no-ui]` | Run or recover only item N under the usual gates, then exit; use the first eligible configured agent or select one |
| `ub-agents cleanup [--apply]` | Preview stale private worktrees and local branches; apply eligible removals |
| `ub-agents retry N --reason TEXT [--agent NAME]` | Let stopped work run again, with a recorded reason |
| `ub-agents approve N` | Print current issue or PR input and post a maintainer [approval record](docs/approvals.md) |
| `ub-agents read N` | Read an open or closed issue or PR as filtered JSON without changing it |
| `ub-agents check` | Validate the configuration files only |
| `ub-agents report` | Used by agents to record their outcome |
| `<report_command> retrospective --body-file PATH` | Post to the supervised agent's configured retrospective board and print the comment URL |

`--config PATH` works before or after every configuration command; giving it in
both positions is a usage error. Commands requiring configuration name `ub-agents init`
when the selected file is missing; `launch` creates no log or local artifacts then.
Without `--agent`, `retry` prints and uses the
first configured agent whose kind applies to the item. Use an explicit agent when
resetting its attempts. The deprecated `--number N` alias remains available for
one release, hidden from help.

## Stopping and restarting

| Signal or key | Effect | Exit |
|---|---|---|
| `q` in the view | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup. A centered shutdown screen stays visible until exit; idle launchers exit promptly. | 0 |
| `SIGTERM` (`kill -TERM <pid>`) | No new claims. The current run or recovery finishes, including its report, label transitions and cleanup; an in-progress checkout refresh finishes without a claim. When idle it exits promptly. | 0 |
| `SIGINT` (Ctrl-C) | Terminates the active agent and prints `Stopped; supervised execution terminated`, including when idle or during a GitHub request. After confirmed cleanup the lease is released as an operator interrupt: attempt count unchanged, eligible on the next launch without backoff. Also applies during a SIGTERM drain. | 130 |
| `SIGHUP` | Same as Ctrl-C. | 130 |
| Further `SIGTERM` | During a SIGTERM drain: no change, the drain continues. After Ctrl-C or SIGHUP: does not interrupt process-group termination, cleanup or release. | unchanged |

Discovery and idle waits (including rate-limit waits outside a run) wake on SIGTERM
and exit 0, and on Ctrl-C/SIGHUP exit 130. A rate-limit wait inside an owned run
keeps draining on SIGTERM; Ctrl-C/SIGHUP interrupt it like the run. A launcher
error keeps its nonzero exit even during a drain. See the
[configuration reference](docs/configuration.md#top-level) for detailed wait rules
and the [coordination contract](docs/coordination.md#execution-boundaries) for
execution and cleanup boundaries.

The view closes and restores the terminal on launcher exit, including errors.
Ctrl-C in the view shows a centered Stopping screen through termination and cleanup,
including after `q`. Repeated `q` presses do nothing; repeated Ctrl-C presses on
the Stopping screen do nothing. Either key closes a standalone view immediately.
A view crash or kill restores the terminal and resumes plain output with one line
reporting the failure. Launcher and view reap their owned processes.

The launcher does not reload code; restart it after an upgrade or after checkout
refresh pulls code changes. An update banner above the terminal panes (or one
plain-output line) names the upgrade command for installed releases, or asks you
to restart when a normal control-checkout fetch finds newer code. See
[terminal view](docs/terminal-view.md#using-the-view) for update-check behavior.

Before upgrading, check the [changelog](CHANGELOG.md) and
[GitHub release notes](https://github.com/uberblick-ai/ub-agents/releases) for any
required configuration edits. Send SIGTERM and wait for the launcher to exit,
upgrade with `brew upgrade ub-agents` (or `git pull` for a development checkout),
then start `ub-agents launch` again. Under tmux, systemd or similar that restarts the
launcher automatically, upgrade first and then send SIGTERM. When a release says
launchers must be upgraded together, stop every launcher for the project before
upgrading any.

## Issue and PR approvals

Set top-level `approvals: on` or `approvals: off` in `ub-agents.yaml` to choose the
policy. By default it is on for public repositories and off for private and
internal repositories, resolved from GitHub visibility each discovery pass. An
unreadable visibility fails the pass without claims or parking writes. `doctor --verbose`
shows the effective value and source; `check` shows the configured value or the
visibility default without contacting GitHub.

With approvals off, a configured trigger label starts work regardless of who
applied it. Agents receive the current title and body, and comments, reviews and
review comments only from authors with `write`, `maintain` or `admin`. Other
feedback remains excluded even with an approval record. PR heads, including fork
heads, need no approval; the launcher makes no approval reads or approval-parking
writes. `ub-agents approve` still works, but its records have no effect. Other
eligibility gates, stop labels and fork revision restrictions still apply.

The following rules apply with approvals on.

People who start and approve work use their own accounts with `maintain` or `admin`.

Every issue run, including preparation, needs a maintainer start. The launcher
checks approvals at pickup and after claiming; disallowed input is parked without
spending an attempt and resumes after approval without `retry`. Agents receive the
post-claim title, body and trusted or cleared comments; PR context also includes
the assigned head, reviews and review comments. Other GitHub comments are not input.
Outside edits during a run do not stop it.

A maintainer adds `needs-preparation` to start an issue. The preparer adds `ready`,
and implementation follows without another approval. An outside title or body edit
parks the issue until a maintainer applies a trigger label again or runs
`ub-agents approve N`. Outside comments are not agent input until a
maintainer clears them.

When approval is the only pickup obstacle, `launch` adds the configured stop
label (`needs-human` in the starter) and posts one **Action needed** notice with
the steps to start or reapprove work. Follow those steps and remove the stop label
to resume; the next claim minimizes the notice. Unreadable history is retried
without parking writes, and `status` stays read-only.

Trusted-authored PRs need no start, and outside feedback cannot stall them.
Outside-authored PRs need both a maintainer trigger label and an approved head;
later outside edits or feedback suspend pickup. Maintainers approve current input
with `ub-agents approve N`, including a PR's head and outside feedback.
A maintainer approving review can approve its head; accepted agent revisions from
eligible heads in the base repository need no new approval. Every changed fork head
needs explicit maintainer approval. Fork PRs can be reviewed, but agent revision
runs remain blocked. The [approval contract](docs/approvals.md) describes the rules.

Use a dedicated launcher account with `write`; `doctor` warns about `maintain` or
`admin` accounts that would let agents start and approve their own work.

## Configure the agents

`ub-agents.yaml` lists the agents. In this example, review always runs on a different
model from the implementation:

```yaml
repository: your-org/your-project
shared-instructions: .agents/ub_agents.md

agents:
  issue-preparer:
    runtime: "claude:opus:high"
    trigger: needs-preparation
    outcomes:
      prepared: {add: [ready]}
      needs-human: {add: [needs-human]}
    instructions: .agents/issue-preparer.md
    worktree: true

  implementer:
    runtime: "codex:gpt-6.1-sol:high"
    trigger: [ready, needs-changes]
    outcomes:
      handed-off: {add: [needs-review]}
    instructions: .agents/implementer.md
    worktree: true

  reviewer:
    runtime: ["claude:opus:high", "codex:gpt-6.1-sol:high"]
    trigger: needs-review
    outcomes:
      approved: {add: [ready-to-merge]}
      changes-requested: {add: [needs-changes]}
    different-runtime-from: implementer
    instructions: .agents/reviewer.md
    worktree: true

  integrator:
    runtime: "claude:opus:high"
    trigger: ready-to-merge
    outcomes:
      merged: {}
      maintainer-merge: {add: [needs-human]}
      changes-requested: {add: [needs-changes]}
    instructions: .agents/integrator.md
    worktree: true
```

- **`trigger`**: the GitHub label, or labels, that start this agent.
- **`outcomes`**: named successful results with `add` and optional `remove` labels.
  The runner removes every trigger and any `remove` labels from the assignment,
  then adds `add` labels to the handoff PR, or to the assignment without a handoff.
- **`runtime`**: `cli:model:effort`. A list gives alternatives; the first one that is
  installed and allowed runs.
- **`different-runtime-from`**: run on a different CLI and model from the agent that
  produced the PR's current commit when its accepted report identifies the source.
  Wait for a pending handoff to finish. Otherwise, use only the first configured
  runtime; block if its CLI is unavailable.
- **`shared-instructions`**: optional top-level project policy file, read for every
  run before its role instructions. The starter uses `.agents/ub_agents.md`.
- **`instructions`**: the agent's task, in your words. `init` writes starters for the
  four roles above.
- **`worktree`**: run in a private checkout of the PR's exact commit, or on a fresh
  branch for an issue.
- **`retrospectives`**: optional positive discussion number in `repository`, such
  as `retrospectives: 203`. `check` validates it offline; `doctor` verifies the board
  on GitHub. Inside a run, use the literal launcher `report_command` followed by
  `retrospective --body-file PATH` to post a top-level comment and print its URL.
  The target is pinned for the agent, checked against the exact discussion URL,
  and unaffected by worktree configuration or `--config`. Posting changes no run
  outcome. The prompt mentions it only for configured agents, when a run lost
  something and the agent can name the change that would have prevented it.

An agent can also be a plain command instead of an LLM session.
[docs/configuration.md](docs/configuration.md) lists every option.

## The starter workflow

| Label | On | Next step |
|---|---|---|
| `needs-preparation` | Issue | A maintainer applies this label; prepare clear requirements. |
| `ready` | Issue | Implement it, publish draft checkpoints, then hand off the ready PR. |
| `needs-review` | PR | Review the current commit. |
| `needs-changes` | PR | Revise the implementation. |
| `ready-to-merge` | PR | Run final checks and merge under the project's policy. |
| `needs-human` | Either | Parked until a person decides. |

These are conventions, not built-ins. Rename them, drop review, or run a single agent
that only investigates issues.

The generated starter files are:

```text
your-project/
├── ub-agents.yaml
└── .agents/
    ├── ub_agents.md   # project loop policy, via shared-instructions
    ├── issue-preparer.md
    ├── implementer.md
    ├── reviewer.md
    └── integrator.md
```

Commit these files with your project and review changes to them like code. Credentials
stay in each tool's own login. `.ub-agents/`, which `init` adds to `.gitignore`,
holds `launch.log`, run logs and artifacts under `runs/`, private `worktrees/`,
and bounded local launcher snapshots under `sessions/`. The private snapshots
contain already observed issue text;
see [session publication](docs/configuration.md) for their format and lifecycle.

## When things go wrong

Before its first pass, `launch` requires a clean control checkout on the default
branch with no commits outside the local `origin` ref. `doctor` warns about the
same checkout conditions during setup; it does not fetch or fast-forward.

Before every new agent run, the launcher fetches `origin`, fast-forwards the
operator's control checkout on the repository's default branch, and rereads that
role's instruction file, shared policy and `ub-agents.yaml`. It replans the claim with the refreshed
configuration. Keep that checkout clean and free of local-only commits.
Unsafe checkout state, Git refresh failures, invalid configuration or invalid
instructions stop the launcher
with a nonzero exit and an actionable message; fix the checkout and restart.
No attempt is charged, and the assignment is not marked blocked or retrying.
Refresh happens between executions and cleanup hooks, never during a run or
durable-outcome recovery.
Instruction text and configuration stay fixed for each run; PR candidates are not
rebased. See [Stopping and restarting](#stopping-and-restarting) for signal handling
during checkout refresh and how to restart after code updates.

Each claim has a fixed 30-minute lease, renewed every 10 minutes throughout setup,
execution, completion and cleanup. If a launcher dies, its
claims expire and another launcher recovers the work: a recorded outcome is
validated and its label transition finished without rerunning the role.
`max-attempts` limits consecutive failures per item and configured agent. Accepted
success resets the count, including outcome-only recovery and PR revisions. An
operator interrupt preserves the count and allows pickup on the next launch without
backoff. An interrupt during completion leaves the reported outcome for expiry
recovery. Agent-reported `blocked` outcomes and human-paused transitions preserve the
count and park the item. A blocked PR becomes eligible when its head differs from
the head recorded at the block, without resetting attempts or other pickup gates.
Commits the run pushed before reporting blocked do not clear its own block;
issues and unchanged PR heads still need `ub-agents retry`.
Crashes without a report, timeouts, exits without a report
(zero or nonzero), setup failures and agent-reported `retry` increment it and retry
with backoff. Invalid success reports, unconfirmed cleanup and unclassified failures
increment it and park the item. `ub-agents retry` resets
the count to 0 and clears the failure block and backoff. It prints the reset record's
link, then explains whether the item is closed, needs stop labels removed or a
trigger label added, or can be picked up by a running launcher on its next poll.
It leaves labels unchanged; `ub-agents status` shows progress and other pickup gates.
The issue and handoff PR keep separate counts; `ub-agents status` shows the
consecutive failure count in `attempts`.
A success counts only after the launcher has checked the result on GitHub; an exit
code alone never does.
A report followed by a nonzero exit is validated and applied normally after
confirmed cleanup; the launcher logs the exit code.

When a run reports a Claude or Codex usage limit, its launcher pauses new runs
on that CLI in memory until reset plus one minute, without charging item attempts.
An unusable reset uses a 15-minute pause. Other CLIs and their runtime alternatives
keep working. Each pause start or changed end time prints the CLI and UTC end time
in launch output and `.ub-agents/launch.log`. Restarting the launcher clears its
pauses. See
[runtime usage pauses](docs/configuration.md#runtime-usage-pauses).

A stop label such as `needs-human` on the assignment or its handoff PR pauses a
transition before it starts. After removing it, set the workflow labels you want or
run `ub-agents retry`. The exact rules for claims, attempts, transitions and recovery
are in the [coordination contract](docs/coordination.md).

Private worktrees left by crashed runs and retained local branches can be inspected
with `ub-agents cleanup` and removed with `ub-agents cleanup --apply`. Removal needs
an eligible GitHub lease from a trusted account whose recorded host is this machine;
another or missing host, dirty trees, locked trees and uncertain artifacts stay.
Projects can configure a supervised [cleanup hook](docs/configuration.md#project-cleanup-hook)
for resources associated with each private worktree. Document operator-only recovery
steps in a project operations document linked from `AGENTS.md`.

Each run also gets a private scratch directory at
`$XDG_STATE_HOME/ub-agents/<owner>/<repo>/runs/<run>/scratch`, exposed through
`UB_AGENTS_SCRATCH` and `TMPDIR`. Unset, empty or relative `XDG_STATE_HOME` falls
back to `~/.local/state`. Scratch must be outside the target checkout and its
private worktrees; a path inside the checkout fails setup before the agent starts.
Use it for temporary files; the launcher removes it and its per-run directory after
confirmed process termination while retaining run logs. Removal failures leave a
diagnostic and any remaining scratch files without blocking run completion. See
[runtime permissions](docs/configuration.md#runtime-permissions)
for runtimes that need access outside their worktree.

## Development

For development, install from a checkout with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m tests
```

`python -m tests` runs the suite on every core; `-j N` sets the number of workers,
`--durations N` lists the slowest tests, and names such as `tests.test_loop` select
tests. `python -m unittest discover -v` runs the same tests one at a time.

With [mise](https://mise.jdx.dev) activated in your shell, entering the checkout does
this for you: it creates `.venv`, installs the checkout into it when missing or when
`pyproject.toml` changes, puts it on `PATH` and lists common commands. Run
`mise trust` once to allow it.

Tests use fakes for GitHub and real child processes for supervision; they never call a
model. This repository is developed with its own loop: see [ub-agents.yaml](ub-agents.yaml)
and [AGENTS.md](AGENTS.md). The roadmap is in the
[milestones](https://github.com/uberblick-ai/ub-agents/milestones).

The [local terminal view](docs/terminal-view.md) shows one launcher's local work,
cached context, outcomes and paged runtime logs in a separate process. On the Issue
tab, description bodies render as Markdown with inert links, and `g` can load a
missing title/body through `gh` on request.
Needs attention rows also have an Unblock tab showing their trusted action-needed
comment from the snapshot or an explicit `g` load, using the same inert Markdown
and bounded GitHub read rules.
The footer shows launcher activity and main keys; `?` lists all keys, and `p`
shows the full raw log path and retention diagnostics.

## License

MIT
