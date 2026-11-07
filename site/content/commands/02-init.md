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

The wizard asks which agent CLI runs the loop, whether agents may work without asking (for Codex, [full access without a sandbox](/docs/configuration/permissions.html#codex)), whether to keep the CLIs and `gh` up to date, who answers the agents' questions, and which extras you want: priority labels, [retrospective boards](/docs/best-practices/retrospectives.html), Uberblick and tailoring the loop policy (selected by default). Then it shows every change for you to confirm.

When both Claude Code and Codex are logged in, your choice stays the default for
issue preparation, implementation and integration. The issue reviewer and PR
reviewer use the other CLI's default model and effort, with
[`different-runtime-from`](/docs/configuration/agents.html#different-runtime-from)
set to their authoring role. Permissions, updates and the Uberblick MCP extra
cover both CLIs without another question. With one CLI logged in, every role uses
your choice.

With updates enabled, selecting the Uberblick extra also writes `runtime-updates.ub: auto` for Homebrew updates of `ub`.

## What you get

| File | What it's for |
|---|---|
| `ub-agents.yaml` | The loop's [configuration](/docs/configuration/index.html): agents, labels and outcomes. |
| `.agents/ub_agents.md` | Rules every agent follows, such as who decides what and when a PR may merge. See [what belongs in it](/docs/best-practices/instructions-as-code.html#what-goes-in-the-shared-policy). |
| `.agents/<role>.md` | One file per starter role: issue-preparer, issue-reviewer, implementer, reviewer and integrator. Adjust them to your project over time. |
| `.gitignore` | Keeps the launcher's local state out of Git. |
| `.mcp.json` and/or `.codex/config.toml` | Only with [Uberblick](https://uberblick.ai): connects the configured CLIs to it, plus `.uberblick.json` when init creates a workspace. |

The files go to a setup PR on your default branch, and init offers to create the missing workflow labels. On another branch, init offers to switch and writes nothing if you decline. With [approvals on](/docs/configuration/approvals.html#outside-input-label), the labels include `outside-input`, and on public repositories the new policy leaves PRs with it to maintainers.

The shared policy judges merge rules by the diff's effect; paths show where those
changes usually live. A maintainer's answer on the issue or PR can cover the diff
and let the integrator merge. Anything beyond that answer needs a new question.

**Tailor loop policy** files an issue labeled `ready` after the setup PR. The loop's first job is to read your repository and propose its required CI check, extra maintainer merge rules and a few review focus points, each backed by file evidence. If the repository has an install step, it also sets [checkout setup](/docs/configuration/checkout-setup.html) to the existing command and lists its lockfiles in `when-changed`; otherwise the key stays commented out. A maintainer merges that PR, including any `ub-agents.yaml` changes. The tailoring issue itself is not an answer covering the PR. When init can't file the issue, it prints the `gh issue create` command instead.

The configuration also includes a commented
[`trusted-bots`](/docs/configuration/approvals.html#trusted-bots) example so you can
choose which bots' feedback reaches agents.

With `AGENTS.md` or `CLAUDE.md`, the policy points to that file for project checks. Otherwise init fills build, test and lint commands from tasks defined in `mise.toml`, scripts in `package.json` or targets in `Makefile`, in that order for each command. It uses `mise run`, `npm run` or `make`; `check` also counts as a lint task, after `lint`. Missing commands keep placeholders. Init reads these definitions without running them and never guesses defaults such as `pytest`. Review the generated commands before launch.

Detection accepts `package.json` with a UTF-8 BOM and Makefiles with non-UTF-8 comments. A target-specific variable assignment alone does not define a Makefile target. If a source cannot be read or its JSON or TOML is invalid, init names the file and stops before writing starter files; fix the file and run init again.

If starter files already exist, init stops and changes nothing. It never touches `AGENTS.md` or `CLAUDE.md`. If something fails halfway, it prints what's left to do.

## Next steps

- Check the agent permissions (`runtime-args`) in `ub-agents.yaml`. See [permissions](/docs/configuration/permissions.html).
- Review `.agents/ub_agents.md` against [what belongs in the shared policy](/docs/best-practices/instructions-as-code.html#what-goes-in-the-shared-policy). {{discuss shared-policy}}
- If your project has its own check commands, such as `mise run ci`, add them to Claude's `--allowedTools` in `ub-agents.yaml`, for example `"Bash(mise run ci)"`.
- Merge the setup PR and pull your default branch.
- Run [`ub-agents doctor --offline`](/docs/commands/doctor.html) for one hint about commands and merging rules still to tailor. The hint adds no warnings; an unset owner stays a separate warning.
- Run [`ub-agents launch`](/docs/commands/launch.html) from a clean checkout of your default branch. If you also work in this checkout yourself, clone a separate one for the launcher.

## Add the optional steps later on

Add retrospective boards or priority labels you skipped:

```sh
ub-agents init --create-boards       # retrospective boards; turn on GitHub Discussions first
ub-agents init --create-priorities   # priority labels
```

Both show the changes first and only add what's missing.

## Unattended setup

```sh
ub-agents init --use-defaults --repository org/project
```

Writes the starter files without asking, with safe defaults: Codex runs the loop (pick another with `--runtime`), agent permissions and updates stay off, and only **Tailor loop policy** is selected. The repository's first maintaining team answers the agents' questions, or you if there is none. It opens no PR, files no issue and creates no labels; it prints the commands instead. Runs without a terminal, such as CI, work the same way. Exit code 0 means everything was written, 1 that something failed.

Unattended setup skips CLI probes and uses the chosen runtime for every role,
even when both CLIs are installed and logged in.

```text
{{help init}}
```

The [init flow reference](https://github.com/uberblick-ai/ub-agents/blob/main/docs/init-wizard.md)
keeps terminal captures for contributors changing the wizard.
