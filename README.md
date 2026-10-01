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
you want to use, such as `codex` or `claude`.

```sh
pipx install .        # from a checkout of this repository
cd your-project
ub-agent init         # starter ub-agent.yaml, AGENTS.md and .agents/ instructions
ub-agent doctor       # check the machine, GitHub labels/access and runtimes
ub-agent launch       # run the loop in the foreground; Ctrl-C stops it
```

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

Each claim has a lease that outlasts the run's timeout. If a launcher dies, its
claims expire and a launcher can recover the work. Timeouts, interruptions
and failures the agent reports as `retry` are retried with backoff, up to
`max-attempts`. Other failures stop the item until a person runs `ub-agent retry`. A
success counts only after the launcher has checked the result on GitHub; an exit code
alone never does.

A stop label on the assignment or handoff PR before a transition starts blocks
the run without changing labels. Removing it does not revive that outcome: set
the desired workflow labels or use `ub-agent retry` to rerun the role. A stop label
added after a transition starts stays in place while the transition completes.
Interrupted transitions recover their recorded changes without rerunning the role
or consuming another attempt. Assignment labels are removed before destination
labels are added; a crash between those steps leaves items idle when no other
trigger is present. Transitions create no cross-item reservations. If an operator
reset supersedes recovery, inspect both items and restore the desired triggers.
Once a transition starts, recovery finishes it even if the PR head or issue link
changes. Provenance retains the original SHA; a newer head still needs its own
implementation outcome before an independent review can run.

The exact rules for claims, attempts and recovery are in the
[coordination contract](docs/coordination.md).

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
