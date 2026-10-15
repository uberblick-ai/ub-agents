# ub-agents init

Run `ub-agents init` in your project's checkout. It checks your machine, asks a few
questions and writes the starter files after one confirmation.

```text
┌   ub-agents init  v0.3.0
│
◇  Checks
│  ✓ Machine    Python 3.13 · git · gh · ps
│  ✓ GitHub     operator · org/project · labels writable
│  ✓ Checkout   main · clean · in sync with origin
│  ✓ Agents     claude logged in · codex not installed
│
◆  Which agent runs the loop?
│  Model and effort can be changed later in ub-agents.yaml.
│  ● Claude Code (claude-opus-5-5 · high)
│  ○ Codex
└
```

Use the arrow keys (or `h` `j` `k` `l`) to move, Space to select an extra, Enter to
continue and Escape to cancel. Nothing is written before **Write setup?**, and a
cancel leaves no files, labels or boards, no branch and no PR.

## Questions

1. **Agent:** Claude Code or Codex. The detected, logged-in CLI is preselected; with
   both, Codex. `--runtime cli:model:effort` sets the default.
2. **Permissions:** lets agents edit, commit and use `gh` without asking. Yes is the
   default; review `runtime-args` in `ub-agents.yaml` before you launch. Agents always
   work in worktrees and branches; they never commit to main. See
   [permissions](/docs/configuration/permissions.html).
3. **Updates:** keeps the agent CLI and `gh` up to date once a day between runs,
   each with its own updater. Yes is the default; see
   [runtime updates](/docs/configuration/runtime-updates.html).
4. **Questions:** who answers the agents' scope and policy questions, written into
   `.agents/ub_agents.md`. Init offers the teams that maintain the repository, then
   your own account, or any `@user` or `@org/team`. This step also shows the effective
   [approvals](/docs/configuration/approvals.html) policy and its source: on for public
   repositories, off for private and internal ones unless configured explicitly.
5. **Extras**, all off by default:
   - **Priorities:** priority labels, highest first, that order the
     [queue](/docs/configuration/queue.html).
   - **Retrospectives:** agents post learnings per run, used to optimize delivery.
     Init sets up a board for each of the five roles: it reuses a
     discussion with the same title (for example "Implementer retrospectives") or
     creates it in the General category, and Claude agents get the permission to
     post. Needs Discussions turned on; see
     [retrospectives](/docs/best-practices/retrospectives.html).
   - **Uberblick:** [uberblick.ai](https://uberblick.ai), shared knowledge for agents
     and humans. Needs `ub` (`brew install uberblick-ai/tap/uberblick`). Init uses the
     nearest `.uberblick.json`; without one it offers to create a local workspace
     with `ub workspace create`, which goes in the setup PR, or explains how to join
     a hub workspace. Before **Write setup?**, Changes shows the chosen client,
     project scope, config path and whether init will add, reuse or keep an entry.
     After confirmation, init runs `ub mcp install claude` or `ub mcp install codex`
     (project scope is the default in `ub` 0.4.3). Claude Code uses `.mcp.json`;
     Codex uses `.codex/config.toml`. The entry runs plain `ub mcp serve`, with no
     workspace pins, hub URLs or credentials, and follows the project's binding.
     It also registers when you pick a hub workspace or Later. Matching entries
     are reused without rewriting, unrelated settings are preserved, and conflicts
     stay unchanged with a manual command. Init verifies the destination through
     `ub`'s read-only `--print` option; unsupported installations register nothing.
6. **Labels:** creates missing workflow and priority labels. Existing labels stay
   unchanged; declining prints the `gh label create` commands.
7. **Write setup?** shows what will change and writes it. Labels and boards are
   created on GitHub right away; the files go to a setup PR.

Init asks for the repository only when it can't infer it from the checkout. When the
checkout is on another branch, it asks to switch to the default branch: launch uses
that branch only to start fresh branches and worktrees for agents and never commits
to it. If Git refuses the switch, init says why and continues.

Init stops before the first question when the checkout has local changes, untracked
files included: commit or stash them, then run it again.

## Checks

The checks run before the first question. Local changes stop setup; other failed
checks show their fixes and let setup continue, but launch needs them fixed.
A maintain or admin account is a warning:
prefer a separate [write-access launcher account](/docs/best-practices/launcher-account.html).

## Files

Init writes `ub-agents.yaml`, `.agents/ub_agents.md` and five role files beside it,
and adds missing `.ub-agents/` and `ub-agents.local.yaml` entries to `.gitignore`.
It refuses to run when a starter file already exists, never overwrites starter files and
leaves `AGENTS.md`, `CLAUDE.md` and an existing `ub-agents.local.yaml` unchanged.

The roles are issue-preparer, issue-reviewer, implementer, reviewer and integrator.
The preparer sends small, bounded and clear work straight to `ready` with `prepared`,
and other implementable work to the issue-reviewer with `review`. Approval gives
`ready`; `changes-requested` returns findings as feedback to the preparer, which
applies them and reports `prepared` without another review. Both issue roles can
report `needs-human` for decisions that need a person.

For work that needs multiple PRs, `split` leaves the parent open and blocked by
native sub-issues with prepared bodies and no workflow labels. A maintainer labels
each sub-issue before it starts. The issue-reviewer and PR reviewer share
`needs-review`, distinguished by item kind, so there is no extra label.
The starter recommends a different runtime for issue review; it cannot enforce
`different-runtime-from` on issues today.

On the default branch, init then commits the files on a new `setup-ub-agents`
branch, pushes it, opens the setup PR and switches back. If that fails, it says
how far it got so you can open the PR yourself. Off the default branch and in
unattended setup, the files stay local and uncommitted.
This includes a project MCP config created or changed by the Uberblick extra.
Init leaves ignored MCP config files unchanged when opening a setup PR, so private
settings stay local and cannot block the other setup files from reaching the PR.

When something fails halfway, init keeps what it did and prints what is left,
including the content of files it could not write and the remaining label commands.

## Next steps

Init ends with a short list: review permissions and `.agents/ub_agents.md` (sensible
defaults you can adjust over time), add your test command for Claude, then review and
merge the setup PR. Launch from a clean
default-branch checkout with `ub-agents launch` after the merge.
With Uberblick selected, the result reports registered, reused, kept existing or
not registered, with any reason and the exact manual command to retry. Registration
failure does not block core setup or its PR. Restart running agent sessions to
pick up the server. Joining a hub or binding a workspace later remains a manual step.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

`--use-defaults`, a nonempty `CI` and non-TTY runs ask nothing and run no checks.
They keep permissions and updates commented out, leave the extras off, register no
MCP server, create no labels, open no PR and print the label commands. Without `--repository`, `gh repo view` infers it.
`TERM=dumb` asks the same questions as numbered prompts.

```text
{{help init}}
```
