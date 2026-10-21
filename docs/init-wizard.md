# Init flow reference

These captures show `ub-agents init` in a 96-column terminal. The
[init page](https://agents.uberblick.ai/docs/commands/init.html) explains each
choice and what init writes.

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
◆  Create 8 missing labels?
│  Workflow:   needs-preparation, needs-changes, ready-to-merge, outside-input
│              used by the loop; see https://agents.uberblick.ai/docs/configuration/
│  Priorities: prio:urgent, prio:high, prio:medium, prio:low
│  ● Yes / ○ No
└
```

```text
◆  Who answers the agents' questions?
│  They decide scope and policy questions and own workflow changes; see .agents/ub_agents.md.
│  Approvals: on (visibility (public)); see https://agents.uberblick.ai/docs/configuration/approvals
│  ● @org/core (maintains this repository)
│  ○ @operator
│  ○ Someone else
└
```

```text
◆  Extras  (space selects, enter confirms)
│  ◻ Priorities
│  ◻ Retrospectives (Discussions are off in org/project)
│  ◻ Uberblick (needs ub: brew install uberblick-ai/tap/uberblick)
│  ◼ Tailor loop policy (first loop issue drafts repository-specific rules)
└
```

Fresh init preselects **Tailor loop policy**; the init page describes the issue it
files.

With Uberblick selected and no `.uberblick.json` in the checkout or above it:

```text
◆  Set up an Uberblick workspace?
│  No .uberblick.json found for this checkout.
│  ● Create a local workspace (ub workspace create; .uberblick.json goes in the setup PR)
│  ○ Use a hub workspace
│  ○ Later
└
```

The Uberblick extra also plans project-scope MCP registration for the chosen
runtime. **Changes** shows one line for that runtime and its planned action, such
as:

```text
│  Uberblick MCP · Claude Code · project scope · .mcp.json · add entry
│  Uberblick MCP · Codex · project scope · .codex/config.toml · reuse matching entry
│  Uberblick MCP · Codex · project scope · .codex/config.toml · keep existing entry (conflicting uberblick entry; left unchanged)
│  Manual command: ub mcp install codex
```

Init writes nothing until you confirm **Write setup?**. It reuses a matching
entry and leaves conflicting or unreadable config unchanged, printing the manual
`ub mcp install` command instead. The result reports **registered**, **reused**,
**kept existing** or **not registered**; core setup and its PR continue either
way. See [How it works with Uberblick](https://agents.uberblick.ai/docs/guides/with-uberblick.html).

## Full run

A checkout on another branch, Claude logged in, an admin account, a maintaining
team, `ub` installed and all extras selected:

```text
┌   ub-agents init  v0.3.1
│
◇  Checks
│  ✓ Machine    Python 3.13 · git · gh · ps
│  ✓ GitHub     operator · org/project · labels writable
│  ✓ Checkout   feature · clean · in sync with origin
│  ✓ Agents     claude logged in · codex not logged in
│  ! Account    operator is admin, good for setup; launch the loop with a write-access agent account
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
◇  Who answers the agents' questions?
│  @org/core
│  Approvals: on (visibility (public)); see https://agents.uberblick.ai/docs/configuration/approvals
│
◇  Extras
│  Priorities, Retrospectives, Uberblick, Tailor loop policy
│
◇  Priority labels, highest first
│  prio:urgent, prio:high, prio:medium, prio:low
│
◇  Set up an Uberblick workspace?
│  Create a local workspace
│
◇  Create 8 missing labels?
│  Yes
│
◇  Changes ─────────────────────────────────────────────────────────────────────────╮
│                                                                                   │
│  + ub-agents.yaml                                                                 │
│  + .agents/ ub_agents.md and 5 role files                                         │
│  ~ .gitignore +4 lines                                                            │
│  + 8 labels on org/project                                                        │
│  + 5 retrospective boards (reused when the title exists)                          │
│  + Tailor `.agents/ub_agents.md` to this repository · ready · file after the setup PR opens
│  + .uberblick.json local Uberblick workspace                                      │
│  Uberblick MCP · Claude Code · project scope · .mcp.json · add entry               │
│                                                                                   │
│  Labels and boards are created on GitHub right away; the files go to a new PR.    │
│                                                                                   │
├───────────────────────────────────────────────────────────────────────────────────╯
│
◇  Write setup?
│  Yes
│
◇  Setup written · boards #204, #207, #203, #205, #206
│
◇  Uberblick workspace created
│  .uberblick.json
│
◇  Setup PR opened
│  https://github.com/org/project/pull/9
│  Back on main
│
◇  Tailoring issue filed
│  https://github.com/org/project/issues/10
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
│  6. First loop issue: https://github.com/org/project/issues/10 · Tailor `.agents/ub_agents.md` to this repository
│  7. Launch the loop with a write-access agent account; see                      │
│     https://agents.uberblick.ai/docs/best-practices/launcher-account            │
│  8. Uberblick MCP · Claude Code · project scope · .mcp.json · registered         │
│  9. Restart running agent sessions to pick up the Uberblick MCP server         │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
└  After the merge, pull main and run ub-agents launch
```

A failed check turns the step title into `▲  Checks · fix these before launch; setup can continue`
and lists the fix under the failing line.

## Declined or failed branch switch

Declining the switch stops before the next question, exits 1 and writes nothing:

```text
▲  Switch this checkout to main?
│  No · init needs main to open the setup PR
│  Switch to main and run ub-agents init again
```

A failed switch stops the same way and shows Git's reason in place of the `No ·`
line.

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

Unattended runs (`--use-defaults`, `CI` set or no TTY) never prompt. They
preselect only **Tailor loop policy**, print the label and tailoring-issue
commands, and open no PR:

```text
┌   ub-agents init  v0.3.1
│
◇  Checks skipped
│  Unattended setup
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
◇  Who answers the agents' questions?
│  @org/maintainers · first maintaining team; edit .agents/ub_agents.md to change it
│
◇  Extras
│  Tailor loop policy
│
◇  Changes ────────────────────────────────────╮
│                                              │
│  + ub-agents.yaml                            │
│  + .agents/ ub_agents.md and 5 role files    │
│  ~ .gitignore +4 lines                       │
│  + Tailor `.agents/ub_agents.md` to this repository · ready · print the filing command
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
◇  File the tailoring issue yourself
│  File this issue after the setup PR merges and the ready label exists
│
gh issue create --repo org/project --title 'Tailor `.agents/ub_agents.md` to this repository' --label ready --body 'The starter …'
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
│  5. Want retrospectives later? Turn on Discussions if needed, then run          │
│     ub-agents init --create-boards                                             │
│                                                                                 │
├─────────────────────────────────────────────────────────────────────────────────╯
│
◇  Create the labels yourself
│  gh label create needs-preparation --repo org/project --description 'ub-agents: starts issue-preparer on an issue; added when issue-reviewer reports changes-requested' --color c5def5
│  gh label create needs-review --repo org/project --description 'ub-agents: starts issue-reviewer on an issue; starts reviewer on a PR' --color fbca04
│  …
│
└  Then run ub-agents launch
```

The capture shortens the issue body. Init prints it in full, quoted so you can
copy the command as is.
