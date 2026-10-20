# ub-agents

**An autonomous coding loop for GitHub.** Any agent, no server.

ub-agents runs coding agents on your GitHub issues and pull requests. A launcher on
your machine runs the agent whose label matches each item, and the agent's outcome
moves the labels on to the next role. Claims and outcomes are comments, so GitHub
is the only state. Each role runs Claude Code, Codex or any command, with
instructions that live in your repository.

**Website and docs: [agents.uberblick.ai](https://agents.uberblick.ai)**

ub-agents is MIT licensed and early stage. It develops itself with its own loop.

## Try it

On macOS or Linux, with `git`, a logged-in `gh` and `claude` or `codex`:

```sh
brew install uberblick-ai/tap/ub-agents
cd your-project
ub-agents init      # opens a setup PR with starter roles
ub-agents launch    # after you review and merge it
```

- [Installation](https://agents.uberblick.ai/docs/installation.html): requirements and first launch
- [init](https://agents.uberblick.ai/docs/commands/init.html): what the setup PR contains and what to review
- [How the loop works](https://agents.uberblick.ai/docs/how-the-loop-works.html): labels, roles and handoffs
- [Permissions](https://agents.uberblick.ai/docs/configuration/permissions.html): what agents may do on your machine
- [Give launchers their own account](https://agents.uberblick.ai/docs/best-practices/launcher-account.html)
- [Run the loop on a public repository](https://agents.uberblick.ai/docs/guides/public-repos.html)
- [Add more launchers](https://agents.uberblick.ai/docs/guides/add-a-launcher.html) and [Unstick an item](https://agents.uberblick.ai/docs/guides/stuck-items.html)
- [Upgrading](https://agents.uberblick.ai/docs/upgrading.html)

## Contributing

This repository runs its own loop, configured in [ub-agents.yaml](ub-agents.yaml)
and [.agents/](.agents/).

- **Open an issue** for a bug or a change. A maintainer labels it to start the
  loop, which prepares it, implements it, reviews the PR and merges it. Small,
  single-change issues work best.
- **Pull requests are welcome.** Agents review fork PRs as a diff and rely on CI;
  they can't push to your fork. See [approvals](docs/approvals.md) for how outside
  input is handled.

To work on the code, use [mise](https://mise.jdx.dev), activated in your shell:

```sh
git clone https://github.com/uberblick-ai/ub-agents && cd ub-agents
mise trust                  # once; entering the checkout installs it into .venv
.venv/bin/python -m tests   # the suite; uses fakes, never calls a model
```

[AGENTS.md](AGENTS.md) has the checks, conventions and release procedure that
people and agents both follow. The roadmap is in the
[milestones](https://github.com/uberblick-ai/ub-agents/milestones).

Reference material for contributors: [configuration](docs/configuration.md),
[operations](docs/operations.md), [coordination contract](docs/coordination.md),
[terminal view](docs/terminal-view.md) and [releases](docs/releases.md).

## License

[MIT](LICENSE)
