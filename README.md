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

`ub-agents launch` opens a live view of its own work. An example session:

```text
╭─ Work · pass complete ─────────────────────╮ ╭─ Log ─────────────────────────────────────────────────────────╮
│                                            │ │                                                               │
│  Running · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   1 Log  2 Issue  3 Runs │ Formatted  Raw                     │
│  ⠇ #248 Refresh the queue while an… 04:13  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│    implementer · this launcher · attempt…  │ │  #248 Refresh the queue while an agent runs                   │
│                                            │ │  implementer · claude opus high · attempt 1                   │
│  Needs attention · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  ? #255 Keep run scratch outside the… 19h  │ │   11:07:31 I'll read how the launcher polls before            │
│    implementer · needs-human · Decide wh…  │ │            changing anything.                                 │
│                                            │ │   11:07:51 ▸ Read src/ub_agents/polling.py                    │
│  Eligible · 2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   11:08:30 ▸ Read src/ub_agents/loop.py                       │
│  ● ⌥252 Drop inherited agent variab… next  │ │   11:09:10 The queue only refreshes between runs. I will      │
│    reviewer                                │ │            poll read-only while the agent works and claim     │
│                                            │ │            nothing until it is idle.                          │
│  ● #191 Shorter ub-agents doctor o… ready  │ │   11:09:30 ▸ Edit src/ub_agents/loop.py +5 -2                 │
│    implementer · 1/5 failures              │ │   11:10:09 ▸ Edit tests/test_loop.py +3 -1                    │
│  Recent activity · 4 today ┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   11:10:49 ▸ Bash python -m tests                             │
│  ✓ ⌥269 Send fixable check failur… merged  │ │   11:11:29 ▸ Bash git push -u origin ub-agents/248-implem…    │
│    integrator · 11:04 · Squash-merged af…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥269 Send fixable check fail… approved  │ │                                                               │
│    reviewer · 10:45 · Approved the curre…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥267 Release 0.1.15             merged  │ │                                                               │
│    integrator · 10:28 · Squash-merged af…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥264 Open the selected item fr… merged  │ │                                                               │
│    integrator · 10:11 · Squash-merged af…  │ │                                                               │
│                                            │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│                                            │ │  ⠧ implementer running · no outcome reported                  │
╰────────────────────────────────────────────╯ ╰───────────────────────────────────────────────────────────────╯
ub-agents · running assignment                                           ↑↓ select ⏎ open 1-3 tabs ? keys q quit
```

## Get started

You need macOS or Linux, `git`, `gh` logged in to an account with write access to
the repository, and the agent CLI you want to run: `codex` or `claude`.

```sh
cd your-project
ub-agents init --runtime claude:claude-opus-5-5:high   # or leave out --runtime for Codex
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

Before handoff, the implementer fetches the PR's base and fixes any merge conflicts.
If the reviewed PR is behind its base but merges cleanly, the integrator merges
the base into it, keeps the review and runs final checks at the new head.

These labels are conventions, not built-ins. Rename them, drop the review, or run
a single agent. Each role is a few lines of `ub-agents.yaml`:

```yaml
implementer:
  runtime: "claude:claude-opus-5-5:high"  # cli:model:effort
  trigger: [ready, needs-changes]         # labels that start it
  outcomes:
    handed-off: {add: [needs-review]}     # what each reported outcome does
  instructions: .agents/implementer.md
  worktree: true                          # private checkout for each run
```

The launcher removes the trigger label and applies the outcome's labels only after
it has checked the agent's report on GitHub. The
[configuration reference](docs/configuration.md) lists every key.

An optional per-agent [health check](docs/configuration.md#project-health-checks)
pauses new claims while a project dependency is unavailable, without spending
attempts. Claiming resumes automatically when the check passes.

Projects can configure [checkout setup](docs/configuration.md#checkout-setup) to
reinstall dependencies when the launcher pulls changes to a lockfile or tool
configuration. Setup runs before the role is claimed; failed setup stops launch
and retries on the next launch without spending an attempt.

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

In the [terminal view](docs/terminal-view.md), mouse selections copy on release;
`y` copies the current selection again. Recent activity scrolls independently of
live work to reach all 20 retained outcomes. Its header and any notice naming
older outcomes that were not retained stay in place while the rows scroll.

In-run commands and Python helpers use a copy of the launcher's code taken at
startup, so checkout refreshes and package upgrades leave active runs on the same
code. Restart the launcher to use an update.

When an agent needs a decision, the item stops with an **Action needed** comment
that lists the options and how to resume. PR run-outcome notices say merging or
closing finishes the item and fold the resume steps under "To send it back to
AGENT instead"; issue notices keep those steps visible. Missing blocked notices
are retried on later passes; Unblock shows the blocked reason and the same resume
structure while a notice is unavailable. GitHub failures while finalizing a stored
report leave it for [expiry recovery](docs/coordination.md#recovery) without
rerunning the agent.
Launcher output is also appended to
`.ub-agents/launch.log`.

**More machines.** Install ub-agents, clone the repository and run
`ub-agents launch`. Every account with write access can run a launcher; list them
in `launchers:` to allow only some ([launcher accounts](docs/configuration.md#launcher-accounts)).
A dedicated launcher account with `write` access is safest: `doctor` warns when an
account with `maintain` or `admin` could let agents start their own work.

**Upgrading.** Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases),
stop the launcher, run `brew update && brew upgrade ub-agents`, and start it again.
When a release says launchers must be upgraded together, stop all of them first.

## Retrospectives (optional)

Agents can tell you what slowed them down. Enable GitHub Discussions, open one
discussion per agent as its board, and give the agent its number:

```yaml
implementer:
  retrospectives: 203   # discussion number in this repository
```

When a run lost something and the agent can name the change that would have
prevented it, the agent posts a comment there through the launcher's
`retrospective` command. The launcher pins the board, and a post never changes
the run's outcome. Claude agents need `"Bash({report_command} retrospective *)"`
in `--allowedTools`, and `doctor` checks that the board exists. Read the boards
from time to time and fix the instructions they point at
([retrospectives](docs/configuration.md#agents)).

## Documentation

- [Configuration reference](docs/configuration.md): every key, runtime permissions and all commands
- [Running launchers](docs/operations.md): logs, stopping, upgrading and failure handling
- [Approvals](docs/approvals.md): who can start work and what agents may read
- [Coordination contract](docs/coordination.md): claims, attempts, outcomes and recovery
- [Terminal view](docs/terminal-view.md): the `launch` view and its keys

## Development

Development uses [mise](https://mise.jdx.dev), activated in your shell:

```sh
git clone https://github.com/uberblick-ai/ub-agents && cd ub-agents
mise trust                  # once; entering the checkout now installs it into .venv
ub-agents --version         # runs this checkout's code, not an installed release
.venv/bin/python -m tests   # the test suite
mise run ci <sha>           # maintainers: local CI for a pushed commit, posts signoff
```

Tests use fakes for GitHub and never call a model. This repository runs its own
loop: see [ub-agents.yaml](ub-agents.yaml) and [AGENTS.md](AGENTS.md). The roadmap
is in the [milestones](https://github.com/uberblick-ai/ub-agents/milestones).

## License

MIT
