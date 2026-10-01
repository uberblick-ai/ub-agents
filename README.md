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
  rank priorities and gate new issues by milestone. Defaults use FIFO and ignore
  milestones.
- **GitHub is the state.** Claims, attempts and outcomes are comments on the issue or
  PR. A restarted launcher rebuilds everything from GitHub; there is no separate
  database or service.
- **The project owns workflow policy.** You choose what labels mean and what should
  happen after each role finishes. The launcher matches triggers, supervises runs,
  retries and validates handoffs; today, the role follows your instructions to make
  the label changes before reporting success.

## Use it in your project

You need macOS or Linux, Python 3.11+, `git`, an authenticated `gh`, and the agent CLIs
you want to use, such as `codex` or `claude`.

```sh
pipx install .        # from a checkout of this repository
cd your-project
ub-agent init         # starter ub-agent.yaml and .agents/ instructions
ub-agent doctor       # check the machine, GitHub access and runtimes
ub-agent launch       # run the loop in the foreground; Ctrl-C stops it
```

Before launching, create the workflow labels on GitHub, give each runtime the
[permissions](docs/configuration.md#runtime-permissions) its job needs, and commit
`ub-agent.yaml` and `.agents/`.

Customize these parts:

- In `ub-agent.yaml`, set each role's trigger labels, CLI and model, runtime
  permissions, worktree choice, and any labels that should pause all work.
- In `.agents/<role>.md` and shared guidance such as `AGENTS.md`, define each role's
  task, project checks, successful handoff, label changes, and questions that need a
  person. Humans own priority and human-only decisions.
- In GitHub, create the labels and set branch protection or required reviews that
  match your merge policy.

A trigger selects work; it does not define the whole label lifecycle. Today, roles
make project-approved label changes before reporting `success`, `retry` or `blocked`;
the launcher validates and records the result but does not choose the next label.
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
    instructions: .agents/issue-preparer.md

  implementer:
    runtime: "codex:gpt-6.1-sol:high"
    trigger: [ready, needs-changes]
    instructions: .agents/implementer.md
    worktree: true

  reviewer:
    runtime: ["claude:opus:high", "codex:gpt-6.1-sol:high"]
    trigger: needs-review
    different-runtime-from: implementer
    instructions: .agents/reviewer.md
    worktree: true

  integrator:
    runtime: "claude:opus:high"
    trigger: ready-to-merge
    instructions: .agents/integrator.md
    worktree: true
```

- **`trigger`**: the GitHub label, or labels, that start this agent.
- **`runtime`**: `cli:model:effort`. A list gives alternatives; the first one that is
  installed and allowed runs.
- **`different-runtime-from`**: run on a different CLI, provider and model from the
  agent that produced the PR's current commit.
- **`instructions`**: the agent's task, in your words. `init` writes starters for the
  four roles above.
- **`worktree`**: run in a private checkout of the PR's exact commit, or on a fresh
  branch for an issue.

An agent can also be a plain command instead of an LLM session, and other agent CLIs
can be added with a small adapter. [docs/configuration.md](docs/configuration.md)
lists every option.

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
├── AGENTS.md          # optional guidance shared by all agents
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

Each claim has a lease that the launcher renews while the agent runs. If a launcher
dies, its claims expire and a launcher can recover the work. Timeouts, interruptions
and failures the agent reports as `retry` are retried with backoff, up to
`max-attempts`. Other failures stop the item until a person runs `ub-agent retry`. A
success counts only after the launcher has checked the result on GitHub; an exit code
alone never does.

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
