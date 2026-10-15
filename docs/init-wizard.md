# Init flow reference

`ub-agents init` prints a Clack-style transcript. Answered steps stay in
scrollback as `◇` with a dim answer; the active question (`◆`, cyan) is a small
inline Textual app that clears itself when answered. These captures come from a
96-column terminal. The [init guide](../site/content/commands/02-init.md) describes
the choices.

## Active question

```text
◆  Switch this checkout to main?
│  ub-agents only uses main to start fresh branches and worktrees for agents.
│  Agents always work in worktrees and branches; they never commit to main.
│  See https://agents.uberblick.ai/docs/configuration/permissions
│  ● Yes / ○ No
└
```

```text
◆  Create 7 missing labels?
│  Workflow:   needs-preparation, needs-changes, ready-to-merge
│              trigger the loop; see https://agents.uberblick.ai/docs/configuration/
│  Priorities: prio:urgent, prio:high, prio:medium, prio:low
│  ● Yes / ○ No
└
```

Hints show only on the highlighted option. Disabled extras show their reason:

```text
◆  Extras  (space selects, enter confirms)
│  ◻ Priorities
│  ◻ Retrospectives (Discussions are off in org/project)
│  ◻ Uberblick (needs ub: brew install uberblick-ai/tap/uberblick)
└
```

With Uberblick selected and no `.uberblick.json` in the checkout or above it:

```text
◆  Set up an Uberblick workspace?
│  No .uberblick.json found for this checkout.
│  ● Create a local workspace (ub workspace create; .uberblick.json goes in the setup PR)
│  ○ Use a hub workspace
│  ○ Later
└
```

## Full run

A checkout on another branch, Claude logged in, an admin account, `ub` installed and all extras selected.

```text
┌   ub-agents init  v0.3.0
│
◇  Checks
│  ✓ Machine    Python 3.13 · git · gh · ps
│  ✓ GitHub     operator · org/project · labels writable
│  ✓ Checkout   feature · clean · in sync with origin
│  ✓ Agents     claude logged in · codex not logged in
│  ! Account    operator is admin on GitHub; use a dedicated agent account with write access
│               See https://agents.uberblick.ai/docs/best-practices/launcher-account
│
◇  Switch this checkout to main?
│  Yes · now on main
│  Agents always work in worktrees and branches; they never commit to main.
│
◇  Which agent runs the loop?
│  Claude Code · claude-opus-5-5 · high
│  Great, Claude Code is the default for all agents.
│  Review later; see https://agents.uberblick.ai/docs/configuration/runtimes
│
◇  Let agents work without asking?
│  Yes · review runtime-args in ub-agents.yaml before you launch
│
◇  Keep claude and gh up to date?
│  Yes · claude and gh update once a day between runs
│
◇  Extras
│  Priorities, Retrospectives, Uberblick
│
◇  Priority labels, highest first
│  prio:urgent, prio:high, prio:medium, prio:low
│
◇  Set up an Uberblick workspace?
│  Create a local workspace
│
◇  Create 7 missing labels?
│  Yes
│
◇  Changes ─────────────────────────────────────────────────────────────────────────╮
│                                                                                   │
│  + ub-agents.yaml                                                                 │
│  + .agents/ ub_agents.md and 4 role files                                         │
│  ~ .gitignore +4 lines                                                            │
│  + 7 labels on org/project                                                        │
│  + 4 retrospective boards (reused when the title exists)                          │
│  + .uberblick.json local Uberblick workspace                                      │
│                                                                                   │
│  Labels and boards are created on GitHub right away; the files go to a new PR.    │
│                                                                                   │
├───────────────────────────────────────────────────────────────────────────────────╯
│
◇  Write setup?
│  Yes
│
◇  Setup written · boards #204, #203, #205, #206
│
◇  Uberblick workspace created
│  .uberblick.json
│
◇  Setup PR opened
│  https://github.com/org/project/pull/9
│  Back on main
│
◇  Next ──────────────────────────────────────────────────────────────────────────╮
│                                                                                 │
│  1. Review the agent permissions (runtime-args) in ub-agents.yaml on the PR;    │
│     see https://agents.uberblick.ai/docs/configuration/permissions              │
│  2. Review checks and merge authority in .agents/ub_agents.md; we set up        │
│     sensible defaults you can adjust over time                                  │
│  3. Add your test command to --allowedTools in ub-agents.yaml                   │
│  4. Review and merge the setup PR: https://github.com/org/project/pull/9        │
│  5. Warning: claude loads no project guidance; add CLAUDE.md,                   │
│     .claude/CLAUDE.md or AGENTS.md so agents know how to build and test.        │
│  6. Use a dedicated agent account with write access; see                        │
│     https://agents.uberblick.ai/docs/best-practices/launcher-account            │
│  7. Connect agents to Uberblick with ub mcp install claude; see                 │
│     https://uberblick.ai                                                        │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
└  After the merge, run ub-agents launch
```

A failed check turns the step title into `▲  Checks · fix these before launch; setup can continue`
and lists the fix under the failing line.

## Local changes

A checkout with local changes (untracked files included) stops after the checks:

```text
◇  Checks
│  ✓ Machine    Python 3.13 · git · gh · ps
│  ✓ GitHub     operator · org/project · labels writable
│  ✓ Checkout   main · local changes · in sync with origin
│  ✓ Agents     claude logged in · codex logged in
│
■  This checkout has local changes; commit or stash them, then run ub-agents init again
└  Setup cancelled. Nothing was written.
```

## Cancel

```text
◇  Extras
│  Priorities
│
■  Priority labels, highest first
└  Setup cancelled. Nothing was written.
```

## Unattended

`--use-defaults`, CI and non-TTY runs:

```text
┌   ub-agents init  v0.3.0
│
◇  Checks skipped
│  Unattended setup; run ub-agents doctor before launch
│
◇  Which agent runs the loop?
│  Codex · gpt-6.1-sol · high
│  Great, Codex is the default for all agents.
│  Review later; see https://agents.uberblick.ai/docs/configuration/runtimes
│
◇  Let agents work without asking?
│  No
│
◇  Keep codex and gh up to date?
│  No
│
◇  Extras
│  None
│
◇  Changes ────────────────────────────────────╮
│                                              │
│  + ub-agents.yaml                            │
│  + .agents/ ub_agents.md and 4 role files    │
│  ~ .gitignore +4 lines                       │
│                                              │
│  The files stay local.                       │
│                                              │
├──────────────────────────────────────────────╯
│
◇  Write setup?
│  Yes
│
◇  Setup written
│
◇  Next ──────────────────────────────────────────────────────────────────────────╮
│                                                                                 │
│  1. Grant agent permissions later: uncomment runtime-args in ub-agents.yaml;    │
│     see https://agents.uberblick.ai/docs/configuration/permissions              │
│  2. Review checks and merge authority in .agents/ub_agents.md; we set up        │
│     sensible defaults you can adjust over time                                  │
│  3. Open a PR with these files and merge it                                     │
│  4. Warning: codex loads no project guidance; add AGENTS.md so agents know      │
│     how to build and test.                                                      │
│  5. Want retrospectives later? Turn on Discussions if needed, then follow       │
│     https://agents.uberblick.ai/docs/best-practices/retrospectives              │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
◇  Create the labels yourself
│  gh label create needs-preparation --repo org/project --description 'ub-agents: starts issue-preparer on an issue' --color 1d76db
│  …
│
└  Then run ub-agents launch
```
