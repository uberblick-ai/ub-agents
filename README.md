# ub-agents

ub-agents runs coding agents in an engineering loop driven by GitHub. Each project
defines its workflow; the launcher watches GitHub, claims matching work, runs the
configured agent, and records its outcome.

> Early stage. The starter workflow runs end to end on this repository. Packaged
> releases (Homebrew, PyPI) are planned in [#4](https://github.com/uberblick-ai/ub-agents/issues/4).

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

You need macOS or Linux, Python 3.11+, `git`, an authenticated `gh`, and the agent CLIs
you want to use, such as `codex` or `claude`. Every launcher for a project must
authenticate `gh` as the same GitHub account: only that account's coordination
comments count, so launchers on different accounts would not see each other's claims.

```sh
pipx install .        # from a checkout of this repository
cd your-project
ub-agent init         # starter ub-agent.yaml, AGENTS.md and .agents/ instructions
ub-agent doctor       # check the machine, GitHub labels/access and runtimes
ub-agent launch       # run the loop in the foreground; Ctrl-C stops it
```

The continuous loop retries transient GitHub discovery failures with bounded waits.
See [polling and retry limits](docs/configuration.md#top-level) for the fixed delays,
failure limit and rate-limit behavior. `launch --once` and `status` fail on the first
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
| `ub-agent check` | Validate the configuration files only |
| `ub-agent report` | Used by agents to record their outcome |

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
  produced the PR's current commit.
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
role's instruction file. Keep that checkout clean and free of local-only commits.
Unsafe checkout state, Git refresh failures or invalid instructions stop the launcher
with a nonzero exit and an actionable message; fix the checkout and restart.
No attempt is charged, and the assignment is not marked blocked or retrying.
Refresh happens between executions and cleanup hooks, never during a run or
durable-outcome recovery.
Instruction text stays fixed for each prompt. Configuration changes in
`ub-agent.yaml` still require a launcher restart; PR candidates are not rebased.

Each claim has a lease that outlasts the run's timeout. If a launcher dies, its
claims expire and another launcher recovers the work: a recorded outcome is
validated and its label transition finished without rerunning the role.
`max-attempts` limits consecutive failures per item and configured agent. Accepted
success resets the count, including outcome-only recovery and PR revisions. An
operator interrupt preserves the count and allows pickup on the next launch without
backoff. Agent-reported `blocked` outcomes and human-paused transitions preserve the
count and park the item. Crashes without a report, timeouts, missing reports after
exit zero, setup failures and agent-reported `retry` increment it and retry with
backoff. Invalid success reports, nonzero exits without a report, unconfirmed cleanup
and unclassified failures increment it and park the item. `ub-agent retry` resets
the count to 0 and clears the parked state. The issue and handoff PR keep separate
counts; `ub-agent status` shows the consecutive failure count in `attempts`.
A success counts only after the launcher has checked the result on GitHub; an exit
code alone never does.

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
