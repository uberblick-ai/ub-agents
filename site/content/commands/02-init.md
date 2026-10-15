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

- `ub-agents.yaml` with five starter roles: issue-preparer, issue-reviewer, implementer, reviewer and integrator. [How the loop works](/docs/how-the-loop-works.html) shows how they pass work along.
- `.agents/ub_agents.md` with your loop policy, plus a role file per agent. See [instructions](/docs/configuration/instructions.html).
- The workflow labels on GitHub, plus the extras you picked. [Uberblick](https://uberblick.ai) adds shared knowledge for agents and people.
- A setup PR with the files, which you can review and approve. Once it is merged, run [`ub-agents launch`](/docs/commands/launch.html). Off the default branch, the files stay local for your own PR.

Init never overwrites existing starter files, `AGENTS.md` or `CLAUDE.md`. If something fails halfway, it prints what's left to do.

## Add boards or priorities later

For an existing setup, run either flag or both:

```sh
ub-agents init --create-boards
ub-agents init --create-priorities
```

`--create-boards` adds a [retrospective](/docs/commands/retrospective.html) board for every agent that doesn't have one yet; turn on GitHub Discussions first. `--create-priorities` asks for your priority labels and creates them. Both show the changes first and add only the missing entries to `ub-agents.yaml`.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

Asks nothing, runs no checks, creates no labels and opens no PR. It prints the label commands; run `ub-agents doctor` before you launch. CI and non-terminal runs behave the same.

```text
{{help init}}
```
