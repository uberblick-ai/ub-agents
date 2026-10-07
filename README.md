# ub-agents

**The loop engineering framework for GitHub.** Any agent, no server.

```sh
brew install uberblick-ai/tap/ub-agents
```

## What is ub-agents?

ub-agents runs coding agents on your GitHub issues and pull requests. You start a
launcher in your project's checkout. It watches the repository, claims an issue or
PR whose label matches one of your agents, runs that agent, and applies the outcome
the agent reports as new labels. Then it picks the next item.

- **Labels are the queue.** Put `ready` on an issue and the implementer picks it up.
  Its pull request gets `needs-review`, and the reviewer takes over. You decide what
  each label means and what happens after each outcome.
- **GitHub is the only state.** Claims, attempts and outcomes are comments on the
  issue or PR. A restarted launcher rebuilds everything from there. There is no
  database and nothing to host.
- **Use the agents you already have.** Each role runs Claude Code or Codex with the
  model you choose, or any command. Its instructions are files in your repository,
  reviewed like code.
- **Run it anywhere.** Launchers on several machines, yours and your teammates',
  share one queue. A claim keeps two launchers off the same item.

ub-agents is MIT-licensed and early stage. It develops itself with its own loop.

## Get started

You need macOS or Linux, `git`, `gh` logged in to an account with write access to
the repository, and the agent CLI you want to run: `codex` or `claude`.

```sh
cd your-project
ub-agents init --runtime claude:opus:high   # or leave out --runtime for Codex
```

`init` writes the starter files and offers to create the workflow labels on GitHub:

```text
your-project/
├── ub-agents.yaml          # agents, their triggers and outcomes
└── .agents/
    ├── ub_agents.md        # project policy every agent reads
    ├── issue-preparer.md
    ├── implementer.md
    ├── reviewer.md
    └── integrator.md
```

Before the first launch:

1. Fill in `.agents/ub_agents.md`: your checks, who may merge, and who answers
   questions. Keep build and test commands in the `AGENTS.md` or `CLAUDE.md` your
   agents already read.
2. Give the agents permissions. `init` offers starter permissions; otherwise
   uncomment `runtime-args` in `ub-agents.yaml`. Claude agents also need your
   check commands in `--allowedTools`.
3. Check the setup, commit and launch:

```sh
ub-agents check
git add ub-agents.yaml .agents .gitignore
git commit -m "Add ub-agents" && git push
ub-agents doctor     # machine, GitHub access, labels and agent CLIs
ub-agents launch     # runs the loop and opens the terminal view
```

Now add `needs-preparation` to an issue. In a public repository, only a label added
by someone with `maintain` or `admin` access starts work on an issue, and outside
edits wait for their approval ([approvals](docs/approvals.md)).

## The starter workflow

```text
needs-preparation ──▶ issue-preparer ──▶ ready, or needs-human
ready ──────────────▶ implementer ─────▶ PR with needs-review
needs-review ───────▶ reviewer ────────▶ ready-to-merge, or needs-changes
needs-changes ──────▶ implementer ─────▶ needs-review
ready-to-merge ─────▶ integrator ──────▶ merged, needs-changes or needs-human
needs-human ────────▶ waits for a person
```

These labels are conventions, not built-ins. Rename them, drop the review, or run
a single agent. Each role is a few lines of `ub-agents.yaml`:

```yaml
implementer:
  runtime: "claude:opus:high"          # cli:model:effort
  trigger: [ready, needs-changes]      # labels that start it
  outcomes:
    handed-off: {add: [needs-review]}  # what each reported outcome does
  instructions: .agents/implementer.md
  worktree: true                       # private checkout for each run
```

The launcher removes the trigger label and applies the outcome's labels only after
it has checked the agent's report on GitHub. The
[configuration reference](docs/configuration.md) lists every key.

## Day to day

| Command | What it does |
|---|---|
| `ub-agents launch` | Watch the queue and run work; `q` stops after the current run, Ctrl-C stops now |
| `ub-agents launch 214` | Run only issue or PR 214, then exit |
| `ub-agents status [214]` | Show the queue, or why one item is waiting |
| `ub-agents retry 214 --reason "…"` | Let an item that failed or blocked run again |
| `ub-agents approve 214` | Approve outside edits or a contributor's PR as agent input |
| `ub-agents cleanup [--apply]` | Preview or remove worktrees left by crashed runs |
| `ub-agents help COMMAND` | Show a command's options and examples |

When an agent needs a decision, the item stops with an **Action needed** comment
that lists the options and how to resume. Launcher output is also appended to
`.ub-agents/launch.log`.

**More machines.** Install ub-agents, clone the repository and run
`ub-agents launch`. Every account with write access can run a launcher; list them
in `launchers:` to allow only some ([launcher accounts](docs/configuration.md#launcher-accounts)).
A dedicated launcher account with `write` access is safest: `doctor` warns when an
account with `maintain` or `admin` could let agents start their own work.

**Upgrading.** Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases),
stop the launcher, run `brew upgrade ub-agents`, and start it again. When a release
says launchers must be upgraded together, stop all of them first.

## Documentation

- [Configuration reference](docs/configuration.md): every key, runtime permissions and all commands
- [Running launchers](docs/operations.md): logs, stopping, upgrading and failure handling
- [Approvals](docs/approvals.md): who can start work and what agents may read
- [Coordination contract](docs/coordination.md): claims, attempts, outcomes and recovery
- [Terminal view](docs/terminal-view.md): the `launch` view and its keys

## Development

From a checkout, with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m tests
```

With [mise](https://mise.jdx.dev), run `mise trust` once and entering the checkout
sets this up. Tests use fakes for GitHub and never call a model. This repository
runs its own loop: see [ub-agents.yaml](ub-agents.yaml) and [AGENTS.md](AGENTS.md).
The roadmap is in the [milestones](https://github.com/uberblick-ai/ub-agents/milestones).

## License

MIT
