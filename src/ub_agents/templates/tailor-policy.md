The starter `.agents/ub_agents.md` needs this repository's own loop rules.

Read the repository before editing: its agent guidance (`AGENTS.md`, `CLAUDE.md`
and applicable nested guidance), README, CI configuration, setup and release
tooling, and code and tests. Open the files you use as evidence.

Add only what that evidence supports:

- Name the required CI check that gates merges.
- Add maintainer-merge bullets for this repository that the starter does not
  already cover.
- Add one to three review focus points specific to this codebase.

Do not repeat the starter, rules from `AGENTS.md` or `CLAUDE.md` (every agent
already reads them), or general review advice. Do not invent rules where the
repository supplies no evidence. Cite only files you actually opened, and list
the evidence for each proposed rule in the PR body.

The PR changes `.agents/`, so the starter policy leaves its merge to a maintainer.
