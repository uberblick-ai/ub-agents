# Init flow reference

`ub-agents init` prints a Clack-style transcript. Answered steps stay in
scrollback as `◇` with a dim answer; the active question (`◆`, cyan) is a small
inline Textual app that clears itself when answered. These captures come from a
96-column terminal. The [init guide](../site/content/commands/02-init.md) describes
the choices.

The starter has five role files: issue-preparer, issue-reviewer, implementer,
reviewer and integrator. Retrospectives sets up a board for each. The preparer uses
`prepared` for small, bounded and clear work, `review` for other implementable work,
`split` for work needing multiple PRs and `needs-human` for a person's decision.
Issue review reports `approved`, `changes-requested` or `needs-human`; returned
findings reach the preparer as feedback, which it applies before reporting
`prepared` without another review. A split leaves the parent open and blocked by
prepared native sub-issues, each without workflow labels until a maintainer starts
it. Issue and PR reviewers share `needs-review`, so labels remain deduplicated.

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

```text
◆  Who answers the agents' questions?
│  They decide scope and policy questions and own workflow changes; see .agents/ub_agents.md.
│  Approvals: on (visibility (public)); see https://agents.uberblick.ai/docs/configuration/approvals
│  ● @org/core (maintains this repository)
│  ○ @operator
│  ○ Someone else
└
```

Hints show only on the highlighted option. Disabled extras show their reason:

```text
◆  Extras  (space selects, enter confirms)
│  ◻ Priorities
│  ◻ Retrospectives (Discussions are off in org/project)
│  ◻ Uberblick (needs ub: brew install uberblick-ai/tap/uberblick)
│  ◼ Tailor loop policy (first loop issue drafts repository-specific rules)
└
```

Fresh init selects **Tailor loop policy** by default. Deselect it with Space, or
enter `0` in a plain numbered prompt to select no extras. Enter keeps the default.
The extra appears in **Changes** and files **Tailor `.agents/ub_agents.md` to this
repository** labeled `ready` only after the setup PR opens. **Next** names its URL.
The issue asks the implementer to open agent guidance, README, CI configuration,
setup and release tooling, and code before proposing the required CI check,
additional maintainer-merge bullets and one to three codebase review focus points.
It forbids repeating the starter, `AGENTS.md`/`CLAUDE.md` rules or general review
advice, and requires evidence for each rule in the PR body, citing only opened
files. A maintainer merges that policy PR because it changes `.agents/`.

When no setup PR opens or filing fails, init files no issue and prints a runnable
`gh issue create` command with the same title, body and `ready` label. A failure
also shows the reason, including a missing `ready` label when label creation was
declined. Deselecting the extra suppresses the issue and command. Existing-setup
flags (`--create-boards`, `--create-priorities`) never offer this extra.

`ub-agents doctor`, including `--offline`, warns once for each starter placeholder
remaining in the configured `shared-instructions` file: `@org/maintainers`,
`<project build command>`, `<project test command>` and
`<project lint or validation command>`. It also warns when `## Merging` still
matches the starter, ignoring line wrapping and init's substituted owner handle.
These warnings never make doctor fail.

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
runtime, including when the workspace choice is **Use a hub workspace** or
**Later**. The Changes note shows the client, scope, path and planned action:

```text
│  Uberblick MCP · Claude Code · project scope · .mcp.json · add entry
│  Uberblick MCP · Codex · project scope · .codex/config.toml · reuse matching entry
│  Uberblick MCP · Codex · project scope · .codex/config.toml · keep existing entry (conflicting uberblick entry; left unchanged)
│  Manual command: ub mcp install codex
```

Only the line for the selected runtime and actual outcome appears. Init requires
`ub` 0.4.3 or newer and checks the project destination and plain `ub mcp serve`
snippet with `ub mcp install <client> --print`, which runs nothing. Nothing is
registered until **Write setup?** is confirmed. Cancelling leaves MCP config
unchanged. Project scope is the command's default; init never passes `--user`.
Matching entries are reused without rewriting and unrelated settings stay intact.
Unreadable, symlinked or conflicting config stays unchanged. If `ub` would target
a different project (for example an ancestor binding), or the setup PR would need
an ignored config, init keeps the manual command instead of registering.

## Full run

A checkout on another branch, Claude logged in, an admin account, a maintaining team, `ub` installed and all extras selected.

```text
┌   ub-agents init  v0.3.1
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
◇  Create 7 missing labels?
│  Yes
│
◇  Changes ─────────────────────────────────────────────────────────────────────────╮
│                                                                                   │
│  + ub-agents.yaml                                                                 │
│  + .agents/ ub_agents.md and 5 role files                                         │
│  ~ .gitignore +4 lines                                                            │
│  + 7 labels on org/project                                                        │
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
│  7. Use a dedicated agent account with write access; see                        │
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

### Uberblick result and Next steps

The result reports the actual MCP outcome: **registered**, **reused**, **kept
existing** or **not registered**, with the reason when applicable. Failure and
unsupported installations retain `ub mcp install claude` or `ub mcp install codex`
as the manual retry command and still allow core setup and its PR. Running agent
sessions need a restart to load the server. A new or changed project config joins
the setup-only commit; when setup files stay local, the MCP config stays local too.
The entry contains no credentials, workspace ID or hub URL: `ub mcp serve` follows
the project's `.uberblick.json`, including a binding added later. Next steps still
explain how to join a hub or select a workspace for **Later** or a failed creation.

## Declined or failed branch switch

Interactive plain init needs the default branch to open the setup PR. Declining
the switch stops before the next question, exits 1 and writes nothing:

```text
▲  Switch this checkout to main?
│  No · init needs main to open the setup PR
│  Switch to main and run ub-agents init again
```

A failed switch stops the same way, showing Git's reason in place of the `No ·`
line. No files, labels, boards, workspace or MCP config are written. Unattended
init still writes local files on any branch. The `--create-boards` and
`--create-priorities` flags use the current branch without a switch question.
An unknown current branch or default branch also leaves the existing flow unchanged.

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

These runs select only **Tailor loop policy** and print its `gh issue create`
command; they file no issue, open no PR, create no labels and register no MCP
server. Setup files remain local-only.
They set who answers the agents' questions to the first team with maintain or
admin access, falling back to the current `gh` user, and print the choice and its
source. If neither is readable, setup still completes, keeps the placeholder and
prints `Replace @org/maintainers in .agents/ub_agents.md before launch`.

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

The command above is abbreviated for this capture; init prints the full issue
body, quoted so the command can be copied verbatim.
