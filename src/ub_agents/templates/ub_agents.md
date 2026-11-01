# Project loop policy

## Checks

{checks}

Run the relevant checks before handoff and record the results. Keep installs inside
your worktree; never install globally.

## Merging

Only the integrator merges, by squash, once the checks above and any required CI
checks pass at the exact head it merges. No agent enables auto-merge, changes branch
protection or publishes a release. Leave the merge to `@org/maintainers` when the PR:

- changes agent configuration or guidance: `ub-agents.yaml`, `.agents/` and any
  `AGENTS.md` or `CLAUDE.md`;
- changes setup, CI or release tooling;
- changes what users rely on, such as commands, options, public APIs or output,
  beyond what the closing issue asks for.

## Human decisions

`@org/maintainers` answers scope and policy questions; replace this handle with the
project's person or team. An issue's author may answer only if they have write
access.

## New issues

When the project uses milestones, give every issue you file one: the assignment's own
when the new issue blocks it or follows from it, otherwise the earliest open milestone
whose goal it fits. Leave it out only when none fits, and say so in your report.

## Review focus

- The change stays within the issue's scope; other ideas become follow-up issues.
- New behavior has tests that fail without the change.
- No secrets, and no untrusted text reaching commands or output.
- Changed behavior is documented where users and agents read it.
- The simplest working change wins; no dead code or speculative options.
