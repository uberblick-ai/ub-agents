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

- changes agent configuration or guidance: `ub-agents.yaml`, `.agents/` or `AGENTS.md`
  beyond what the closing issue asks for;
- changes setup, CI or release tooling: `mise.toml`, `.github/` or `scripts/`
  beyond what the closing issue asks for;
- changes what decides the `Test result` check: any file under `.github/workflows/`,
  or a script under `scripts/` that `.github/workflows/test.yml` runs, even when the
  closing issue asks for it (this bullet does not cover changes to tests or source
  code);
- is labelled `outside-input` (built from outside text);
- changes the `ub-agents` command line (adds, removes or renames commands or options,
  or changes what they do or print) beyond what the closing issue asks for;
- needs every launcher of a project restarted together, because a launcher from the
  previous build would reject or misread its coordination record format, agent
  branch names, config file, or a config key or value this repository's
  `ub-agents.yaml` starts using.

"What the closing issue asks for" means the issue text as a maintainer last wrote
it, before the issue preparer's edits. Check the issue's edit history to see who
wrote each version. An edit added to the scope by the preparer, or requested only
by a preparer-written acceptance criterion, still leaves the merge to a maintainer.
If the integrator cannot establish the maintainer-written version, leave the merge
to a maintainer.

## Human decisions

`@uberblick-ai/maintainers` answers scope and policy questions. An issue's author may
answer only if they have write access.

## New issues

Give every issue you file a milestone: the assignment's own when the new issue blocks it
or follows from it, otherwise the earliest open milestone whose goal it fits. Sub-issues
take their parent's milestone. Leave it out only when none fits and say so in your
report: an issue without a milestone waits behind all milestone work.

## Review focus

When a change touches coordination or execution, scrutinize durable outcomes, lost
ownership, claim races, recovery and process cleanup. For a change to what users
install, run, configure or see, the affected `site/content/` pages must still be
accurate.
