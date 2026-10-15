# ub-agents init

Run `ub-agents init` in a clean checkout of your project. It checks your machine and GitHub access, asks a few questions and shows every change before writing anything. Cancel at any point and nothing is left behind.

```text
┌   ub-agents init  v0.3.1
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
- whether agents may edit, commit and use `gh` without asking
- whether to keep the agent CLI and `gh` up to date
- who answers the agents' questions
- which extras you want: priority labels, retrospective boards and Uberblick

Then it shows every change and asks once before writing.

## What you get

| File | What it's for |
|---|---|
| `ub-agents.yaml` | The loop's configuration: agents, labels and outcomes. |
| `.agents/ub_agents.md` | Rules every agent follows, such as who decides what and when a PR may merge. |
| `.agents/<role>.md` | One file per starter role: issue-preparer, issue-reviewer, implementer, reviewer and integrator. They work well as they are; adjust them to your project over time. |
| `.gitignore` | Keeps the launcher's local state and machine-only settings out of Git. |
| `.mcp.json` or `.codex/config.toml` | Only with [Uberblick](https://uberblick.ai): connects your agent to it, plus `.uberblick.json` when init creates a workspace. |

Init also creates the workflow labels on GitHub, plus any extras you picked. The files go to a setup PR, which you can review and approve. [How the loop works](/docs/how-the-loop-works.html) shows how the roles pass work along.

Init never overwrites existing starter files, `AGENTS.md` or `CLAUDE.md`. If something fails halfway, it prints what's left to do.

## Next steps

- Check the agent permissions (`runtime-args`) in `ub-agents.yaml`. See [permissions](/docs/configuration/permissions.html).
- Check the build and test commands and who may merge in `.agents/ub_agents.md`.
- For Claude, add your test command to `--allowedTools` in `ub-agents.yaml`.
- Merge the setup PR.
- Run [`ub-agents launch`](/docs/commands/launch.html) from a clean checkout of your default branch.

## Add the optional steps later on

```sh
ub-agents init --create-boards       # retrospective boards; turn on GitHub Discussions first
ub-agents init --create-priorities   # priority labels
```

Both show the changes first and only add what's missing.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

Asks nothing, runs no checks, creates no labels and opens no PR. It prints the label commands; run `ub-agents doctor` before you launch. CI and non-terminal runs behave the same.

```text
{{help init}}
```
