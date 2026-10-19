# ub-agents

**An autonomous coding loop for GitHub.** Any agent, no server.

```sh
brew install uberblick-ai/tap/ub-agents
```

## What is ub-agents?

Your agents prepare issues, implement them, review the pull requests and send work
back when it needs fixing. When a decision needs a person, they raise a hand and
wait. You define the roles and rules; ub-agents handles the handoffs.

- **Labels are the queue.** A launcher on your machine runs the agent whose label
  matches each issue or PR. The agent's outcome moves the labels on to the next role.
- **GitHub is the only state.** Claims, attempts and outcomes are comments on the
  issue or PR. A restarted launcher rebuilds everything from there. There is no
  database and nothing to host.
- **Use the agents you already have.** Each role runs Claude Code or Codex with the
  model you choose, or any command. Its instructions are files in your repository,
  reviewed like code.
- **Add loops to go faster.** Every launcher, on a laptop, a teammate's machine or
  an always-on server, works from the same queue, and a claim keeps two launchers
  off the same item.

Under the hood it is a glorified script that polls labels and calls an agent. The
rest is handling duplicate claims, interrupted runs, endless review loops, comments
from strangers and GitHub failures. ub-agents is MIT licensed, early stage, and
develops itself with its own loop. [Why it works this way](https://agents.uberblick.ai/docs/motivation.html).

`ub-agents launch` opens a live view of its own work:

```text
╭─ Work · pass complete ─────────────────────╮ ╭─ Agent ───────────────────────────────────────────────────────╮
│                                            │ │                                                               │
│  Running · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   1 Agent  2 Issue  3 Runs │ Formatted  Raw (u)               │
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
│  Recent activity · showing 4 ┄┄┄┄┄┄┄┄┄┄┄┄  │ │   11:10:49 ▸ Bash python -m tests                             │
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
ub-agents · running assignment                                                            1-3 tabs ? keys q quit
```

## A real run

On 19 October a user reported that `ub-agents launch` rejected their `origin`
because it used an SSH host alias ([#597](https://github.com/uberblick-ai/ub-agents/issues/597)).
Once the issue was labeled `needs-preparation`, the loop took it from there (UTC):

```text
14:19  issue-preparer  claude  found the issue small and clear, marked it ready
14:21  implementer     codex   changed 7 files, ran 2,109 tests, opened #598
14:33  reviewer        claude  reviewed against the acceptance criteria, approved
14:38  integrator      claude  confirmed CI and the merge policy
14:41  #598 merged, #597 closed
```

Twenty-two minutes from the first agent to the merge, with no person in between.
Every step is a comment on [#597](https://github.com/uberblick-ai/ub-agents/issues/597)
and [#598](https://github.com/uberblick-ai/ub-agents/pull/598).

## The starter workflow

`ub-agents init` sets up this workflow:

```text
needs-preparation  issue-preparer  → ready, or needs-human to ask
ready              implementer     → a pull request with needs-review
needs-review       reviewer        → ready-to-merge, or needs-changes → implementer
ready-to-merge     integrator      → merged, or needs-human for a maintainer
```

Each role is a few lines of `ub-agents.yaml`:

```yaml
implementer:
  runtime: "codex:gpt-6.1-sol:high"   # cli:model:effort
  trigger: [ready, needs-changes]     # labels that start it
  outcomes:
    handed-off: {add: [needs-review]}  # what each reported outcome does
  instructions: .agents/implementer.md
  worktree: true                      # private checkout for each run
```

The preparer can also ask a fifth role, the issue-reviewer, to check scope before
work starts. The labels are conventions, not built-ins. Rename them, drop the review, add a
step or run a single agent. Review rounds are capped, so a disagreement ends with a
person instead of an endless loop. Agents can post retros about what they were
missing, so you fix the instructions once. See the
[configuration docs](https://agents.uberblick.ai/docs/configuration/index.html).

## Get started

You need macOS or Linux, `git`, `gh` logged in to an account with write access to
the repository, and the agent CLI you want to run: `claude` or `codex`.

```sh
cd your-project
ub-agents init      # asks a few questions, then opens a setup PR
```

Review the setup PR before merging: the merge policy and who answers questions in
`.agents/ub_agents.md`, your check commands, and the agents' permissions. Then, from
a clean checkout of your default branch:

```sh
ub-agents doctor    # machine, GitHub access, labels and agent CLIs
ub-agents launch    # runs the loop and opens the terminal view
```

Add `needs-preparation` to an issue to start it. See
[installation](https://agents.uberblick.ai/docs/installation.html) and
[init](https://agents.uberblick.ai/docs/commands/init.html).

## Before you run it

- **Agents run on your machine with the access you give their CLI.** ub-agents adds
  no permission flags; you grant each role what it needs
  ([permissions](https://agents.uberblick.ai/docs/configuration/permissions.html)).
  Work happens in worktrees and branches, never on main.
- **Give the launcher its own GitHub account** with write access, not admin
  ([launcher account](https://agents.uberblick.ai/docs/best-practices/launcher-account.html)).
- **Public repositories** need a maintainer's label before outside issues and PRs
  reach agents. Fork PRs are reviewed as a diff and never run on a launcher
  ([public repos](https://agents.uberblick.ai/docs/guides/public-repos.html)).
- **One GitHub account serves about ten launchers** under GitHub's rate limits
  ([account capacity](https://agents.uberblick.ai/docs/best-practices/account-capacity.html)).

## Day to day

| Command | What it does |
|---|---|
| `ub-agents launch` | Watch the queue and run work; `q` stops after the current run, Ctrl-C stops now |
| `ub-agents launch 214` | Run only issue or PR 214, then exit |
| `ub-agents status [214]` | Show the queue, or why one item is waiting |
| `ub-agents retry 214 --reason "…"` | Let an item that failed or blocked run again |
| `ub-agents help COMMAND` | Show a command's options and examples |

When an agent needs a decision, the item stops with an **Action needed** comment
that lists the options and how to resume. More capacity is one more
`ub-agents launch` on another machine
([add more launchers](https://agents.uberblick.ai/docs/guides/add-a-launcher.html)).
To upgrade, read the [release notes](https://github.com/uberblick-ai/ub-agents/releases),
stop the launcher, `brew upgrade ub-agents`, and start it again.

## Documentation

The guides live at [agents.uberblick.ai](https://agents.uberblick.ai/docs/installation.html).
Reference material in this repository:

- [Configuration reference](docs/configuration.md): every key and all commands
- [Running launchers](docs/operations.md): logs, cleanup, stopping and failure handling
- [Approvals](docs/approvals.md): who can start work and what agents may read
- [Coordination contract](docs/coordination.md): claims, attempts, outcomes and recovery
- [Terminal view](docs/terminal-view.md): the `launch` view and its keys

## Development

Development uses [mise](https://mise.jdx.dev), activated in your shell:

```sh
git clone https://github.com/uberblick-ai/ub-agents && cd ub-agents
mise trust                  # once; entering the checkout now installs it into .venv
.venv/bin/python -m tests   # the test suite; -j N workers, --durations N slowest
```

Tests use fakes for GitHub and never call a model. This repository runs its own
loop: see [ub-agents.yaml](ub-agents.yaml) and [AGENTS.md](AGENTS.md). The roadmap
is in the [milestones](https://github.com/uberblick-ai/ub-agents/milestones), and
releases are described in [publishing releases](docs/releases.md).

## License

MIT
