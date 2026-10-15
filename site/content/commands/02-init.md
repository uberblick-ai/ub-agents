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
cancel leaves no files, labels or boards.

## Questions

1. **Agent:** Claude Code or Codex. The detected, logged-in CLI is preselected; with
   both, Codex. `--runtime cli:model:effort` sets the default.
2. **Permissions:** lets agents edit, commit and use `gh` without asking. Yes is the
   default; review `runtime-args` in `ub-agents.yaml` before you launch. Agents always
   work in worktrees and branches; they never commit to main. See
   [permissions](/docs/configuration/permissions.html).
3. **Extras**, all off by default:
   - **Priorities:** priority labels, highest first, that order the
     [queue](/docs/configuration/queue.html).
   - **Retrospectives:** a lessons board for each of the four roles. Init reuses a
     discussion with the same title (for example "Implementer retrospectives") or
     creates it in the General category. Needs Discussions turned on; see
     [retrospectives](/docs/best-practices/retrospectives.html).
   - **Uberblick:** a link to [uberblick.ai](https://uberblick.ai), shared knowledge
     for agents and humans. Init enables no integration.
4. **Labels:** creates missing workflow and priority labels. Existing labels stay
   unchanged; declining prints the `gh label create` commands.
5. **Write setup?** shows what will change and writes it.

Init asks for the repository only when it can't infer it from the checkout. When the
checkout is on another branch, it asks to switch to the default branch: launch uses
that branch only to start fresh branches and worktrees for agents and never commits
to it. If Git refuses the switch, init says why and continues.

Init stops before the first question when the checkout has local changes, untracked
files included: commit or stash them, then run it again.

## Checks

The checks run before the first question and never block setup. A failed check
shows its fix; launch needs it fixed. A maintain or admin account is a warning:
prefer a separate [write-access launcher account](/docs/best-practices/launcher-account.html).

## Files

Init writes `ub-agents.yaml`, `.agents/ub_agents.md` and four role files beside it,
and adds missing `.ub-agents/` and `ub-agents.local.yaml` entries to `.gitignore`.
It refuses to run when a starter file already exists, never overwrites anything and
leaves `AGENTS.md`, `CLAUDE.md` and an existing `ub-agents.local.yaml` unchanged.
Init never commits or pushes.

When something fails halfway, init keeps what it did and prints what is left,
including the content of files it could not write and the remaining label commands.

## Next steps

Init ends with a short list: review permissions and `.agents/ub_agents.md`, add your
test command for Claude, then open a PR with the files and merge it. Launch from a
clean default-branch checkout with `ub-agents launch`.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

`--use-defaults`, a nonempty `CI` and non-TTY runs ask nothing and run no checks.
They keep permissions commented out, leave the extras off, create no labels and
print the label commands. Without `--repository`, `gh repo view` infers it.
`TERM=dumb` asks the same questions as numbered prompts.

```text
{{help init}}
```
