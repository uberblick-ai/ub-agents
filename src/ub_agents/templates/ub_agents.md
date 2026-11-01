# Project loop policy

## Checks

{checks}

Run the relevant checks before handoff and record the results. Keep installs inside
your worktree; never install globally.

## Merging

Only the integrator merges, by squash, once the checks above and every CI check
pass at the exact head it merges. No agent enables auto-merge, changes branch
protection or publishes a release. Leave the merge to `@org/maintainers` when the PR:

- changes agent configuration or guidance: `ub-agents.yaml`, `.agents/` and any
  `AGENTS.md` or `CLAUDE.md`;
- changes setup, CI or release tooling;
- adds a runtime dependency;
- changes what users rely on, such as commands, options, public APIs or output,
  beyond what the closing issue asks for.

Only for the setup, CI or release tooling rule, a change limited to a verified minor
or patch version bump of an existing dependency or workflow's `uses:` pin does not
trigger that rule. All other maintainer-merge rules still apply. If the release
level cannot be verified, leave the merge to a maintainer.

## Human decisions

`@org/maintainers` answers scope and policy questions; replace this handle with the
project's person or team. An issue's author may answer only if they have write
access. An answer covers what it names; anything beyond it is a new question.

## Review focus

- The change stays within the issue's scope; other ideas become follow-up issues.
- New behavior has tests that fail without the change.
- Callers of changed behavior still work; a finding names a concrete input that goes
  wrong.
- No secrets, and no untrusted text reaching commands or output.
- Changed behavior is documented where users and agents read it; search the docs for
  the old wording, not only the pages the issue names.
- The simplest working change wins; no dead code or speculative options.
