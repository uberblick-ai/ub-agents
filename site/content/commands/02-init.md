# ub-agents init

Start `ub-agents init` in your project's checkout to open the setup wizard. It
explains the four-role, project-owned GitHub loop and shows installed/latest
versions, machine and GitHub findings, checkout conditions and the selected runtime.
Unavailable version lookup permits continuing; init never upgrades software.
Failed or skipped prerequisites remain visible with remedies and what blocks launch.
A maintain/admin launcher account is a warning: prefer a separate
[write-access launcher account](/docs/best-practices/launcher-account.html).

The framed terminal pages keep their header and action footer in fixed positions,
reflow in narrow terminals and scroll longer content. Use Tab/Shift-Tab, Space to
toggle choices, and Enter on a focused button. Shortcuts are Ctrl+B (Back), Ctrl+N
(Continue, Apply or Finish), Ctrl+S (contextual Skip) and Escape (Cancel).
Small or dumb terminals use a readable line transcript with Back/Continue/Skip/Cancel.

Nothing is written while collecting choices. Review shows the exact file content,
effective configuration including existing local overrides, and every opted-in
remote label command. Apply defaults to no and requires explicit confirmation.
Back or Cancel before Apply changes no files, directories or GitHub labels.

## Files and policy

Init creates the selected configuration (default `ub-agents.yaml`),
`.agents/ub_agents.md` and four role files: issue-preparer, implementer, reviewer
and integrator. Files go beside the configuration. Existing starter paths cause
refusal before any writes; existing `AGENTS.md` / `CLAUDE.md` stay unchanged.
The shared policy references existing check guidance or supplies placeholders.
Project owners must review build/test commands, merge authority, decision-makers
and review priorities before launch; continuing the wizard does not approve them.

Runtime permissions are a separate, deferrable choice, with exact arguments shown.
Codex starter permissions grant full access without its sandbox. Claude grants
unattended edits and allowed git/gh/report commands; add project check commands to
`--allowedTools` before launch. These arguments are never silently enabled or broadened.
See [permissions](/docs/configuration/permissions.html).

Init adds missing `.ub-agents/` and `ub-agents.local.yaml` ignore entries, preserving
existing entries. It creates no local override file; an existing
[local override layer](/docs/configuration/index.html#machine-local-overrides) stays
unchanged and is included in validation and review.

Missing workflow labels are a separate default-no choice. Existing labels remain
unchanged. Decline or an unavailable label lookup defers creation and prints commands.

## Optional features

Select features to see only their compact setup pages; every feature can be skipped.

- **Priority:** unique labels ordered highest to lowest, with a default from that
  list or explicitly no default (unlabeled items rank below the configured labels).
  Skip leaves priority unconfigured, so all items have equal priority. Creation of
  missing priority labels is a separate default-no choice.
- **Retrospectives:** existing positive-integer discussion numbers for chosen roles.
  Init validates read access and the exact target repository URL. Invalid or unreadable
  boards cannot be enabled: fix and retry or Skip, which leaves posting off.
  Posting access remains **unverified**; later agents publish publicly as the launcher
  account. Give its token Discussions read/write access and review Claude's separate
  retrospective command permission. [Create discussions yourself](https://docs.github.com/en/discussions/managing-discussions-for-your-community/creating-a-discussion);
  init creates no boards and posts no test comments.
- **Uberblick shared knowledge:** a recommended optional follow-up, never a runtime
  dependency. Init creates no workspace/account, requests no secrets and enables no integration.

## Results and recovery

Finish shows actual files and labels, configured/skipped/deferred options, remaining
label commands and next steps. Partial failures retain completed effects and show
remaining work. Inspect completed and incomplete files, repair access, then complete
missing files manually from the recovery content retained in the result transcript
and retry remaining label commands.
Init refuses existing starters; rerunning it does not overwrite or repair partial files.

Setup success is distinct from launch readiness. Review the policy, run `ub-agents check`,
commit the files through a PR and merge them into the default branch, then run
`ub-agents doctor` from a clean default-branch launcher checkout without ahead/divergent
commits. Launch fetches and fast-forwards from origin before runs to pick up code,
configuration and policy. A separate launcher checkout is recommended if you also
code here. Agents use private worktrees/branches and PRs, never direct default-branch pushes.
Init works on a setup branch; it never switches, resets, stashes or clones implicitly,
launches, commits, pushes, merges, installs software, changes accounts or alters protections.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
ub-agents init --use-defaults --runtime claude:claude-opus-5-5:high --config workflow.yaml
```

`--use-defaults` bypasses every prompt even in a terminal. CI (nonempty `CI`) or
non-TTY stdin/stdout also uses defaults without prompts. These paths honor
`--repository`, `--runtime`, `--config` and the existing starter defaults. They leave
permission arguments commented out, priority unconfigured, retrospectives off and
Uberblick integration off; they create no GitHub labels. Existing local overrides
still take precedence. A concise defaults summary, remaining tasks and label commands
are printed. No wizard version/prerequisite lookups run. Without `--repository`,
the existing `gh repo view --json nameWithOwner` inference is the only GitHub call.

```text
{{help init}}
```
