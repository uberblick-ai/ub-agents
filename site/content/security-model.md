# Security model

Agents use the access you give their CLI and launcher. A private worktree separates each run's changes; it does not isolate the agent from the machine.

## Where agents run

Agents run on the launcher machine, as the launcher user. They inherit the launcher's environment and act with its GitHub account. ub-agents adds no permission flags of its own: you grant CLI permissions through `runtime-args`. See [Permissions](/docs/configuration/permissions.html) for configuration and examples.

## What they can reach

Within those permissions, agents can reach the worktree, the run's scratch directory, the network, every repository the launcher account can access, and anything else the granted permissions allow. The worktree and scratch directory are not an access boundary.

For example, Codex's `danger-full-access` means access to the whole machine as the launcher user, subject to that user's operating-system permissions.

## What limits access and input

- Use a dedicated GitHub agent account with write access only to the target repository, without maintain or admin access. See [Give launchers their own account](/docs/best-practices/launcher-account.html).
- Protect the default branch with a ruleset requiring checks and blocking force pushes.
- Keep secrets out of the launcher's environment.
- Use [Approvals](/docs/configuration/approvals.html) so outside input needs a maintainer's start before it reaches an agent. This controls input; it does not restrict machine access.
- Review fork pull requests as diffs without running their code on the launcher; rely on CI for their checks. This is how ub-agents handles fork PRs. See [Run the loop on a public repository](/docs/guides/public-repos.html).

## Isolation

Run launchers on a dedicated machine, VM or container to limit what an agent can affect. Built-in container support is planned for the [v0.7.0 milestone](https://github.com/uberblick-ai/ub-agents/milestone/12).
