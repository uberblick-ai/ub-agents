# ub-agents

**An autonomous coding loop for GitHub.** Any agent, no server.

ub-agents runs coding agents on GitHub issues and pull requests and passes the work
between roles with labels. To use it, start at
**[agents.uberblick.ai](https://agents.uberblick.ai)**. This README is for working
on ub-agents itself.

Built-in runtimes are Claude Code, Codex and opencode, including local models through
Ollama. See [runtimes](https://agents.uberblick.ai/docs/configuration/runtimes.html).

`ub-agents launch` running this repository's own loop:

```text
╭─ Work · pass complete ─────────────────────╮ ╭─ Agent ───────────────────────────────────────────────────────╮
│                                            │ │                                                               │
│  Running · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   1 Agent  2 Issue  3 Runs │ Formatted  Raw (u)               │
│  ⠇ #248 Refresh the queue while an… 04:13  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│    implementer · this launcher · attempt…  │ │  #248 Refresh the queue while an agent runs                   │
│                                            │ │  implementer · claude opus high · attempt 1                   │
│  Needs attention · 1 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│  ? #255 Keep run scratch outside the… 19h  │ │   11:07:31 I'll read how the launcher polls before            │
│    implementer · needs-human · Decide wh…  │ │            changing anything.                                 │
│                                            │ │   11:07:51 ▸ Read src/ub_agents/polling.py                    │
│  Eligible · 2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │ │   11:08:30 ▸ Read src/ub_agents/loop.py                       │
│  ● ⌥252 Drop inherited agent variab… next  │ │   11:09:10 The queue only refreshes between runs. I will      │
│    reviewer                                │ │            poll read-only while the agent works and claim     │
│                                            │ │            nothing until it is idle.                          │
│  ● #191 Shorter ub-agents doctor o… ready  │ │   11:09:30 ▸ Edit src/ub_agents/loop.py +5 -2                 │
│    implementer · 1/5 failures              │ │   11:10:09 ▸ Edit tests/test_loop.py +3 -1                    │
│  Recent activity · showing 4 ┄┄┄┄┄┄┄┄┄┄┄┄  │ │   11:10:49 ▸ Bash python -m tests                             │
│  ✓ ⌥269 Send fixable check failur… merged  │ │   11:11:29 ▸ Bash git push -u origin ub-agents/248-implem…    │
│    integrator · 11:04 · Squash-merged af…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥269 Send fixable check fail… approved  │ │                                                               │
│    reviewer · 10:45 · Approved the curre…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥267 Release 0.1.15             merged  │ │                                                               │
│    integrator · 10:28 · Squash-merged af…  │ │                                                               │
│                                            │ │                                                               │
│  ✓ ⌥264 Open the selected item fr… merged  │ │                                                               │
│    integrator · 10:11 · Squash-merged af…  │ │                                                               │
│                                            │ │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  │
│                                            │ │  ⠧ implementer running · no outcome reported                  │
╰────────────────────────────────────────────╯ ╰───────────────────────────────────────────────────────────────╯
ub-agents · running assignment                                                            1-3 tabs ? keys q quit
```

## Guides

- [Installation](https://agents.uberblick.ai/docs/installation.html) and [upgrading](https://agents.uberblick.ai/docs/upgrading.html)
- [How the loop works](https://agents.uberblick.ai/docs/how-the-loop-works.html)
- [Commands](https://agents.uberblick.ai/docs/commands/index.html) and [configuration](https://agents.uberblick.ai/docs/configuration/index.html)
- [Add more launchers](https://agents.uberblick.ai/docs/guides/add-a-launcher.html), [unstick an item](https://agents.uberblick.ai/docs/guides/stuck-items.html), [add a step](https://agents.uberblick.ai/docs/guides/add-a-step.html) and [run on a public repository](https://agents.uberblick.ai/docs/guides/public-repos.html)

## Contributing

This repository runs its own loop, configured in [ub-agents.yaml](ub-agents.yaml)
and [.agents/](.agents/).

- **Open an issue** for a bug or a change. A maintainer labels it to start the
  loop, which prepares it, implements it, reviews the PR and merges it. Small,
  single-change issues work best.
- **Pull requests are welcome.** Agents review fork PRs as a diff and rely on CI;
  they can't push to your fork. See [approvals](docs/approvals.md) for how outside
  input is handled.
- **Found a vulnerability?** Report it privately as [SECURITY.md](SECURITY.md)
  describes, not in an issue.

[AGENTS.md](AGENTS.md) has the checks, conventions, changelog and release rules
that people and agents both follow. The roadmap is in the
[milestones](https://github.com/uberblick-ai/ub-agents/milestones).

## Local development

With [mise](https://mise.jdx.dev) activated in your shell:

```sh
git clone https://github.com/uberblick-ai/ub-agents && cd ub-agents
mise trust                  # once; entering the checkout installs it into .venv
ub-agents --version         # runs this checkout's code, not an installed release
.venv/bin/python -m tests   # the suite; fakes GitHub and never calls a model
ub-agents doctor --offline  # validates this repository's own loop setup
mise run ci                 # optional: CI's checks on HEAD in a fresh worktree
```

Without mise, `python3 -m venv .venv && .venv/bin/pip install -e .` sets up the
same `.venv`.

The test runner uses every core by default. Pass test names to run a subset,
`-j N` to set the worker count, `--durations N` to list the slowest tests and
`--timeout SECONDS` to change the five-minute limit per test.

The website builds from `site/content/` into `site/out/` with
`.venv/bin/python site/build.py`; open `site/out/index.html` to preview it.
Workflow diagrams have their own [README](site/diagrams/README.md).

Reference for contributors: [configuration](docs/configuration.md),
[coordination contract](docs/coordination.md), [approvals](docs/approvals.md),
[polling](docs/polling.md), [operations](docs/operations.md),
[terminal view](docs/terminal-view.md), [init wizard](docs/init-wizard.md),
[session snapshots](docs/session-snapshots.md) and [releases](docs/releases.md).

## License

[MIT](LICENSE)
