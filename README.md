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
  rank priorities and gate new issues by milestone. New issues wait for open
  GitHub blockers by default; blockers inherit priority from open local
  dependents. PRs inherit priority from the open issues they close. Defaults use
  FIFO and ignore milestones.
- **GitHub is the state.** Claims, attempts and outcomes are comments on the issue or
  PR. A restarted launcher rebuilds everything from GitHub; there is no separate
  database or service.
- **The project owns workflow policy.** You choose what labels mean and what should
  happen after each role finishes. The launcher matches triggers, supervises runs,
  retries and validates handoffs, then applies the label transition declared for
  the role's reported outcome.

## Use it in your project

You need macOS or Linux, `git`, an authenticated `gh`, and the agent CLIs you want to
use, such as `codex` or `claude`. Homebrew installs Python and `gh`; checkout installs
require Python 3.11+. Every launcher for a project must
authenticate `gh` as the same GitHub account: only that account's coordination
comments count, so launchers on different accounts would not see each other's claims.

```sh
brew install uberblick-ai/tap/ub-agents
cd your-project
ub-agent init         # starter ub-agent.yaml, AGENTS.md and .agents/ instructions
ub-agent doctor       # check the machine, GitHub labels/access and runtimes
ub-agent launch       # run the loop in the foreground; Ctrl-C stops it
```

Launch output is flushed immediately to the terminal and appended to
`.ub-agent/launch.log` in the control checkout, with a UTC timestamp on each file
line, including stop and error messages. This also applies to `launch --once`;
the log is never truncated or rotated. Follow it from another terminal with
`tail -f .ub-agent/launch.log`. See [Stopping and restarting](#stopping-and-restarting)
for signal handling, including during a GitHub request.

The continuous loop retries transient GitHub discovery failures with bounded waits.
Continuous launch waits out GitHub rate limits without charging poll failures or
item attempts. Owned runs retry rate-limited reads while the lease permits; writes
keep their existing handling. `doctor` reports request quota and reset time in UTC,
warning below 10% remaining. See [polling and retry limits](docs/configuration.md#top-level)
for the waits and failure limit. `launch --once` and `status` fail on the first
error.

In an interactive terminal, `init` explains the missing workflow labels and offers
to create them; the default is no. Otherwise it prints runnable `gh label create`
commands. Noninteractive runs make no GitHub calls beyond repository inference.
Existing labels are never changed. `doctor` fails for missing trigger or transition
labels and warns for missing stop labels.

Before launching, create any missing labels, fill in the project checks in
`AGENTS.md`, and uncomment or customize each agent's starter `runtime-args` to grant
the [permissions](docs/configuration.md#runtime-permissions) its job needs. These
examples match `init --runtime` and remain commented out until you enable them.
`doctor` warns for each runtime agent without arguments. Commit `ub-agent.yaml`,
`AGENTS.md` and `.agents/`. `init` preserves an existing `AGENTS.md`.

Customize these parts:

- In `ub-agent.yaml`, set each role's trigger labels, CLI and model, runtime
  permissions, worktree choice, named outcomes and their label transitions, and
  any labels that should pause all work.
- In `.agents/<role>.md` and shared guidance such as `AGENTS.md`, define each role's
  task, project checks, successful handoff, and questions that need a
  person. Humans own priority and human-only decisions.
- In GitHub, create the labels and set branch protection or required reviews that
  match your merge policy.

A trigger selects work. An agent reports a declared outcome with
`ub-agent report --outcome NAME --summary TEXT [--handoff PR]`; the launcher validates
it and applies the project's transition. `--status retry|blocked` changes no labels.
Review the [coordination contract](docs/coordination.md) and
[configuration reference](docs/configuration.md) for recovery and permissions.

| Command | What it does |
|---|---|
| `ub-agent status` | Show matching work, who owns it, and what it reported |
| `ub-agent launch --once` | Run at most one assignment, then exit |
| `ub-agent cleanup [--apply]` | Preview stale private worktrees and local branches; apply eligible removals |
| `ub-agent retry` | Let stopped work run again, with a recorded reason |
| `ub-agent approve --number N` | Print current issue or PR input and post a maintainer [approval record](docs/approvals.md) |
| `ub-agent check` | Validate the configuration files only |
| `ub-agent report` | Used by agents to record their outcome |

## Stopping and restarting

| Signal | Effect | Exit |
|---|---|---|
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

The launcher does not reload code; restart it after an upgrade or after checkout
refresh pulls code changes.

Before upgrading, check the [changelog](CHANGELOG.md) and
[GitHub release notes](https://github.com/uberblick-ai/ub-agents/releases) for any
required configuration edits. Send SIGTERM and wait for the launcher to exit,
upgrade with `brew upgrade ub-agents` (or `git pull` for a development checkout),
then start `ub-agent launch` again. Under tmux, systemd or similar that restarts the
launcher automatically, upgrade first and then send SIGTERM. When a release says
launchers must be upgraded together, stop every launcher for the project before
upgrading any.

## Issue and PR approvals

Every issue run, including preparation, needs a maintainer start. The launcher
checks approvals at pickup and after claiming; disallowed input is parked without
spending an attempt and resumes after approval without `retry`. Agents receive the
post-claim title, body and trusted or cleared comments; PR context also includes
the assigned head, reviews and review comments. Other GitHub comments are not input.
Outside edits during a run do not stop it.

When approval is the only pickup obstacle, `launch` adds the configured stop
label (`needs-human` in the starter) and posts one **Action needed** notice with
the steps to start or reapprove work. Follow those steps and remove the stop label
to resume; the next claim minimizes the notice. Unreadable history is retried
without parking writes, and `status` stays read-only.

Trusted-authored PRs need no start, and outside feedback cannot stall them.
Outside-authored PRs need both a maintainer trigger label and an approved head;
later outside edits or feedback suspend pickup. Maintainers approve current input
with `ub-agent approve --number N`, including a PR's head and outside feedback.
A maintainer approving review can approve its head; accepted agent revisions from
eligible heads in the base repository need no new approval. Every changed fork head
needs explicit maintainer approval. Fork PRs can be reviewed, but agent revision
runs remain blocked. The [approval contract](docs/approvals.md) describes the rules.

Use a dedicated launcher account with `write`; `doctor` warns about `maintain` or
`admin` accounts that would let agents start and approve their own work.

## Configure the agents

`ub-agent.yaml` lists the agents. In this example, review always runs on a different
model from the implementation:

```yaml
repository: your-org/your-project

agents:
  issue-preparer:
    runtime: "claude:opus:high"
    trigger: needs-preparation
    outcomes:
      prepared: {add: [ready]}
      needs-human: {add: [needs-human]}
    instructions: .agents/issue-preparer.md

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
- **`instructions`**: the agent's task, in your words. `init` writes starters for the
  four roles above.
- **`worktree`**: run in a private checkout of the PR's exact commit, or on a fresh
  branch for an issue.

An agent can also be a plain command instead of an LLM session.
[docs/configuration.md](docs/configuration.md) lists every option.

## The starter workflow

| Label | On | Next step |
|---|---|---|
| `needs-preparation` | Issue | Turn the request into clear requirements. |
| `ready` | Issue | Implement it, publish draft checkpoints, then hand off the ready PR. |
| `needs-review` | PR | Review the current commit. |
| `needs-changes` | PR | Revise the implementation. |
| `ready-to-merge` | PR | Run final checks and merge under the project's policy. |
| `needs-human` | Either | Parked until a person decides. |

These are conventions, not built-ins. Rename them, drop review, or run a single agent
that only investigates issues.

Your project contains:

```text
your-project/
├── ub-agent.yaml
├── AGENTS.md          # shared guidance; init preserves an existing file
└── .agents/
    ├── issue-preparer.md
    ├── implementer.md
    ├── reviewer.md
    └── integrator.md
```

Commit these files with your project and review changes to them like code. Credentials
stay in each tool's own login. Logs and worktrees live under `.ub-agent/`, which
`init` adds to `.gitignore`.

## When things go wrong

Before every new agent run, the launcher fetches `origin`, fast-forwards the
operator's control checkout on the repository's default branch, and rereads that
role's instruction file and `ub-agent.yaml`. It replans the claim with the refreshed
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

Each claim has a lease that outlasts the run's timeout. If a launcher dies, its
claims expire and another launcher recovers the work: a recorded outcome is
validated and its label transition finished without rerunning the role.
`max-attempts` limits consecutive failures per item and configured agent. Accepted
success resets the count, including outcome-only recovery and PR revisions. An
operator interrupt preserves the count and allows pickup on the next launch without
backoff. An interrupt during completion leaves the reported outcome for expiry
recovery. Agent-reported `blocked` outcomes and human-paused transitions preserve the
count and park the item. Crashes without a report, timeouts, exits without a report
(zero or nonzero), setup failures and agent-reported `retry` increment it and retry
with backoff. Invalid success reports, unconfirmed cleanup and unclassified failures
increment it and park the item. `ub-agent retry` resets
the count to 0 and clears the parked state. The issue and handoff PR keep separate
counts; `ub-agent status` shows the consecutive failure count in `attempts`.
A success counts only after the launcher has checked the result on GitHub; an exit
code alone never does.
A report followed by a nonzero exit is validated and applied normally after
confirmed cleanup; the launcher logs the exit code.

A stop label such as `needs-human` on the assignment or its handoff PR pauses a
transition before it starts. After removing it, set the workflow labels you want or
run `ub-agent retry`. The exact rules for claims, attempts, transitions and recovery
are in the [coordination contract](docs/coordination.md).

Private worktrees left by crashed runs and retained local branches can be inspected
with `ub-agent cleanup` and removed with `ub-agent cleanup --apply`. Removal needs
an actor-owned, eligible GitHub lease; dirty, locked or uncertain artifacts stay.
Projects can configure a supervised [cleanup hook](docs/configuration.md#project-cleanup-hook)
for resources associated with each private worktree. Document operator-only recovery
steps in a project operations document linked from `AGENTS.md`.

## Development

For development, install from a checkout with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -v
```

Tests use fakes for GitHub and real child processes for supervision; they never call a
model. This repository is developed with its own loop: see [ub-agent.yaml](ub-agent.yaml)
and [AGENTS.md](AGENTS.md). The roadmap is in the
[milestones](https://github.com/uberblick-ai/ub-agents/milestones).

## License

MIT
