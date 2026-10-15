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
│  Running · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   1 Log  2 Issue  3 Runs │ Formatted  Raw (u)                 │
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

The item tab bar shows `│ Formatted  Raw (u)` on every tab. Press `u` to toggle
the selected log's mode; the active label stays accented (Formatted underlined,
Raw reversed). The muted shortcut shortens to `u` when space is tight, including
the 60-column item view with all four tabs. Clicking the indicator has no effect.

The footer keeps navigation hints brief: `1-3 tabs ? keys q quit` in follow mode,
or `? keys q quit` in the narrow Work list. Arrow keys still select rows and
`Enter` opens them; `?` lists all keys. Attached macOS views offer
`r poll now c Stay awake ? keys` when there is room. With keep awake enabled,
the footer reads `ub-agents v0.2.1 · ☕ · next poll 26s`, or
`v0.2.1 · ☕ · poll 26s` in the narrow layout. The `☕` indicator uses two terminal
cells and stays visible as the `c Stay awake` hint drops first.

## Get started

You need macOS or Linux, `git`, `gh` logged in to an account with write access to
the repository, and the agent CLI you want to run: `codex` or `claude`.

```sh
cd your-project
ub-agents init
# Or preselect a runtime:
ub-agents init --runtime claude:claude-opus-5-5:high
```

In a terminal, `init` checks your machine, preselects the detected, logged-in CLI
(Codex when both are logged in), and asks which agent runs the loop, whether
agents may work without asking, and which extras you want (priorities, retrospectives,
Uberblick). It writes nothing until you confirm **Write setup?**. Agents always work
in worktrees and branches; they never commit to main. See the
[init command guide](https://agents.uberblick.ai/docs/commands/init.html).
The starter files are:

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

For unattended setup, including in a terminal:

```sh
ub-agents init --use-defaults --repository org/project
```

CI and non-TTY runs also use defaults without prompts. Without `--runtime`, they
use Codex. They keep permissions and updates commented out, leave the extras off,
keep the `@org/maintainers` placeholder, print label commands and open no PR.

Before the first launch:

1. Review `.agents/ub_agents.md`: your checks, who may merge, and who answers
   questions. Keep build and test commands in the `AGENTS.md` or `CLAUDE.md` your
   agents already read.
2. Give the agents permissions. If you didn't let init grant them,
   uncomment the top-level `runtime-args` mapping in `ub-agents.yaml`. It supplies
   defaults per CLI; an agent's own `runtime-args` replaces them. Claude agents
   also need your check commands in `--allowedTools`.
3. On the default branch, interactive init opens a setup PR: review the files there
   and merge it. Off the default branch or unattended, commit the local files
   through your project's PR workflow. After they merge, use a clean
   default-branch launcher checkout and launch:

```sh
ub-agents doctor --offline  # local validation without GitHub or agent CLIs
ub-agents doctor     # machine, GitHub access, labels and agent CLIs
ub-agents launch     # runs the loop and opens the terminal view
```

Setup success does not establish launch readiness. Launch fetches and fast-forwards
the control checkout before runs; use a separate checkout if you also code here.

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
If GitHub reports an approved PR as conflicting, the launcher sends it back to the
implementer with `needs-changes` instead of starting the integrator.
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

For machine-specific settings, put an untracked `ub-agents.local.yaml` beside the
required base file. With `--config PATH`, the local file is read from the resolved
base file's directory, never an unrelated working directory. Local mappings merge
recursively; supplied scalars and lists replace the entire base value. For example,
if the base implementer uses `codex:gpt-6.1-sol:high`, keep that alternative by
repeating it in the local replacement list:

```yaml
# ub-agents.local.yaml
agents:
  implementer:
    runtime: ["codex:gpt-6.1-sol:high", "codex:machine-model:high"]
```

The implementer's other settings and other agents inherit from the base. Missing,
empty or comment-only local files leave it unchanged. `doctor`, `init`
validation and launch use the effective settings; launch rereads both files before
each run, so adding, editing or removing the local file takes effect at the next
reload. It stays in the control checkout and is not copied into private worktrees.
`init` adds its ignore entry; existing projects should add `ub-agents.local.yaml`
to `.gitignore` before creating it. See [local overrides](docs/configuration.md#machine-local-overrides)
for validation and replacement rules.

An optional per-agent [health check](docs/configuration.md#project-health-checks)
pauses new claims while a project dependency is unavailable, without spending
attempts. Claiming resumes automatically when the check passes.

Projects can configure [checkout setup](docs/configuration.md#checkout-setup) to
reinstall dependencies when the launcher pulls changes to a lockfile or tool
configuration. Control-checkout setup runs before the role is claimed; a failure
stops launch and retries on the next launch without spending an attempt. Each new
private worktree also runs setup once before its agent starts.

## Day to day

| Command | What it does |
|---|---|
| `ub-agents launch` | Watch the queue and run work; `q` stops after the current run, Ctrl-C stops now |
| `ub-agents launch --agent issue-preparer` | Serve only that configured agent under the normal queue gates; stop manually before launching the normal queue |
| `ub-agents launch 214` | Run only issue or PR 214, skipping queue priority and milestone policy while applying all other gates, then exit |
| `ub-agents status [214]` | Show the queue, stopped items' Action needed links and waiting times, or why one item is waiting |
| `ub-agents retry 214 --reason "…"` | Let an item that failed or blocked run again |
| `ub-agents help COMMAND` | Show a command's options and examples |

At run end, the launcher removes the private worktree and deletes its local branch
when its tip is reachable from a freshly fetched origin branch. Unpublished tips
and Git failures keep the branch and log its name, reason and tip SHA. A kept
worktree also keeps its branch.

Every launch sweeps all roles' leftover worktrees, unregistered folders under
`.ub-agents/worktrees/`, and local `ub-agents/` or older `ub-agent/` run branches
once before polling. It keeps artifacts of live leases on this host, branches
heading open PRs or checked out in remaining worktrees, and artifacts whose
processes may still be running. Missing, older, unreadable or other-host leases,
ended runs without a PR, dirty trees and locked trees do not prevent removal.
Symlinks are removed as links. Cleanup hooks run before removal; failed hooks and
GitHub coordination or PR read failures keep affected artifacts. Decisions appear
in the launch log and terminal view, or plain output with `--no-ui`; removed
branches with tips not known remotely include their restoration SHA. Sweep
failures do not stop launch. See [stale artifact cleanup](docs/configuration.md#stale-artifact-cleanup).

The same startup sweep prunes run diagnostics whose newest file is older than
7 days, preserving live or uncertain runs and runs with kept worktrees or branches.
It also prunes ended or stale session snapshots and orphan session files older
than 7 days, preserving live sessions and the current launcher's files. One line
reports the counts and space freed when anything is pruned; retention is fixed.

In the [terminal view](docs/terminal-view.md), mouse selections copy on release;
`y` copies the current selection again. Recent activity scrolls independently of
live work to reach all 20 retained outcomes. Its fixed header reads
`Recent activity · showing N`, counting every listed outcome while the rows scroll
directly below it.

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
`.ub-agents/launch.log`. Before opening it, a launch rotates a log larger than
5 MB to `launch.log.1`, replacing the previous archive, and records the rotation
in the fresh log. Concurrent launchers can continue appending through their
existing handles.

**More machines.** Install ub-agents, clone the repository and run
`ub-agents launch`. Every account with write access can run a launcher; list them
in `launchers:` to allow only some ([launcher accounts](docs/configuration.md#launcher-accounts)).
A dedicated launcher account with `write` access is safest: `doctor` warns when an
account with `maintain` or `admin` could let agents start their own work.

**Upgrading.** Read the [release notes](https://github.com/uberblick-ai/ub-agents/releases),
stop the launcher, run `brew update && brew upgrade ub-agents`, and start it again.
When a release says launchers must be upgraded together, stop all of them first.

## Retrospectives (optional)

Agents can report losses and topics for later human review. Enable GitHub
Discussions, open one discussion per agent as its board, and give the agent its number:

```yaml
implementer:
  retrospectives: 203   # discussion number in this repository
```

Agents post through the launcher's `retrospective` command. A loss paragraph needs
a real loss or something the run needed but missed, and a change that would have
prevented it. A post may instead consist of, or end with, at most two optional
topic lines, even when the run lost nothing:

```text
Would have helped: topic A, topic B
Suggest creating: topic C, topic D
```

The first names existing guidance, records or pointers the run lacked or had to
reconstruct; the second names lasting choices no record covers for later human
review. Omit empty lines, use short comma-separated names, and reuse a name from
an existing record or earlier post when it fits. Include no per-topic rationale,
scores, identifiers or other structure; link the item for evidence. Posting is
optional, at most once per item, without repeating an earlier run's post.

A person groups topic lines over several weeks and reads the linked items for
rationale before deciding which records to create. Repetition is evidence, not a
creation threshold: one consequential compatibility or security choice can merit
a record. Records are never created automatically; this route uses no scoring,
analytics service or scheduler.

A project's own rules decide when a run creates a record. They can defer optional
new records to topic lines, so a missing proposed record alone does not block
completion. Binding decisions, behavior guarantees and human-approval gates stay
in force; this framework guidance does not override project rules.

The launcher pins the board, and a post never changes the run's outcome. Use a
scratch body file and keep public posts free of credentials, environment values,
local paths, hostnames and log excerpts; a failed post blocks nothing. Claude
agents need `"Bash({report_command} retrospective *)"` in `--allowedTools`, and
`doctor` checks that the board exists
([retrospectives](docs/configuration.md#retrospectives)).

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
mise run ci                 # optional: GitHub's checks on HEAD, locally, before pushing
```

Tests use fakes for GitHub and never call a model. This repository runs its own
loop: see [ub-agents.yaml](ub-agents.yaml) and [AGENTS.md](AGENTS.md). The roadmap
is in the [milestones](https://github.com/uberblick-ai/ub-agents/milestones).

After a release-prep PR merges, maintainers use `mise run release X.Y.Z` to publish
the GitHub release and open its tested Homebrew tap PR. The
[release skill](.agents/skills/release/SKILL.md) handles readiness and confirmation;
a maintainer merges the tap PR. See [publishing releases](docs/releases.md).

## License

MIT
