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
│  See https://agents.uberblick.ai/docs/configuration/permissions.html
│  Your local changes come along.
│  ● Yes / ○ No
└
```

```text
◆  Create 7 missing labels?
│  Workflow:   needs-preparation, needs-changes, ready-to-merge
│              trigger the loop; see https://agents.uberblick.ai/docs/configuration/index.html
│  Priorities: prio:urgent, prio:high, prio:medium, prio:low
│  ● Yes / ○ No
└
```

Hints show only on the highlighted option. Disabled extras show their reason:

```text
◆  Extras  (space selects, enter confirms)
│  ◻ Priorities
│  ◻ Retrospectives (Discussions are off in org/project)
│  ◻ Uberblick
└
```

## Full run

A checkout on another branch, Claude logged in, an admin account, Priorities and Retrospectives selected.

```text
┌   ub-agents init  v0.3.0
│
◇  Checks
│  ✓ Machine    Python 3.13 · git · gh · ps
│  ✓ GitHub     operator · org/project · labels writable
│  ✓ Checkout   feature · clean · in sync with origin
│  ✓ Agents     claude logged in · codex not installed
│  ! Account    operator is admin on GitHub; run agents as a write-only account
│               See https://agents.uberblick.ai/docs/best-practices/launcher-account.html
│
◇  Switch this checkout to main?
│  Yes · now on main
│  Agents always work in worktrees and branches; they never commit to main.
│
◇  Which agent runs the loop?
│  Claude Code · claude-opus-5-5 · high
│  Great, Claude Code is the default for all agents.
│  Review later; see https://agents.uberblick.ai/docs/configuration/runtimes.html
│
◇  Let agents work without asking?
│  Yes · review runtime-args in ub-agents.yaml before you launch
│
◇  Extras
│  Priorities, Retrospectives
│
◇  Priority labels, highest first
│  prio:urgent, prio:high, prio:medium, prio:low
│
◇  Create 7 missing labels?
│  Yes
│
◇  Changes ───────────────────────────────────────────────────╮
│                                                             │
│  + ub-agents.yaml                                           │
│  + .agents/ ub_agents.md and 4 role files                   │
│  ~ .gitignore +4 lines                                      │
│  + 7 labels on org/project                                  │
│  + 4 retrospective boards (reused when the title exists)    │
│                                                             │
├─────────────────────────────────────────────────────────────╯
│
◇  Write setup?
│  Yes
│
◇  Setup written · boards #204, #203, #205, #206
│
◇  Next ──────────────────────────────────────────────────────────────────────────╮
│                                                                                 │
│  1. Review the agent permissions (runtime-args) in ub-agents.yaml; see          │
│     https://agents.uberblick.ai/docs/configuration/permissions.html             │
│  2. Review checks and merge authority in .agents/ub_agents.md                   │
│  3. Add your test command to --allowedTools in ub-agents.yaml                   │
│  4. Add "Bash({report_command} retrospective *)" to --allowedTools so agents    │
│     can post lessons                                                            │
│  5. Open a PR with these files and merge it                                     │
│  6. Run agents as a separate write-access account; see                          │
│     https://agents.uberblick.ai/docs/best-practices/launcher-account.html       │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
└  Then run ub-agents launch
```

A failed check turns the step title into `▲  Checks · fix these before launch; setup can continue`
and lists the fix under the failing line.

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
│
◇  Let agents work without asking?
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
├──────────────────────────────────────────────╯
│
◇  Write setup?
│  Yes
│
◇  Setup written
│
◇  Next ──────────────────────────────────────────────────────────────────────────╮
│                                                                                 │
│  1. Grant agent permissions later: uncomment runtime-args in ub-agents.yaml:    │
│     https://agents.uberblick.ai/docs/configuration/permissions.html             │
│  2. Review checks and merge authority in .agents/ub_agents.md                   │
│  3. Open a PR with these files and merge it                                     │
│  4. Want retrospectives later? Turn on Discussions if needed, then follow       │
│     https://agents.uberblick.ai/docs/best-practices/retrospectives.html         │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
◇  Create the labels yourself
│  gh label create needs-preparation --repo org/project --description '…' --color 1d76db
│  …
│
└  Then run ub-agents launch
```
