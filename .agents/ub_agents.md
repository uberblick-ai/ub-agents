# ub-agents loop policy

## Checks

Run the checks in `AGENTS.md` before handoff and record the results. Keep installs
inside your worktree; never install globally or into the launcher's installation.

## Merging

Only the integrator merges, by squash, once a green `Test result` from the GitHub
Actions `Test` workflow applies to the exact head it merges. Every push, including the
integrator's own base merge, starts a new run: wait with `gh pr checks N --watch`.
No agent enables auto-merge, changes branch protection or publishes a release.
Leave the merge to `@uberblick-ai/maintainers` when the PR:

- changes `AGENTS.md`, `.agents/`, `ub-agents.yaml`, `.github/` or `mise.toml`;
- changes the `ub-agents` command line (adds, removes or renames commands or options,
  or changes what they do or print) beyond what the closing issue asks for;
- needs every launcher of a project restarted together, because a launcher from the
  previous build would reject or misread its coordination record format, agent
  branch names, config file, or a config key or value this repository's
  `ub-agents.yaml` starts using.

## Human decisions

`@uberblick-ai/maintainers` answers scope and policy questions. An issue's author may
answer only if they have write access.

## Review focus

When a change touches coordination or execution, scrutinize durable outcomes, lost
ownership, claim races, recovery and process cleanup. For a change to what users
install, run, configure or see, the affected `site/content/` pages must still be
accurate.
