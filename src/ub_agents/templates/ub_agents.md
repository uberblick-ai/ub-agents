# Project loop policy

## Checks

{checks}

Run the relevant checks before handoff and record the results. Keep installs inside
your worktree; never install globally.

## Merging

Only the integrator merges, by squash, once the checks above and any required CI
checks pass at the exact head it merges. No agent enables auto-merge, changes branch
protection or publishes a release. Leave the merge to `@org/maintainers` when the PR:

- changes agent configuration or guidance: `ub-agents.yaml`, `.agents/`, `AGENTS.md`
  or `CLAUDE.md`;
- changes CI, permissions or release tooling;
- changes what users rely on, such as commands, options, public APIs or output,
  beyond what the closing issue asks for.

## Human decisions

`@org/maintainers` answers scope and policy questions; replace this handle with the
project's person or team. An issue's author may answer only if they have write
access.

## Review focus

- The change stays within the issue's scope; other ideas become follow-up issues.
- New behavior has tests that fail without the change.
- No secrets, and no untrusted text reaching commands or output.
- Changed behavior is documented where users and agents read it.
- The simplest working change wins; no dead code or speculative options.
