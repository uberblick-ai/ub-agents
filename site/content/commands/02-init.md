# ub-agents init

Start `ub-agents init` in your project's checkout to open the setup wizard. It
explains the four-role, project-owned GitHub loop and shows installed/latest
versions, machine and GitHub findings, checkout conditions and the selected runtime.
Unavailable version lookup permits continuing; init never upgrades software.
Failed or skipped prerequisites remain visible with remedies and what blocks launch.
A maintain/admin launcher account is a warning: prefer a separate
[write-access launcher account](/docs/best-practices/launcher-account.html).

The framed terminal pages use the actual terminal width, keep a single-row action
footer in a fixed position, reflow in narrow terminals and scroll longer content.
The 92×19 and 48×25 reference frames describe presentation, not a runtime width cap.
See the [ASCII reference layouts](https://github.com/uberblick-ai/ub-agents/blob/main/docs/init-wizard.md).
Use Tab/Shift-Tab, Space to
toggle choices, and Enter on a focused button. Shortcuts are Ctrl+B (Back), Ctrl+N
(Continue, Apply or Finish), Ctrl+S (contextual Skip) and Escape (Cancel).
Permissions use Y (accept and enable) or N (continue without, the default).
Ctrl+R retries board verification or result readiness; Ctrl+L selects Finish and start loop.
Narrow action labels are abbreviated; the body retains their complete meanings.
Small or dumb terminals use a readable line transcript with Back/Continue/Skip/Cancel.

Nothing is written while collecting choices. Review shows the exact file content,
effective configuration including existing local overrides, and every opted-in
remote label command and requested public discussion creation, including category
and body. Apply defaults to no and requires explicit confirmation.
Back or Cancel before Apply changes no files, directories, labels or boards.

## Files and policy

Init creates the selected configuration (default `ub-agents.yaml`),
`.agents/ub_agents.md` and four role files: issue-preparer, implementer, reviewer
and integrator. Files go beside the configuration. Existing starter paths cause
refusal before any writes; existing `AGENTS.md` / `CLAUDE.md` stay unchanged.
The shared policy references existing check guidance or supplies placeholders.
Project owners must review build/test commands, merge authority, decision-makers
and review priorities before launch; continuing the wizard does not approve them.

Runtime permissions are a separate, deferrable choice, with exact arguments shown.
The actions are Accept and enable permissions (y), Continue without permissions (n)
and Cancel (esc); continuing without permissions is the default.
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

Uberblick, priorities and retrospectives appear directly in that order. Each step
explains its benefit and says “You can enable this later”; Skip is the default.
Priority and retrospective entries are checked first, then the same page shows
“This adds to <selected config path>:” with only the proposed nested YAML keys.
Continue again confirms that preview. Changing entries requires a fresh preview;
the checked additions scroll into view before confirmation and remain proposed until Apply.

- **Uberblick shared knowledge:** a shared knowledge base for agents and humans alike.
  Learn more at [https://uberblick.at](https://uberblick.at). This is an optional
  follow-up, never a runtime dependency. Init creates no workspace/account, requests
  no secrets and enables no integration.
- **Priority:** unique labels ordered highest to lowest, with a default from that
  list or explicitly no default (unlabeled items rank below the configured labels).
  Skip leaves priority unconfigured, so all items have equal priority. Creation of
  missing priority labels is a separate default-no choice.
- **Retrospectives:** choose existing discussion numbers per role, or enter
  `create: <chosen title>` and an exact available category name to request creation.
  Init checks Discussions availability, repository identity and account creation
  access, and discovers/reuses exact chosen titles across all discussion pages.
  It validates positive integer numbers, exact URLs, open/unlocked state and
  GitHub's reported update capability as conservative evidence of write access.
  GitHub exposes no read-only comment-permission probe; init posts no test comments.
  A creation request verifies token access through the requested creation at Apply;
  account role alone is not proof of token access. Unverified destinations are never
  written to config. When manual creation or access repair is needed, the step pauses
  with Retry/Skip/Cancel. [Manual creation instructions](https://docs.github.com/en/discussions/managing-discussions-for-your-community/creating-a-discussion).
  Skip leaves posting off. Later agents publish publicly as the launcher account;
  review Claude's separate retrospective command permission.

## Results and recovery

The result shows actual files, labels and boards, configured/skipped/deferred options, remaining
label commands and next steps. Partial failures retain completed effects and show
remaining work. Inspect completed and incomplete files, repair access, then complete
missing files manually from the recovery content retained in the result transcript
and retry remaining label commands.
Boards are created/reused and verified before their config is written. Partial board
failure retains successful public creations and leaves the config unwritten. Inspect
the exact selected titles before retrying, including any creation whose response was
lost. If no starter files were written, rerunning init can reuse those boards.
Init refuses existing starters; rerunning it does not overwrite or repair partial files.

Setup success is distinct from launch readiness. Review the policy, run `ub-agents check`,
commit the files through a PR and merge them into the default branch, then run
`ub-agents doctor` from a clean default-branch launcher checkout without ahead/divergent
commits. Launch fetches and fast-forwards from origin before runs to pick up code,
configuration and policy. A separate launcher checkout is recommended if you also
code here. Agents use private worktrees/branches and PRs, never direct default-branch pushes.
Init works on a setup branch; it never switches, resets, stashes or clones implicitly,
commits, pushes, merges, installs software, changes accounts or alters protections.

**Finish** exits. **Retry readiness** reruns read-only doctor checks and the existing
launch checkout/label gates against the current configuration after external
preparation. Unresolved starter policy placeholders and missing runtime permission
arguments also block this wizard's start action. **Finish and start loop** explicitly
checks readiness again, exits setup and invokes the normal launch command only when
all gates pass. A successful retry does not launch automatically. Setup completion
and unattended defaults never automatically launch.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
ub-agents init --use-defaults --runtime claude:claude-opus-5-5:high \
  --config workflow.yaml
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
