# ub-agents init

Run `ub-agents init` from the root of a clean checkout of your project. It checks your machine and GitHub access and asks a few questions. Cancel any time before you confirm, and nothing is written.

```text
┌   ub-agents init
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

## What to expect

The wizard asks:

- which agent CLI runs the loop
- whether agents may work without asking; for Codex that means [full access without a sandbox](/docs/configuration/permissions.html#codex)
- whether to keep the agent CLI and `gh` up to date
- who answers the agents' questions
- which extras you want: priority labels, [retrospective boards](/docs/best-practices/retrospectives.html), Uberblick and tailoring the loop policy (selected by default)

Then it shows every change for you to confirm.

## What you get

| File | What it's for |
|---|---|
| `ub-agents.yaml` | The loop's [configuration](/docs/configuration/index.html): agents, labels and outcomes. |
| `.agents/ub_agents.md` | Rules every agent follows, such as who decides what and when a PR may merge. See [what belongs in it](/docs/best-practices/instructions-as-code.html#what-goes-in-the-shared-policy). |
| `.agents/<role>.md` | One file per starter role: issue-preparer, issue-reviewer, implementer, reviewer and integrator. They work well as they are; adjust them to your project over time. |
| `.gitignore` | Keeps the launcher's local state and machine-only settings out of Git. |
| `.mcp.json` or `.codex/config.toml` | Only with [Uberblick](https://uberblick.ai): connects your agent to it, plus `.uberblick.json` when init creates a workspace. |

Init offers to create the missing workflow labels, plus any extras you picked. The files go to a setup PR on your default branch, which you can review and approve. On another branch, init offers to switch and stops without writing anything if you decline. [How the loop works](/docs/how-the-loop-works.html) shows how the roles pass work along.

With [approvals on](/docs/configuration/approvals.html#outside-input-label), the
optional labels include `outside-input` for work built from outside text. On
public repositories with approvals on, the new shared policy leaves PRs with that label to
maintainers for merging. Existing policy files are kept unchanged.

Fresh init selects **Tailor loop policy** by default and lists it in **Changes**. After opening the setup PR, it files **Tailor `.agents/ub_agents.md` to this repository** labeled `ready`; **Next** includes the issue's URL. The first loop issue asks the implementer to read the repository and propose its required CI check, additional maintainer merge rules and one to three review focus points. Every rule needs file evidence in the PR body; the issue says to avoid repeating the starter, agent guidance or general review advice. A maintainer merges the resulting policy PR.

Without a setup PR, or if the issue cannot be filed (for example, you declined creating the missing `ready` label), init prints a runnable `gh issue create` command with the same title, body and label. A failed filing also shows its reason. Deselecting **Tailor loop policy** skips both filing and the command. In a plain numbered prompt, Enter keeps the selected extra and `0` selects none.

If starter files already exist, init stops and changes nothing. It never touches `AGENTS.md` or `CLAUDE.md`. If something fails halfway, it prints what's left to do.

## Next steps

- Check the agent permissions (`runtime-args`) in `ub-agents.yaml`. See [permissions](/docs/configuration/permissions.html).
- Review `.agents/ub_agents.md` against [what belongs in the shared policy](/docs/best-practices/instructions-as-code.html#what-goes-in-the-shared-policy). {{discuss shared-policy}}
- If your project has its own check commands, such as `mise run ci`, add them to Claude's `--allowedTools` in `ub-agents.yaml`, for example `"Bash(mise run ci)"`.
- Merge the setup PR and pull your default branch.
- Run [`ub-agents doctor --offline`](/docs/commands/doctor.html) to check for remaining starter placeholders and an unchanged Merging section. These warnings do not fail doctor.
- Run [`ub-agents launch`](/docs/commands/launch.html) from a clean checkout of your default branch. If you also work in this checkout yourself, clone a separate one for the launcher.

## Add the optional steps later on

If you skipped retrospective boards or priority labels during setup, add them later with these options:

```sh
ub-agents init --create-boards       # retrospective boards; turn on GitHub Discussions first
ub-agents init --create-priorities   # priority labels
```

Both show the changes first and only add what's missing. Neither offers the tailoring issue; that extra belongs to fresh init only.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

Writes the starter files without asking, with safe defaults: Codex runs the loop (pick another with `--runtime`), agent permissions and updates stay off, and only **Tailor loop policy** is selected. The first team that maintains the repository answers the agents' questions, or you if there is none. It opens no setup PR, files no issue and creates no labels; it prints the label commands and the tailoring issue's `gh issue create` command instead. CI and runs without a terminal work the same way. Init exits with 0 when everything is written and 1 when something fails, for example when starter files already exist.

```text
{{help init}}
```
