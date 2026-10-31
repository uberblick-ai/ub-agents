# ub-agents

ub-agents runs an implementation and review loop for your GitHub issues in the
background, on your own machine or server.

## Why this exists

Once coding agents could write most of the code, our job changed. We were
starting sessions, waiting for an agent to ask something, carrying its work to
a reviewer, carrying the review back and restarting runs that died. We spent
more time waiting than deciding.

So we split the work. Keep working in Claude Code or Codex to decide what to
build and how. When a piece of work is clear, hand it to the loop instead of
managing every implementation, review and revision yourself. The loop stops and
asks when it needs a person. The
[motivation page](https://agents.uberblick.ai/docs/motivation.html) has the
longer version.

Under the hood it is small: a launcher polls your repository for labels, runs
Claude Code or Codex for the matching role, and writes the result back as a
comment and a new label. GitHub holds the workflow state, the roles and their
instructions live in your repository, and there is no separate orchestration
server.

## How we use it

1. **Work with your coding agent as usual.** Discuss a feature or a bug in
   Claude Code or Codex, read the code together and settle the approach.
2. **Agree on what done means.** Scope and acceptance criteria, in a few
   lines. Small issues work best.
3. **Ask it to file the work for the loop.** There is no special command or
   integration. Your agent opens a GitHub issue with `gh` and adds the
   `needs-preparation` label, or `ready` when the issue is already clear
   enough to build. The label is the handoff, so it needs nothing beyond a
   `gh` login with write access. In a public repository, by default only a
   label from someone with the maintain or admin role starts work.
4. **A launcher picks it up.** Every launcher watches the same labels, takes
   the next eligible item by priority and milestone, and claims it with a
   comment so no other launcher runs it.
5. **The roles hand it on.** In the starter workflow a preparer checks the
   issue against the code, an implementer opens a pull request, a reviewer
   (ideally on another model) approves it or sends it back, and an integrator
   merges when your checks and merge policy allow. When an agent needs a decision, or
   your policy reserves the merge for a maintainer, the item gets
   `needs-human` and a comment saying what to decide.
6. **You keep working.** Come back to read the merged pull request, or answer
   the question on GitHub and hand the item back, which your own agent can do
   for you. See [unstick an item](https://agents.uberblick.ai/docs/guides/stuck-items.html).

```text
You, in Claude Code or Codex
  discuss → agree on scope and criteria → file the issue with needs-preparation
                                    │
The loop, on your machine or server ▼
  issue-preparer  checks the issue against the code ─┐
  implementer     opens a pull request               │   a decision is needed,
  reviewer        approves, or sends it back ↺       ├─► or a person must merge:
  integrator      merges if checks and policy allow ─┘   needs-human, you decide
                                    │                    and hand it back
                                    ▼
                          merged, issue closed
```

Labels, roles and outcomes are lines in `ub-agents.yaml`, so you can drop a
role, add one or change where a person steps in. See
[how the loop works](https://agents.uberblick.ai/docs/how-the-loop-works.html).

`ub-agents launch` shows what the loop is doing. This is our own loop on this
repository:

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


## Try it

On macOS or Linux, with `git`, `gh` logged in to an account with write access,
and `claude` or `codex` installed and signed in:

```sh
brew install uberblick-ai/tap/ub-agents
cd your-project              # a clean checkout of your default branch
ub-agents init
```

`init` asks a few questions, shows every change and opens a setup pull request
with `ub-agents.yaml` and the role instructions in `.agents/`. Review it before
you merge: check the agent permissions and the merge policy, which decides what
the loop may merge on its own. Init's
[next steps](https://agents.uberblick.ai/docs/commands/init.html#next-steps)
list what to look at. Then merge it, pull and start the launcher:

```sh
git pull
ub-agents launch
```

If you also work in this checkout yourself, clone a separate one for the
launcher. Label an issue `needs-preparation` to start. The
[installation guide](https://agents.uberblick.ai/docs/installation.html) has the
full steps.

## Three examples

**A feature, shaped first and then handed off.** We wanted the loop to be able
to notify Slack. Before filing
[#664](https://github.com/uberblick-ai/ub-agents/issues/664) we settled the
config, the event fields and what a failing hook must never break, so the issue
holds those decisions. The loop prepared it, implemented it in
[#666](https://github.com/uberblick-ai/ub-agents/pull/666), reviewed it and
merged it, with nothing left to ask.

**A bug, through preparation, review and a person's answer.**
[#630](https://github.com/uberblick-ai/ub-agents/issues/630) was a sorting bug
(`v0.5.10` came before `v0.5.9`), filed from a Claude Code session. The preparer
checked it and marked it ready, and the implementer opened
[#632](https://github.com/uberblick-ai/ub-agents/pull/632). The reviewer
[sent it back](https://github.com/uberblick-ai/ub-agents/pull/632#pullrequestreview-5638139759):
the code was fine, but three pages in our Uberblick workspace named in the
acceptance criteria were still unchanged. The implementer updated two. The third
was an approved decision that only a maintainer may change, so it stopped with
`needs-human` and asked. The PR waited three days until a maintainer
[answered](https://github.com/uberblick-ai/ub-agents/pull/632#issuecomment-6439953422)
from a Codex session and handed it back; then the reviewer approved and the
integrator merged.

**Dependency updates from Dependabot.** These arrive as pull requests, not
issues, and take a shorter path. In the repository that builds
[Uberblick](https://uberblick.ai), Dependabot is a trusted pull request author
and its `dependencies` label sends the pull request straight to the integrator. That project's merge
policy lets the integrator merge minor and patch updates that touch only
manifests and lockfiles once CI is green, gives a failing build one repair
attempt, and sends major
versions, pinned packages and changes to sensitive areas to a person. The
starter workflow does none of this; it is three settings and a few lines of
policy. See
[Letting agents auto-merge Dependabot pull requests](https://uberblick.ai/blog/2026/agents-auto-merge-dependabot).

## Controls and boundaries

- **People decide.** Add `needs-human` to any issue or pull request and the loop
  leaves it alone until you remove it. Your merge policy in
  `.agents/ub_agents.md` says what waits for a maintainer, and without an
  integrator every merge is yours.
- **Outside input waits for a maintainer.** On public repositories, outside
  issues and pull requests reach an agent only after a maintainer starts them,
  and fork pull requests are reviewed as a diff without running their code. See
  [approvals](https://agents.uberblick.ai/docs/configuration/approvals.html).
- **The models are still hosted and paid.** ub-agents is free, but agents run
  on your Claude Code or Codex seat or API key, and every run counts against it.
  Check your provider's terms for unattended use.
- **A worktree is not a sandbox.** Each run gets its own worktree and branch, but
  the agent runs as the launcher user with the permissions you grant its CLI.
  Use a dedicated GitHub account and machine for launchers; see the
  [security model](https://agents.uberblick.ai/docs/security-model.html).
- **Uberblick is optional.** We use our [Uberblick](https://uberblick.ai)
  workspace for product pages the agents read; ub-agents doesn't need it.
- **Rough edges.** GitHub only, and launchers run on macOS or Linux. Agents run
  through Claude Code, Codex or a command you configure. Runs fail and get
  retried, and an item waits as long as nobody answers it.

## Docs

- [Motivation](https://agents.uberblick.ai/docs/motivation.html) and the [FAQ](https://agents.uberblick.ai/docs/faq.html)
- [Installation](https://agents.uberblick.ai/docs/installation.html) and [upgrading](https://agents.uberblick.ai/docs/upgrading.html)
- [How the loop works](https://agents.uberblick.ai/docs/how-the-loop-works.html)
- [Security model](https://agents.uberblick.ai/docs/security-model.html)
- [Commands](https://agents.uberblick.ai/docs/commands/index.html) and [configuration](https://agents.uberblick.ai/docs/configuration/index.html)
- [Add more launchers](https://agents.uberblick.ai/docs/guides/add-a-launcher.html), [unstick an item](https://agents.uberblick.ai/docs/guides/stuck-items.html), [add a step](https://agents.uberblick.ai/docs/guides/add-a-step.html), [run on a public repository](https://agents.uberblick.ai/docs/guides/public-repos.html) and [run a command on loop events](https://agents.uberblick.ai/docs/guides/loop-events.html)

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
